# hamster_server.py — local + Render-hosted video downloader + pro player
# Run: python hamster_server.py
# Env: PORT (auto on Render), HOST, HAMSTER_API_KEY (optional), HAMSTER_OUT_DIR
from __future__ import annotations

import glob as _glob
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from flask import Flask, Response, jsonify, request

# ============================= config ====================================
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "7823"))
API_KEY = os.environ.get("HAMSTER_API_KEY", "").strip()
IS_LOCAL = HOST in ("127.0.0.1", "localhost")


def _pick_out_dir() -> Path:
    env = os.environ.get("HAMSTER_OUT_DIR")
    if env:
        p = Path(env).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    for cand in (
        Path("/storage/emulated/0/BDSEER"),
        Path("/sdcard/BDSEER"),
        Path("/tmp/Hamster") if not IS_LOCAL else Path.home() / "Downloads" / "Hamster",
        Path.home() / "Downloads" / "Hamster",
        Path.cwd() / "Hamster",
    ):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            t = cand / ".write_test"
            t.write_text("ok", "utf-8")
            t.unlink()
            return cand
        except Exception:
            continue
    p = Path.cwd() / "Hamster"
    p.mkdir(parents=True, exist_ok=True)
    return p


OUT_DIR = _pick_out_dir()
HISTORY_FILE = OUT_DIR / ".hamster_history.json"
MAX_WORKERS = int(os.environ.get("HAMSTER_WORKERS", "3"))
HISTORY_LIMIT = 200

DEFAULT_TPL = "%(title)s.%(ext)s"  # ID removed per request

app = Flask(__name__)

QUALITY_MAP = {
    "best": "bv*+ba/b",
    "2160": "bv*[height<=2160]+ba/b[height<=2160]/b[height<=2160]",
    "1440": "bv*[height<=1440]+ba/b[height<=1440]/b[height<=1440]",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b[height<=1080]",
    "720":  "bv*[height<=720]+ba/b[height<=720]/b[height<=720]",
    "480":  "bv*[height<=480]+ba/b[height<=480]/b[height<=480]",
    "audio": "ba/b",
}

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")


# ============================= CORS ======================================
@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.before_request
def _auth():
    if request.method == "OPTIONS":
        return ("", 204)
    if not API_KEY:
        return None
    # allow the UI itself (same-origin) and any request carrying the key
    if request.path == "/" or request.path.startswith("/static"):
        return None
    key = request.headers.get("X-API-Key") or request.args.get("key")
    if key and hmac.compare_digest(key, API_KEY):
        return None
    # allow same-origin browser calls (no Referer header difference trick needed)
    # if you want to lock this down harder, remove the next 3 lines
    if request.headers.get("Sec-Fetch-Site", "") in ("same-origin", "none"):
        return None
    return jsonify({"error": "unauthorized"}), 401


# ============================= ffmpeg ====================================
def resolve_ffmpeg() -> str | None:
    env = os.environ.get("FFMPEG_LOCATION")
    if env and Path(env).exists():
        return env
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p
    candidates: list[str] = []
    if os.name == "nt":
        candidates += [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\ffmpeg\bin\ffmpeg.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin\ffmpeg.exe"),
            os.path.expandvars(r"%USERPROFILE%\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
        ]
    elif sys.platform == "darwin":
        candidates += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    else:
        candidates += [
            "/data/data/com.termux/files/usr/bin/ffmpeg",
            "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg",
            "/snap/bin/ffmpeg", "/usr/lib/ffmpeg/ffmpeg",
        ]
    for pattern in candidates:
        for hit in _glob.glob(pattern):
            if Path(hit).exists():
                return hit
    return None


FFMPEG_PATH = resolve_ffmpeg()


# ============================= helpers ===================================
def human(n) -> str:
    if not n:
        return "—"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def open_path(p: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
            return
        if shutil.which("termux-open"):
            subprocess.Popen(["termux-open", str(p)])
            return
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(p)])
            return
    except Exception:
        pass
    print(f"open: {p}")


def pick_folder_blocking() -> str:
    code = (
        "import tkinter as tk;from tkinter import filedialog;"
        "r=tk.Tk();r.withdraw();r.attributes('-topmost',True);"
        "print(filedialog.askdirectory() or '')"
    )
    try:
        out = subprocess.check_output([sys.executable, "-c", code], text=True, timeout=180)
        return out.strip()
    except Exception:
        return ""


def parse_rate(rate: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)", rate.strip(), re.I)
    if not m:
        raise ValueError(rate)
    n = float(m.group(1))
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    return int(n * mult)


def parse_langs(s: str | None) -> list[str]:
    if not s:
        return ["en", "en-orig"]
    return [x.strip() for x in s.split(",") if x.strip()]


# ============================= job model =================================
@dataclass
class Job:
    id: str
    url: str
    out_dir: str
    quality: str = "best"
    audio_only: bool = False
    playlist: bool = False
    subtitles: bool = False
    thumbnail: bool = False
    metadata: bool = False
    filename_tpl: str = DEFAULT_TPL
    cookies: str | None = None
    rate_limit: str | None = None
    ffmpeg_location: str | None = None
    user_agent: str | None = None
    referer: str | None = None
    format_override: str | None = None
    proxy: str | None = None
    source_address: str | None = None
    languages: str | None = None

    title: str = ""
    uploader: str = ""
    thumbnail_url: str = ""
    duration: int = 0

    status: str = "queued"
    progress: float = 0.0
    speed: float = 0.0
    eta: int = 0
    total_bytes: int = 0
    downloaded: int = 0
    filename: str = ""
    error: str = ""
    attempt: int = 0
    created_at: float = field(default_factory=time.time)
    cancel_flag: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        return {
            "id": self.id, "url": self.url,
            "title": self.title, "uploader": self.uploader,
            "thumbnail": self.thumbnail_url, "duration": self.duration,
            "status": self.status,
            "progress": round(self.progress, 2),
            "speed": self.speed,
            "speed_h": (human(self.speed) + "/s") if self.speed else "—",
            "eta": self.eta,
            "downloaded_h": human(self.downloaded),
            "total_h": human(self.total_bytes),
            "filename": Path(self.filename).name if self.filename else "",
            "filepath": self.filename,
            "error": self.error,
            "out_dir": self.out_dir,
            "created_at": self.created_at,
            "quality": self.quality,
            "audio_only": self.audio_only,
            "attempt": self.attempt,
        }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
WORK_Q: "queue.Queue[str]" = queue.Queue()

CLIENTS: set = set()
CLIENTS_LOCK = threading.Lock()


def broadcast(payload: dict) -> None:
    with CLIENTS_LOCK:
        dead = []
        for q in CLIENTS:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            CLIENTS.discard(q)


def push_job(job: Job) -> None:
    broadcast({"type": "job", "job": job.snapshot()})


# ============================= history ===================================
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text("utf-8"))[:HISTORY_LIMIT]
    except Exception:
        return []


HISTORY = load_history()
HISTORY_LOCK = threading.Lock()


def record_history(job: Job) -> None:
    entry = job.snapshot()
    entry.pop("created_at", None)
    with HISTORY_LOCK:
        HISTORY.insert(0, entry)
        del HISTORY[HISTORY_LIMIT:]
        try:
            HISTORY_FILE.write_text(json.dumps(HISTORY, indent=2), "utf-8")
        except Exception:
            pass
    broadcast({"type": "history", "history": HISTORY})


# ============================= yt-dlp glue ===============================
class Cancelled(Exception):
    pass


def build_opts(job: Job) -> dict:
    if job.format_override:
        fmt = job.format_override
    elif job.audio_only:
        fmt = "ba/b"
    else:
        fmt = QUALITY_MAP.get(job.quality, QUALITY_MAP["best"])

    opts: dict = {
        "format": fmt,
        "outtmpl": str(Path(job.out_dir) / job.filename_tpl),
        "noplaylist": not job.playlist,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": os.name == "nt",
        "progress_hooks": [lambda d, j=job: on_progress(j, d)],
        "postprocessor_hooks": [lambda d, j=job: on_postproc(j, d)],
        "postprocessors": [],
        "http_headers": {
            "User-Agent": job.user_agent or DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "ignoreerrors": False,
        "no_color": True,
        "nocheckcertificate": True,
    }
    if job.referer:
        opts["http_headers"]["Referer"] = job.referer
    if job.proxy:
        opts["proxy"] = job.proxy
    if job.source_address:
        opts["source_address"] = job.source_address

    if job.audio_only:
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        })

    if job.subtitles:
        langs = parse_langs(job.languages)
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": langs,
            "subtitlesformat": "srt/best",
        })

    if job.thumbnail:
        opts["writethumbnail"] = True

    if job.metadata:
        opts["postprocessors"].append({"key": "FFmpegMetadata"})

    if job.cookies:
        opts["cookiefile"] = job.cookies

    if job.rate_limit:
        try:
            opts["ratelimit"] = parse_rate(job.rate_limit)
        except ValueError:
            pass

    ff = job.ffmpeg_location or FFMPEG_PATH
    if ff:
        opts["ffmpeg_location"] = ff
        opts["merge_output_format"] = "mp4"

    return opts


def on_progress(job: Job, d: dict) -> None:
    if job.cancel_flag.is_set():
        raise Cancelled()
    info = d.get("info_dict") or {}
    if info.get("title") and not job.title:
        job.title = info["title"]
    if info.get("uploader") and not job.uploader:
        job.uploader = info["uploader"]
    if info.get("duration") and not job.duration:
        try:
            job.duration = int(info["duration"])
        except Exception:
            pass
    if info.get("thumbnail") and not job.thumbnail_url:
        job.thumbnail_url = info["thumbnail"]

    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        got = d.get("downloaded_bytes", 0)
        job.total_bytes = int(total)
        job.downloaded = int(got)
        job.progress = (got / total * 100) if total else 0.0
        job.speed = float(d.get("speed") or 0)
        job.eta = int(d.get("eta") or 0)
        job.filename = d.get("filename", job.filename)
        job.status = "downloading"
        push_job(job)
    elif d["status"] == "finished":
        job.progress = 99.0
        job.status = "processing"
        push_job(job)


def on_postproc(job: Job, d: dict) -> None:
    if job.cancel_flag.is_set():
        raise Cancelled()
    if d.get("status") == "finished":
        job.progress = 100.0
        push_job(job)


# ============================= worker pool ===============================
def worker() -> None:
    while True:
        job_id = WORK_Q.get()
        try:
            job = JOBS.get(job_id)
            if job and not job.cancel_flag.is_set():
                run_job(job)
        finally:
            WORK_Q.task_done()


for _ in range(MAX_WORKERS):
    threading.Thread(target=worker, daemon=True).start()


def _merge_fail(msg: str) -> bool:
    m = (msg or "").lower()
    return ("ffmpeg" in m and ("not found" in m or "not installed" in m or "unable" in m)) \
        or ("postprocessing" in m and "ffmpeg" in m)


_NET_FAIL_MARKERS = (
    "failed to resolve", "no address associated",
    "temporary failure in name resolution", "getaddrinfo",
    "connection reset", "connection refused", "timed out",
    "read timed out", "unable to download video data",
    "connection aborted", "remote end closed",
)


def _is_net_fail(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in _NET_FAIL_MARKERS)


def _fill_final_info(job: Job, info) -> None:
    if not info:
        return
    if isinstance(info, dict) and info.get("entries"):
        info = info["entries"][0] if info["entries"] else info
    if info.get("title"):
        job.title = info["title"]
    if info.get("uploader"):
        job.uploader = info["uploader"]
    if info.get("thumbnail"):
        job.thumbnail_url = info["thumbnail"]
    rd = info.get("requested_downloads") or []
    if rd and rd[0].get("filepath"):
        job.filename = rd[0]["filepath"]
    elif info.get("_filename"):
        job.filename = info["_filename"]


def run_job(job: Job) -> None:
    attempts = 3
    last_err = ""

    for attempt in range(1, attempts + 1):
        job.attempt = attempt
        if job.cancel_flag.is_set():
            job.status = "cancelled"
            push_job(job)
            return

        if attempt == 1:
            job.status = "downloading"
            push_job(job)
        else:
            job.status = "processing"
            job.progress = 0.0
            broadcast({
                "type": "job",
                "job": job.snapshot(),
                "note": f"retry {attempt}/{attempts}",
            })
            time.sleep(2.5 * (attempt - 1))

        try:
            with yt_dlp.YoutubeDL(build_opts(job)) as ydl:
                info = ydl.extract_info(job.url, download=True)
                _fill_final_info(job, info)
            job.status = "done"
            job.progress = 100.0
            job.speed = 0.0
            job.eta = 0
            push_job(job)
            record_history(job)
            return

        except Cancelled:
            job.status = "cancelled"
            job.speed = 0.0
            push_job(job)
            return

        except Exception as e:
            if job.cancel_flag.is_set():
                job.status = "cancelled"
                push_job(job)
                return

            msg = str(e)
            last_err = msg

            if _merge_fail(msg) and not job.format_override:
                try:
                    fb = build_opts(job)
                    fb["format"] = "b[ext=mp4]/b"
                    fb.pop("merge_output_format", None)
                    with yt_dlp.YoutubeDL(fb) as ydl:
                        info = ydl.extract_info(job.url, download=True)
                        _fill_final_info(job, info)
                    job.status = "done"
                    job.progress = 100.0
                    push_job(job)
                    record_history(job)
                    return
                except Exception as e2:
                    last_err = str(e2)
                    msg = last_err

            if _is_net_fail(msg) and attempt < attempts:
                continue

            break

    job.status = "error"
    job.error = last_err
    push_job(job)
    record_history(job)


# ============================= routes ====================================
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "ffmpeg": FFMPEG_PATH or None,
        "ffmpeg_ok": bool(FFMPEG_PATH),
        "out_dir": str(OUT_DIR),
        "workers": MAX_WORKERS,
        "platform": sys.platform,
        "host": HOST,
        "port": PORT,
        "auth_required": bool(API_KEY),
        "version": "3.0.0",
    })


@app.post("/api/download")
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    urls_raw = (data.get("urls") or data.get("url") or "").strip()
    if not urls_raw:
        return jsonify({"error": "no url"}), 400

    urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
    out_dir = Path(data.get("out_dir") or OUT_DIR).expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        out_dir = OUT_DIR

    created = []
    for url in urls:
        job = Job(
            id=uuid.uuid4().hex[:12],
            url=url,
            out_dir=str(out_dir),
            quality=data.get("quality", "best"),
            audio_only=bool(data.get("audio_only")),
            playlist=bool(data.get("playlist")),
            subtitles=bool(data.get("subtitles")),
            thumbnail=bool(data.get("thumbnail")),
            metadata=bool(data.get("metadata")),
            filename_tpl=data.get("filename_tpl") or DEFAULT_TPL,
            cookies=data.get("cookies") or None,
            rate_limit=data.get("rate_limit") or None,
            ffmpeg_location=data.get("ffmpeg_location") or None,
            user_agent=data.get("user_agent") or None,
            referer=data.get("referer") or None,
            format_override=data.get("format_override") or None,
            proxy=data.get("proxy") or None,
            source_address=data.get("source_address") or None,
            languages=data.get("languages") or None,
        )
        with JOBS_LOCK:
            JOBS[job.id] = job
        WORK_Q.put(job.id)
        created.append(job.id)
        push_job(job)

    return jsonify({"ids": created})


def _ydl_opts_for(data: dict, skip: bool = True) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": skip,
        "noplaylist": True, "no_color": True, "nocheckcertificate": True,
        "socket_timeout": 30,
        "http_headers": {"User-Agent": data.get("user_agent") or DEFAULT_UA,
                         "Accept-Language": "en-US,en;q=0.9"},
    }
    if data.get("cookies"):
        opts["cookiefile"] = data["cookies"]
    if data.get("proxy"):
        opts["proxy"] = data["proxy"]
    if data.get("referer"):
        opts["http_headers"]["Referer"] = data["referer"]
    return opts


@app.post("/api/probe")
def api_probe():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    try:
        with yt_dlp.YoutubeDL(_ydl_opts_for(data)) as ydl:
            info = ydl.extract_info(url, download=False)
        fmts = info.get("formats") or []
        heights = sorted({f.get("height") for f in fmts if f.get("height")}, reverse=True)
        return jsonify({
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "view_count": info.get("view_count"),
            "extractor": info.get("extractor_key") or info.get("extractor"),
            "is_playlist": info.get("_type") == "playlist",
            "entries": info.get("playlist_count") or 1,
            "heights": heights[:6],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/formats")
def api_formats():
    """Return a clean list of playable formats for the online player."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    try:
        opts = _ydl_opts_for(data)
        opts["format"] = "all"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if isinstance(info, dict) and info.get("entries"):
            info = info["entries"][0] if info["entries"] else info

        raw = info.get("formats") or []
        combined = []
        video_only = []
        audio_only = []
        for f in raw:
            if not f.get("url"):
                continue
            vcodec = f.get("vcodec") or "none"
            acodec = f.get("acodec") or "none"
            has_v = vcodec not in ("none", None)
            has_a = acodec not in ("none", None)
            entry = {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "width": f.get("width"),
                "fps": f.get("fps"),
                "tbr": f.get("tbr"),
                "vcodec": vcodec,
                "acodec": acodec,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "protocol": f.get("protocol"),
                "has_audio": has_a,
                "has_video": has_v,
            }
            if has_v and has_a:
                combined.append(entry)
            elif has_v:
                video_only.append(entry)
            elif has_a:
                audio_only.append(entry)

        def score(e):
            return (e.get("height") or 0, e.get("tbr") or 0)

        combined.sort(key=score, reverse=True)
        video_only.sort(key=score, reverse=True)
        audio_only.sort(key=lambda e: e.get("tbr") or 0, reverse=True)

        return jsonify({
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "combined": combined[:20],
            "video_only": video_only[:20],
            "audio_only": audio_only[:10],
            "has_progressive": bool(combined),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _pick_stream(info: dict, format_id: str | None, prefer: str | None):
    formats = info.get("formats") or []
    if format_id:
        for f in formats:
            if f.get("format_id") == format_id and f.get("url"):
                return f
    combined = [f for f in formats if f.get("url")
                and (f.get("vcodec") or "none") not in ("none",)
                and (f.get("acodec") or "none") not in ("none",)]
    if combined:
        return max(combined, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    video = [f for f in formats if f.get("url")
             and (f.get("vcodec") or "none") not in ("none",)]
    if video:
        return max(video, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    any_url = [f for f in formats if f.get("url")]
    if any_url:
        return max(any_url, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return None


@app.post("/api/stream")
def api_stream():
    """Return a direct stream URL for in-browser playback. Optional format_id."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    format_id = (data.get("format_id") or "").strip() or None
    try:
        opts = _ydl_opts_for(data)
        opts["format"] = format_id or "b[ext=mp4]/b[ext=webm]/bv*+ba/b"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if isinstance(info, dict) and info.get("entries"):
            info = info["entries"][0] if info["entries"] else info

        picked = _pick_stream(info, format_id, None)
        stream_url = (picked or {}).get("url") or info.get("url")
        if not stream_url:
            return jsonify({"error": "no playable stream found"}), 502
        return jsonify({
            "url": stream_url,
            "format_id": (picked or {}).get("format_id"),
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "ext": (picked or {}).get("ext") or info.get("ext"),
            "height": (picked or {}).get("height"),
            "http_headers": (picked or {}).get("http_headers") or {},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/cancel/<job_id>")
def api_cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    job.cancel_flag.set()
    return jsonify({"ok": True})


@app.post("/api/retry/<job_id>")
def api_retry(job_id: str):
    old = JOBS.get(job_id)
    if not old:
        return jsonify({"error": "not found"}), 404
    job = Job(
        id=uuid.uuid4().hex[:12],
        url=old.url, out_dir=old.out_dir, quality=old.quality,
        audio_only=old.audio_only, playlist=old.playlist, subtitles=old.subtitles,
        thumbnail=old.thumbnail, metadata=old.metadata, filename_tpl=old.filename_tpl,
        cookies=old.cookies, rate_limit=old.rate_limit,
        ffmpeg_location=old.ffmpeg_location, user_agent=old.user_agent,
        referer=old.referer, format_override=old.format_override,
        proxy=old.proxy, source_address=old.source_address,
        languages=old.languages,
    )
    with JOBS_LOCK:
        JOBS[job.id] = job
    WORK_Q.put(job.id)
    push_job(job)
    return jsonify({"id": job.id})


@app.get("/api/events")
def api_events():
    q: "queue.Queue" = queue.Queue(maxsize=500)
    with CLIENTS_LOCK:
        CLIENTS.add(q)

    def stream():
        try:
            with JOBS_LOCK:
                snap = [j.snapshot() for j in sorted(JOBS.values(), key=lambda x: x.created_at)]
            yield f"data: {json.dumps({'type': 'snapshot', 'jobs': snap, 'history': HISTORY, 'ffmpeg_ok': bool(FFMPEG_PATH)})}\n\n"
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with CLIENTS_LOCK:
                CLIENTS.discard(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.get("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        snap = [j.snapshot() for j in sorted(JOBS.values(), key=lambda x: x.created_at)]
    return jsonify({"jobs": snap, "history": HISTORY, "ffmpeg_ok": bool(FFMPEG_PATH)})


@app.post("/api/history/clear")
def api_history_clear():
    global HISTORY
    with HISTORY_LOCK:
        HISTORY = []
        try:
            HISTORY_FILE.write_text("[]", "utf-8")
        except Exception:
            pass
    broadcast({"type": "history", "history": HISTORY})
    return jsonify({"ok": True})


@app.post("/api/open-folder")
def api_open_folder():
    data = request.get_json(force=True, silent=True) or {}
    p = Path(data.get("path") or OUT_DIR).expanduser()
    try:
        open_path(p if p.exists() else OUT_DIR)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/open-file")
def api_open_file():
    data = request.get_json(force=True, silent=True) or {}
    p = Path(data.get("path") or "")
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        open_path(p)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/reveal-file")
def api_reveal_file():
    data = request.get_json(force=True, silent=True) or {}
    p = Path(data.get("path") or "")
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        elif shutil.which("termux-open"):
            subprocess.Popen(["termux-open", "--content-type", "resource/folder", str(p.parent)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/pick-folder")
def api_pick_folder():
    return jsonify({"path": pick_folder_blocking()})


# ============================= front-end =================================
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Hamster · Video Downloader</title>
<script src="https://cdn.tailwindcss.com?plugins=forms"></script>
<script>
  tailwind.config = {
    theme: {
      extend: {
        colors: {
          ink: { 900:'#05070b', 800:'#0a0e14', 700:'#101620', 600:'#161e2a', 500:'#1e2836' },
          brand: { 300:'#93c5fd', 400:'#6cc4ff', 500:'#3ba9dd', 600:'#2563eb', 700:'#1d4ed8' },
          violet2: '#b07bff',
          ok:'#4ade80', err:'#ff6b81', warn:'#ffcc66'
        },
        fontFamily: { mono: ['ui-monospace','SFMono-Regular','Menlo','Consolas','monospace'] },
        keyframes: {
          shimmer: { '0%': { transform: 'translateX(-100%)' }, '100%': { transform: 'translateX(100%)' } },
          fadeUp: { '0%': { opacity: '0', transform: 'translateY(8px)' }, '100%': { opacity: '1', transform: 'none' } },
        },
        animation: {
          shimmer: 'shimmer 1.6s linear infinite',
          fadeUp: 'fadeUp .35s cubic-bezier(.2,.7,.3,1) both',
        },
      }
    }
  }
</script>
<style>
  html,body{height:100%}
  body{
    background:
      radial-gradient(1200px 700px at 10% -10%, rgba(108,196,255,.18), transparent 60%),
      radial-gradient(900px 600px at 100% 0%, rgba(176,123,255,.15), transparent 55%),
      linear-gradient(180deg,#05070b, #0a0e14);
    background-attachment: fixed;
    color-scheme: dark;
  }
  .glass { background: rgba(20,26,34,.65); backdrop-filter: blur(20px) saturate(140%); -webkit-backdrop-filter: blur(20px) saturate(140%); }
  .glass-2 { background: rgba(16,22,32,.85); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); }
  .ring-gradient { background: conic-gradient(from 180deg, #6cc4ff, #7b8cff, #b07bff, #6cc4ff); }
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:#1e2836;border-radius:6px}
  ::-webkit-scrollbar-thumb:hover{background:#2a3648}
  .thumb-fallback{ background: linear-gradient(135deg, #101620 0%, #1e2836 100%); }

  /* ============================ PRO PLAYER ============================ */
  #playerShell {
    position: fixed; inset: 0; z-index: 200;
    background: #000;
    display: none;
  }
  #playerShell.on { display: block; }
  #playerShell.theater { background: #000; }
  #playerWrap {
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
  }
  #playerTopBar {
    position: absolute; top: 0; left: 0; right: 0;
    padding: 14px 18px;
    display: flex; align-items: center; gap: 12px;
    background: linear-gradient(180deg, rgba(0,0,0,.85), transparent);
    opacity: 0; transition: opacity .25s;
    z-index: 3;
  }
  #playerShell.show-ui #playerTopBar { opacity: 1; }
  #playerTitle {
    flex: 1; min-width: 0;
    font-size: 14px; font-weight: 600; color: #fff;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  #playerBottom {
    position: absolute; left: 0; right: 0; bottom: 0;
    padding: 14px 18px 18px;
    background: linear-gradient(0deg, rgba(0,0,0,.9), transparent);
    opacity: 0; transition: opacity .25s;
    z-index: 3;
  }
  #playerShell.show-ui #playerBottom { opacity: 1; }
  #playerCenter {
    position: absolute; inset: 0;
    display: grid; place-items: center;
    pointer-events: none;
    z-index: 2;
  }
  #bigPlayBtn {
    width: 84px; height: 84px; border-radius: 50%;
    background: rgba(108,196,255,.92); color: #04121c;
    display: grid; place-items: center;
    pointer-events: auto; cursor: pointer; border: 0;
    box-shadow: 0 20px 60px rgba(108,196,255,.5);
    transition: opacity .25s, transform .15s;
    opacity: 0;
  }
  #playerShell.paused.show-ui #bigPlayBtn { opacity: 1; }
  #bigPlayBtn:hover { transform: scale(1.06); }

  #playerVideo {
    position: absolute; inset: 0;
    width: 100%; height: 100%;
    object-fit: contain;
    background: #000;
    z-index: 1;
  }

  .pbtn {
    width: 40px; height: 40px; border-radius: 10px;
    display: grid; place-items: center;
    background: transparent; color: #e6ecf3; border: 0; cursor: pointer;
    transition: background .15s, color .15s;
  }
  .pbtn:hover { background: rgba(255,255,255,.12); color: #6cc4ff; }
  .pbtn svg { width: 20px; height: 20px; }

  .pselect {
    background: rgba(255,255,255,.08); color: #e6ecf3;
    border: 1px solid rgba(255,255,255,.15);
    border-radius: 8px; padding: 6px 22px 6px 10px;
    font-size: 12px; font-family: ui-monospace, monospace;
    appearance: none; cursor: pointer;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23e6ecf3' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 6px center; background-size: 14px;
  }
  .pselect:focus { outline: 2px solid #6cc4ff; }

  #ptimeline {
    position: relative; height: 14px; margin-bottom: 8px;
    cursor: pointer; display: flex; align-items: center;
  }
  #ptrack {
    position: relative; width: 100%; height: 4px;
    background: rgba(255,255,255,.2); border-radius: 4px; overflow: hidden;
    transition: height .15s;
  }
  #ptimeline:hover #ptrack { height: 7px; }
  #pbuffer {
    position: absolute; top: 0; left: 0; height: 100%;
    background: rgba(255,255,255,.32);
  }
  #pprogress {
    position: absolute; top: 0; left: 0; height: 100%;
    background: linear-gradient(90deg, #6cc4ff, #7b8cff);
  }
  #pscrub {
    position: absolute; top: 50%; transform: translate(-50%, -50%);
    width: 14px; height: 14px; border-radius: 50%;
    background: #6cc4ff; box-shadow: 0 0 12px rgba(108,196,255,.7);
    pointer-events: none; opacity: 0; transition: opacity .15s;
  }
  #ptimeline:hover #pscrub, #ptimeline.scrubbing #pscrub { opacity: 1; }

  #ptip {
    position: absolute; bottom: 26px; transform: translateX(-50%);
    background: rgba(0,0,0,.9); color: #e6ecf3;
    padding: 3px 8px; border-radius: 6px;
    font-family: ui-monospace, monospace; font-size: 11px;
    pointer-events: none; opacity: 0; transition: opacity .12s;
    white-space: nowrap;
  }
  #ptimeline:hover #ptip, #ptimeline.scrubbing #ptip { opacity: 1; }

  .pspeed-menu {
    position: absolute; bottom: 100%; right: 0; margin-bottom: 6px;
    background: rgba(10,14,20,.95); border: 1px solid rgba(255,255,255,.15);
    border-radius: 10px; padding: 6px; display: none; flex-direction: column; gap: 2px;
    min-width: 90px; z-index: 5;
  }
  .pspeed-menu.on { display: flex; }
  .pspeed-item {
    padding: 6px 12px; border-radius: 6px; font-size: 12.5px;
    color: #e6ecf3; cursor: pointer; text-align: center; font-family: ui-monospace, monospace;
  }
  .pspeed-item:hover, .pspeed-item.on { background: rgba(108,196,255,.15); color: #6cc4ff; }

  #pvolWrap {
    display: flex; align-items: center; gap: 4px;
  }
  #pvolSlider {
    width: 0; opacity: 0;
    transition: width .2s, opacity .2s;
    -webkit-appearance: none; appearance: none;
    height: 4px; border-radius: 4px;
    background: rgba(255,255,255,.2); outline: none; cursor: pointer;
  }
  #pvolWrap:hover #pvolSlider, #pvolSlider:focus { width: 80px; opacity: 1; }
  #pvolSlider::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 12px; height: 12px; border-radius: 50%;
    background: #6cc4ff; cursor: pointer;
    box-shadow: 0 0 8px rgba(108,196,255,.6);
  }
  #pvolSlider::-moz-range-thumb {
    width: 12px; height: 12px; border-radius: 50%;
    background: #6cc4ff; cursor: pointer; border: 0;
  }
  #ptime {
    font-family: ui-monospace, monospace; font-size: 12px;
    color: #cbd5e1; white-space: nowrap;
  }
  #ptime .cur { color: #fff; font-weight: 600; }
  .spacer { flex: 1; }

  .pv-toast {
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    background: rgba(0,0,0,.75); color: #fff;
    padding: 12px 18px; border-radius: 10px;
    font-size: 15px; font-family: ui-monospace, monospace;
    pointer-events: none; opacity: 0;
    transition: opacity .2s;
    z-index: 4;
  }
  .pv-toast.on { opacity: 1; }
</style>
</head>
<body class="min-h-screen font-sans antialiased selection:bg-brand-400/30">

<header class="sticky top-0 z-30 border-b border-white/5 glass">
  <div class="max-w-6xl mx-auto px-5 sm:px-8 py-3.5 flex items-center gap-4">
    <div class="flex items-center gap-3">
      <div class="relative w-10 h-10 rounded-xl ring-gradient p-[1.5px] shadow-lg shadow-brand-400/20">
        <div class="w-full h-full rounded-[10px] bg-ink-800 grid place-items-center text-brand-400 font-black text-lg">H</div>
      </div>
      <div class="leading-tight">
        <div class="font-semibold tracking-tight">Hamster</div>
        <div class="text-[11px] text-slate-500 font-mono">yt-dlp · pro player</div>
      </div>
    </div>

    <div class="flex-1"></div>

    <div id="ffStatus" class="hidden sm:flex items-center gap-2 text-[11.5px] font-mono text-slate-400 border border-white/10 rounded-full px-3 py-1.5">
      <span id="ffDot" class="w-2 h-2 rounded-full bg-slate-500"></span>
      <span id="ffText">checking…</span>
    </div>

    <button id="openFolder" title="Open downloads folder"
      class="w-10 h-10 grid place-items-center rounded-xl border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4">
        <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
      </svg>
    </button>
  </div>
</header>

<main class="max-w-6xl mx-auto px-5 sm:px-8 pt-8 pb-24">

  <section class="mb-8">
    <div class="relative overflow-hidden rounded-3xl border border-white/10 glass p-6 sm:p-8">
      <div class="absolute -top-24 -right-24 w-72 h-72 rounded-full bg-brand-400/10 blur-3xl pointer-events-none"></div>
      <div class="absolute -bottom-32 -left-24 w-80 h-80 rounded-full bg-violet2/10 blur-3xl pointer-events-none"></div>

      <div class="relative">
        <div class="flex items-center gap-2 mb-1.5">
          <span class="text-[10.5px] font-mono uppercase tracking-[0.18em] text-brand-400/90">New Download</span>
          <span class="h-px flex-1 bg-gradient-to-r from-brand-400/40 to-transparent"></span>
        </div>
        <h1 class="text-2xl sm:text-3xl font-semibold tracking-tight mb-1">Paste. Preview. Download.</h1>
        <p class="text-slate-400 text-sm mb-5">Drop any video URL. Preview, play in a full pro player, or save to disk.</p>

        <div class="relative">
          <textarea id="urls" rows="3" spellcheck="false"
            placeholder="https://…&#10;https://… (one per line)"
            class="w-full rounded-2xl bg-ink-900/70 border border-white/10 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20 outline-none px-4 py-3.5 font-mono text-[13.5px] leading-relaxed placeholder:text-slate-600 transition resize-y min-h-[86px]"></textarea>
          <div class="absolute right-3 bottom-3 flex gap-2">
            <button id="pasteBtn" class="text-[12px] px-2.5 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">Paste</button>
            <button id="clearBtn" class="text-[12px] px-2.5 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:border-err/60 hover:text-err transition">Clear</button>
          </div>
        </div>

        <div id="preview" class="hidden mt-4 rounded-2xl border border-white/10 bg-ink-900/40 overflow-hidden">
          <div class="flex flex-col sm:flex-row gap-4 p-3.5">
            <div class="relative w-full sm:w-56 aspect-video rounded-xl overflow-hidden thumb-fallback flex-shrink-0">
              <img id="pvThumb" alt="" class="w-full h-full object-cover hidden">
              <div id="pvThumbSkel" class="absolute inset-0 animate-pulse bg-white/5"></div>
              <div id="pvDurBadge" class="absolute bottom-1.5 right-1.5 px-1.5 py-0.5 rounded bg-black/80 text-[10.5px] font-mono hidden"></div>
              <button id="pvPlayInline" class="absolute inset-0 grid place-items-center opacity-0 hover:opacity-100 transition bg-black/40 group">
                <span class="w-12 h-12 rounded-full bg-brand-400 text-ink-900 grid place-items-center shadow-lg group-hover:scale-105 transition">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
                </span>
              </button>
            </div>
            <div class="flex-1 min-w-0">
              <div id="pvTitle" class="font-semibold truncate text-[15px] mb-1">—</div>
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-slate-400 font-mono">
                <span id="pvUploader" class="truncate max-w-[220px]"></span>
                <span id="pvExtractor" class="px-1.5 py-0.5 rounded bg-white/5 border border-white/10"></span>
                <span id="pvHeights" class="text-brand-400"></span>
                <span id="pvViews"></span>
              </div>
            </div>
          </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-12 gap-3 mt-5">
          <div class="md:col-span-3">
            <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Quality</label>
            <select id="quality"
              class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm">
              <option value="best">Best available</option>
              <option value="2160">2160p · 4K</option>
              <option value="1440">1440p</option>
              <option value="1080">1080p</option>
              <option value="720">720p</option>
              <option value="480">480p</option>
              <option value="audio">Audio only (mp3)</option>
            </select>
          </div>
          <div class="md:col-span-3">
            <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Subtitle language</label>
            <select id="langs"
              class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm">
              <option value="en,en-orig">English</option>
              <option value="ta">Tamil (ta)</option>
              <option value="ta,en">Tamil + English</option>
              <option value="hi">Hindi (hi)</option>
              <option value="hi,en">Hindi + English</option>
              <option value="te">Telugu (te)</option>
              <option value="ml">Malayalam (ml)</option>
              <option value="kn">Kannada (kn)</option>
              <option value="bn">Bengali (bn)</option>
              <option value="mr">Marathi (mr)</option>
              <option value="ur">Urdu (ur)</option>
              <option value="ar">Arabic (ar)</option>
              <option value="es">Spanish (es)</option>
              <option value="fr">French (fr)</option>
              <option value="de">German (de)</option>
              <option value="ja">Japanese (ja)</option>
              <option value="ko">Korean (ko)</option>
              <option value="zh">Chinese (zh)</option>
              <option value="ru">Russian (ru)</option>
            </select>
          </div>
          <div class="md:col-span-6">
            <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Options</label>
            <div class="flex flex-wrap gap-2">
              <button class="chip" data-opt="playlist">Playlist</button>
              <button class="chip" data-opt="subtitles">Subtitles</button>
              <button class="chip" data-opt="thumbnail">Thumbnail</button>
              <button class="chip" data-opt="metadata">Metadata</button>
              <button class="chip" data-opt="audio_only">Audio only</button>
            </div>
          </div>
        </div>

        <div class="flex flex-wrap items-center gap-2.5 mt-6">
          <button id="goBtn"
            class="group relative inline-flex items-center gap-2.5 rounded-xl px-5 py-3 font-semibold text-ink-900 bg-gradient-to-br from-brand-400 to-brand-600 hover:brightness-110 active:translate-y-px transition shadow-lg shadow-brand-400/25 disabled:opacity-60 disabled:cursor-not-allowed">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 3v12"/><path d="m7 12 5 5 5-5"/><path d="M5 21h14"/>
            </svg>
            <span id="goLabel">Download</span>
          </button>

          <button id="playBtn" disabled
            class="inline-flex items-center gap-2.5 rounded-xl px-5 py-3 font-medium border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition disabled:opacity-40 disabled:cursor-not-allowed">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
            Play online
          </button>

          <div class="flex-1"></div>

          <details class="relative">
            <summary class="list-none inline-flex items-center gap-2 rounded-xl px-4 py-3 cursor-pointer border border-white/10 hover:border-white/20 transition text-sm text-slate-300">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              Advanced
            </summary>
            <div class="absolute right-0 mt-2 w-[min(92vw,640px)] rounded-2xl border border-white/10 glass-2 p-5 shadow-2xl z-40">
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-3.5">
                <div class="sm:col-span-2">
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Output folder</label>
                  <div class="flex gap-2">
                    <input id="outDir" type="text" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                    <button id="browseBtn" class="px-3 py-2.5 rounded-xl border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition text-sm">Browse</button>
                  </div>
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Filename template</label>
                  <input id="tpl" type="text" value="%(title)s.%(ext)s" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Rate limit</label>
                  <input id="rate" type="text" placeholder="e.g. 2M" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Cookies file</label>
                  <input id="cookies" type="text" placeholder="/path/cookies.txt" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Proxy</label>
                  <input id="proxy" type="text" placeholder="socks5://127.0.0.1:1080" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Referer</label>
                  <input id="referer" type="text" placeholder="https://example.com/" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">User-Agent</label>
                  <input id="ua" type="text" placeholder="default Chrome UA" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">ffmpeg path</label>
                  <input id="ffmpegLoc" type="text" placeholder="auto-detected" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div>
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Source IP</label>
                  <input id="srcAddr" type="text" placeholder="e.g. 192.168.1.5" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div class="sm:col-span-2">
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">Format override</label>
                  <input id="fmtOverride" type="text" placeholder="e.g. bestvideo+bestaudio/best" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
                <div class="sm:col-span-2">
                  <label class="block text-[10.5px] font-mono uppercase tracking-[0.15em] text-slate-500 mb-1.5">API key (if server requires)</label>
                  <input id="apiKey" type="text" placeholder="X-API-Key value" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
                </div>
              </div>
            </div>
          </details>
        </div>
      </div>
    </div>
  </section>

  <section class="mb-10">
    <div class="flex items-center gap-3 mb-4">
      <h2 class="text-[13px] font-mono uppercase tracking-[0.18em] text-slate-400">Active</h2>
      <span id="activeCount" class="text-[11px] font-mono text-slate-600"></span>
      <div class="flex-1"></div>
      <button id="clearDone" class="text-[12.5px] px-3 py-1.5 rounded-lg border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">Clear finished</button>
    </div>
    <div id="activeList" class="grid gap-3">
      <div class="rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-500 text-sm">Nothing downloading — paste a link above.</div>
    </div>
  </section>

  <section>
    <div class="flex items-center gap-3 mb-4">
      <h2 class="text-[13px] font-mono uppercase tracking-[0.18em] text-slate-400">History</h2>
      <span id="histCount" class="text-[11px] font-mono text-slate-600"></span>
      <div class="flex-1"></div>
      <button id="clearHistory" class="text-[12.5px] px-3 py-1.5 rounded-lg border border-white/10 hover:border-err/60 hover:text-err transition">Clear history</button>
    </div>
    <div id="historyList" class="grid gap-3">
      <div class="rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-500 text-sm">No history yet.</div>
    </div>
  </section>

  <section class="mt-12">
    <details class="rounded-2xl border border-white/10 glass p-5">
      <summary class="cursor-pointer text-[13px] font-mono uppercase tracking-[0.18em] text-slate-400">REST API</summary>
      <div class="mt-4 text-[12.5px] font-mono text-slate-400 grid gap-2">
        <div><span class="text-brand-400">GET</span>  /api/health</div>
        <div><span class="text-brand-400">POST</span> /api/probe         · {"url":"…"}</div>
        <div><span class="text-brand-400">POST</span> /api/formats       · {"url":"…"} → list of formats</div>
        <div><span class="text-brand-400">POST</span> /api/stream        · {"url":"…","format_id":"137"} → direct URL</div>
        <div><span class="text-brand-400">POST</span> /api/download      · {"urls":"…","quality":"1080"}</div>
        <div><span class="text-brand-400">GET</span>  /api/jobs</div>
        <div><span class="text-brand-400">GET</span>  /api/events        · SSE stream</div>
        <div><span class="text-brand-400">POST</span> /api/cancel/&lt;id&gt;</div>
        <div><span class="text-brand-400">POST</span> /api/retry/&lt;id&gt;</div>
        <div><span class="text-brand-400">POST</span> /api/history/clear</div>
        <div><span class="text-brand-400">POST</span> /api/open-folder   · {"path":"…"}</div>
        <div><span class="text-brand-400">POST</span> /api/open-file     · {"path":"…"}</div>
        <div><span class="text-brand-400">POST</span> /api/reveal-file   · {"path":"…"}</div>
        <div class="text-slate-500">Header <span class="text-brand-400">X-API-Key: &lt;key&gt;</span> required if HAMSTER_API_KEY is set.</div>
      </div>
    </details>
  </section>
</main>

<footer class="text-center text-[11px] font-mono text-slate-600 pb-8">
  <span id="serverInfo">…</span>
</footer>

<div id="toasts" class="fixed bottom-5 right-5 z-[300] flex flex-col gap-2.5 max-w-[360px]"></div>

<!-- download overlay -->
<div id="dlOverlay" class="fixed inset-0 z-[250] hidden items-center justify-center p-6 bg-black/70 backdrop-blur-xl">
  <div class="w-full max-w-[440px] rounded-3xl border border-white/10 glass-2 p-7 text-center shadow-2xl">
    <div class="relative w-[120px] h-[120px] mx-auto mb-5">
      <svg viewBox="0 0 120 120" class="w-full h-full -rotate-90">
        <defs>
          <linearGradient id="dlGrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#6cc4ff"/>
            <stop offset="55%" stop-color="#7b8cff"/>
            <stop offset="100%" stop-color="#b07bff"/>
          </linearGradient>
        </defs>
        <circle cx="60" cy="60" r="52" fill="none" stroke="rgba(255,255,255,.08)" stroke-width="8"/>
        <circle id="dlArc" cx="60" cy="60" r="52" fill="none" stroke="url(#dlGrad)" stroke-width="8" stroke-linecap="round"
                stroke-dasharray="326.72" stroke-dashoffset="326.72" style="transition:stroke-dashoffset .35s ease"/>
      </svg>
      <div id="dlPct" class="absolute inset-0 grid place-items-center font-mono font-bold text-2xl">0%</div>
    </div>
    <div id="dlTitle" class="font-semibold text-[15px] mb-1 truncate">Preparing…</div>
    <div id="dlSub" class="text-slate-400 text-[12.5px] font-mono mb-5">resolving stream</div>
    <button id="dlCancel" class="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl border border-err/40 text-err hover:bg-err/10 transition text-sm font-medium">
      Cancel download
    </button>
  </div>
</div>

<!-- ====================== PRO PLAYER ====================== -->
<div id="playerShell">
  <div id="playerWrap">
    <video id="playerVideo" playsinline preload="metadata" crossorigin="anonymous"></video>

    <div id="playerTopBar">
      <button id="pclose" class="pbtn" title="Close (Esc)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
      <div id="playerTitle">Stream</div>
      <button id="preload" class="pbtn" title="Reload stream">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
      </button>
    </div>

    <div id="playerCenter">
      <button id="bigPlayBtn" title="Play / Pause (Space)">
        <svg viewBox="0 0 24 24" fill="currentColor" width="36" height="36"><path d="M8 5v14l11-7z"/></svg>
      </button>
    </div>

    <div id="playerBottom">
      <div id="ptimeline">
        <div id="ptrack">
          <div id="pbuffer" style="width:0%"></div>
          <div id="pprogress" style="width:0%"></div>
        </div>
        <div id="pscrub" style="left:0%"></div>
        <div id="ptip">0:00</div>
      </div>

      <div class="flex items-center gap-1.5">
        <button id="pskipBack" class="pbtn" title="Back 10s (←)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11 17l-5-5 5-5"/><path d="M18 17l-5-5 5-5"/>
          </svg>
        </button>
        <button id="pplay" class="pbtn" title="Play / Pause (Space)">
          <svg id="pplayIcon" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        </button>
        <button id="pskipFwd" class="pbtn" title="Forward 10s (→)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M13 17l5-5-5-5"/><path d="M6 17l5-5-5-5"/>
          </svg>
        </button>

        <div id="pvolWrap">
          <button id="pmute" class="pbtn" title="Mute (M)">
            <svg id="pvolIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>
            </svg>
          </button>
          <input id="pvolSlider" type="range" min="0" max="1" step="0.01" value="1" title="Volume">
        </div>

        <div id="ptime"><span class="cur">0:00</span> / <span id="pdura">0:00</span></div>

        <div class="spacer"></div>

        <select id="pquality" class="pselect" title="Quality"></select>
        <div style="position:relative">
          <button id="pspeedBtn" class="pbtn" title="Playback speed" style="width:auto; padding:0 10px; font-size:12px; font-family:ui-monospace,monospace;">
            <span id="pspeedVal">1.0×</span>
          </button>
          <div id="pspeedMenu" class="pspeed-menu">
            <div class="pspeed-item" data-sp="0.5">0.5×</div>
            <div class="pspeed-item" data-sp="0.75">0.75×</div>
            <div class="pspeed-item on" data-sp="1">1.0×</div>
            <div class="pspeed-item" data-sp="1.25">1.25×</div>
            <div class="pspeed-item" data-sp="1.5">1.5×</div>
            <div class="pspeed-item" data-sp="2">2.0×</div>
          </div>
        </div>
        <button id="ppip" class="pbtn" title="Picture in picture">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="5" width="18" height="14" rx="2"/><rect x="12" y="12" width="7" height="5"/>
          </svg>
        </button>
        <button id="pfull" class="pbtn" title="Fullscreen (F)">
          <svg id="pfullIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 4h6M4 4v6"/><path d="M20 4h-6M20 4v6"/><path d="M4 20h6M4 20v-6"/><path d="M20 20h-6M20 20v-6"/>
          </svg>
        </button>
      </div>
    </div>

    <div id="pvToast" class="pv-toast"></div>
  </div>
</div>

<script>
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
const state = { jobs: new Map(), history: [] };
const opts = { playlist:false, subtitles:false, thumbnail:false, metadata:false, audio_only:false };
let activePreview = null;
let overlayJobId = null;
let serverAuthRequired = false;

/* ===== helpers ===== */
const fmtDur = (s) => {
  if (!s && s !== 0) return '';
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${m}:${String(sec).padStart(2,'0')}`;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const firstUrl = () => $('#urls').value.trim().split('\n').map(s => s.trim()).filter(Boolean)[0] || '';

function apiHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const k = $('#apiKey')?.value.trim();
  if (k) h['X-API-Key'] = k;
  return h;
}
async function api(path, body) {
  const init = { method: 'POST', headers: apiHeaders() };
  if (body !== undefined) init.body = JSON.stringify(body);
  return fetch(path, init);
}

function toast(msg, kind='') {
  const el = document.createElement('div');
  const color = kind === 'ok' ? 'border-l-ok' : kind === 'err' ? 'border-l-err' : 'border-l-brand-400';
  el.className = `glass-2 border border-white/10 border-l-2 ${color} rounded-xl px-4 py-3 text-[13px] shadow-xl`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; }, 3400);
  setTimeout(() => el.remove(), 3800);
}

/* ===== chips ===== */
$$('.chip').forEach(c => {
  const update = () => {
    const on = c.dataset.on === '1';
    c.className = 'chip px-3.5 py-2 rounded-full text-[12.5px] font-medium border transition ' +
      (on ? 'border-brand-400/60 text-brand-400 bg-brand-400/10'
          : 'border-white/10 text-slate-400 hover:border-white/25 hover:text-slate-200');
  };
  update();
  c.onclick = () => {
    const k = c.dataset.opt;
    opts[k] = !opts[k];
    c.dataset.on = opts[k] ? '1' : '0';
    update();
  };
});

/* ===== paste/clear ===== */
$('#pasteBtn').onclick = async () => {
  try {
    const t = await navigator.clipboard.readText();
    if (t) { $('#urls').value = t.trim(); probe(); }
  } catch { toast('Clipboard blocked', 'err'); }
};
$('#clearBtn').onclick = () => { $('#urls').value = ''; hidePreview(); };

/* ===== probe ===== */
let probeTimer = null;
$('#urls').addEventListener('input', () => {
  clearTimeout(probeTimer);
  probeTimer = setTimeout(probe, 550);
});
function hidePreview(){
  $('#preview').classList.add('hidden');
  $('#playBtn').disabled = true;
  activePreview = null;
}
function showPreviewLoading(){
  $('#preview').classList.remove('hidden');
  $('#pvThumb').classList.add('hidden');
  $('#pvThumbSkel').classList.remove('hidden');
  $('#pvDurBadge').classList.add('hidden');
  $('#pvTitle').textContent = 'Loading…';
  ['#pvUploader','#pvExtractor','#pvHeights','#pvViews'].forEach(s => $(s).textContent = '');
  $('#playBtn').disabled = true;
}
async function probe(){
  const u = firstUrl();
  if (!u) return hidePreview();
  showPreviewLoading();
  try {
    const r = await api('/api/probe', {
      url: u,
      cookies: $('#cookies').value.trim() || null,
      user_agent: $('#ua').value.trim() || null,
      proxy: $('#proxy').value.trim() || null,
      referer: $('#referer').value.trim() || null,
    });
    const d = await r.json();
    if (d.error) { hidePreview(); return; }
    if (d.thumbnail) {
      $('#pvThumb').src = d.thumbnail;
      $('#pvThumb').classList.remove('hidden');
      $('#pvThumbSkel').classList.add('hidden');
    } else $('#pvThumbSkel').classList.add('hidden');
    if (d.duration) { $('#pvDurBadge').textContent = fmtDur(d.duration); $('#pvDurBadge').classList.remove('hidden'); }
    $('#pvTitle').textContent = d.title || '—';
    $('#pvUploader').textContent = d.uploader || '';
    $('#pvExtractor').textContent = d.extractor || '';
    $('#pvHeights').textContent = (d.heights && d.heights.length) ? ('≤' + d.heights[0] + 'p') : '';
    $('#pvViews').textContent = d.view_count ? (Intl.NumberFormat().format(d.view_count) + ' views') : '';
    $('#playBtn').disabled = false;
    activePreview = { url: u, title: d.title };
  } catch { hidePreview(); }
}

/* ===== browse ===== */
$('#browseBtn').onclick = async () => {
  try {
    const r = await api('/api/pick-folder');
    const d = await r.json();
    if (d.path) $('#outDir').value = d.path;
  } catch { toast('Folder picker unavailable', 'err'); }
};

/* ===== download overlay ===== */
function openOverlay(title){
  $('#dlTitle').textContent = title || 'Downloading…';
  $('#dlSub').textContent = 'starting…';
  setArc(0);
  const el = $('#dlOverlay');
  el.classList.remove('hidden'); el.classList.add('flex');
  overlayJobId = null;
}
function closeOverlay(){
  const el = $('#dlOverlay');
  el.classList.add('hidden'); el.classList.remove('flex');
  overlayJobId = null;
}
function setArc(pct){
  pct = Math.max(0, Math.min(100, pct || 0));
  const c = 2 * Math.PI * 52;
  $('#dlArc').setAttribute('stroke-dashoffset', String(c * (1 - pct/100)));
  $('#dlPct').textContent = pct.toFixed(0) + '%';
}
$('#dlCancel').onclick = async () => {
  if (overlayJobId) { await api(`/api/cancel/${overlayJobId}`); toast('Cancelling…'); }
  else closeOverlay();
};

/* ===== download ===== */
$('#goBtn').onclick = async () => {
  const urls = $('#urls').value.trim();
  if (!urls) { $('#urls').focus(); return; }
  const btn = $('#goBtn'), label = $('#goLabel');
  btn.disabled = true;
  const original = label.textContent;
  label.textContent = 'Starting…';

  if (!activePreview || activePreview.url !== firstUrl()) await probe();
  const title = (activePreview && activePreview.title) || firstUrl();
  openOverlay(title);

  const body = {
    urls,
    quality: $('#quality').value,
    languages: $('#langs').value,
    out_dir: $('#outDir').value.trim() || null,
    filename_tpl: $('#tpl').value.trim() || null,
    cookies: $('#cookies').value.trim() || null,
    rate_limit: $('#rate').value.trim() || null,
    ffmpeg_location: $('#ffmpegLoc').value.trim() || null,
    referer: $('#referer').value.trim() || null,
    user_agent: $('#ua').value.trim() || null,
    format_override: $('#fmtOverride').value.trim() || null,
    proxy: $('#proxy').value.trim() || null,
    source_address: $('#srcAddr').value.trim() || null,
    ...opts
  };
  try {
    const r = await api('/api/download', body);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    overlayJobId = d.ids[0] || null;
    toast(`Queued ${d.ids.length} download${d.ids.length>1?'s':''}`, 'ok');
  } catch (e) {
    closeOverlay();
    toast('Error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    label.textContent = original;
  }
};

/* ================================================================
   PRO PLAYER
   ================================================================ */
const P = {
  shell: null, video: null, title: null,
  play: null, playIcon: null, bigPlay: null,
  skipBack: null, skipFwd: null,
  muteBtn: null, volIcon: null, volSlider: null,
  timeline: null, progress: null, buffer: null, scrub: null, tip: null,
  timeCur: null, timeDur: null,
  quality: null, speedBtn: null, speedVal: null, speedMenu: null,
  pipBtn: null, fullBtn: null, fullIcon: null, closeBtn: null, reloadBtn: null,
  toastEl: null,
  hideTimer: null, scrubbing: false, qualitiesLoaded: false,
  currentUrl: '', streamFormatId: null, currentTime: 0, wasPlaying: false,
};

function initPlayer() {
  P.shell = $('#playerShell');
  P.video = $('#playerVideo');
  P.title = $('#playerTitle');
  P.play = $('#pplay'); P.playIcon = $('#pplayIcon'); P.bigPlay = $('#bigPlayBtn');
  P.skipBack = $('#pskipBack'); P.skipFwd = $('#pskipFwd');
  P.muteBtn = $('#pmute'); P.volIcon = $('#pvolIcon'); P.volSlider = $('#pvolSlider');
  P.timeline = $('#ptimeline'); P.progress = $('#pprogress'); P.buffer = $('#pbuffer');
  P.scrub = $('#pscrub'); P.tip = $('#ptip');
  P.timeCur = $('#ptime .cur'); P.timeDur = $('#pdura');
  P.quality = $('#pquality');
  P.speedBtn = $('#pspeedBtn'); P.speedVal = $('#pspeedVal'); P.speedMenu = $('#pspeedMenu');
  P.pipBtn = $('#ppip'); P.fullBtn = $('#pfull'); P.fullIcon = $('#pfullIcon');
  P.closeBtn = $('#pclose'); P.reloadBtn = $('#preload');
  P.toastEl = $('#pvToast');

  P.play.onclick = () => togglePlay();
  P.bigPlay.onclick = () => { togglePlay(); };
  P.skipBack.onclick = () => skip(-10);
  P.skipFwd.onclick = () => skip(10);

  P.video.addEventListener('click', e => {
    if (P.video.paused) togglePlay();
    else if (!P.shell.classList.contains('show-ui')) showUI();
    else P.video.pause();
  });
  P.video.addEventListener('dblclick', e => { e.preventDefault(); toggleFullscreen(); });

  P.video.addEventListener('play', () => { P.shell.classList.remove('paused'); updatePlayIcon(); });
  P.video.addEventListener('pause', () => { P.shell.classList.add('paused'); updatePlayIcon(); });
  P.video.addEventListener('timeupdate', updateProgress);
  P.video.addEventListener('progress', updateBuffer);
  P.video.addEventListener('durationchange', () => { P.timeDur.textContent = fmtDur(P.video.duration); });
  P.video.addEventListener('volumechange', updateVolIcon);
  P.video.addEventListener('waiting', () => showToast('Buffering…'));
  P.video.addEventListener('playing', () => hideToast());
  P.video.addEventListener('error', () => showToast('Playback error'));

  P.volSlider.addEventListener('input', () => {
    P.video.volume = parseFloat(P.volSlider.value);
    P.video.muted = false;
  });
  P.muteBtn.onclick = () => { P.video.muted = !P.video.muted; };

  // timeline
  const setFromX = (clientX) => {
    const r = P.timeline.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    if (P.video.duration) P.video.currentTime = pct * P.video.duration;
    return pct;
  };
  P.timeline.addEventListener('pointerdown', e => {
    P.scrubbing = true;
    P.timeline.classList.add('scrubbing');
    P.timeline.setPointerCapture(e.pointerId);
    setFromX(e.clientX);
    showUI(true);
  });
  P.timeline.addEventListener('pointermove', e => {
    const r = P.timeline.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    P.tip.style.left = (pct * 100) + '%';
    P.tip.textContent = fmtDur(pct * (P.video.duration || 0));
    if (P.scrubbing) setFromX(e.clientX);
  });
  P.timeline.addEventListener('pointerup', e => {
    P.scrubbing = false;
    P.timeline.classList.remove('scrubbing');
    try { P.timeline.releasePointerCapture(e.pointerId); } catch {}
  });

  // speed menu
  P.speedBtn.onclick = (e) => { e.stopPropagation(); P.speedMenu.classList.toggle('on'); };
  P.speedMenu.querySelectorAll('.pspeed-item').forEach(it => {
    it.onclick = () => {
      const sp = parseFloat(it.dataset.sp);
      P.video.playbackRate = sp;
      P.speedVal.textContent = sp.toFixed(sp < 1 ? 2 : 1) + '×';
      P.speedMenu.querySelectorAll('.pspeed-item').forEach(x => x.classList.remove('on'));
      it.classList.add('on');
      P.speedMenu.classList.remove('on');
    };
  });
  document.addEventListener('click', () => P.speedMenu.classList.remove('on'));

  // quality
  P.quality.onchange = () => switchQuality(P.quality.value);

  // PiP
  P.pipBtn.onclick = async () => {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await P.video.requestPictureInPicture();
    } catch (e) { showToast('PiP unavailable'); }
  };

  // fullscreen
  P.fullBtn.onclick = toggleFullscreen;
  document.addEventListener('fullscreenchange', () => {
    const fs = !!document.fullscreenElement;
    P.fullIcon.innerHTML = fs
      ? '<path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M16 3v3a2 2 0 0 0 2 2h3"/><path d="M8 21v-3a2 2 0 0 0-2-2H3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/>'
      : '<path d="M4 4h6M4 4v6"/><path d="M20 4h-6M20 4v6"/><path d="M4 20h6M4 20v-6"/><path d="M20 20h-6M20 20v-6"/>';
  });

  // close
  P.closeBtn.onclick = closePlayer;
  P.reloadBtn.onclick = () => { if (P.currentUrl) playOnline(P.currentUrl, true); };

  // mouse show/hide
  P.shell.addEventListener('mousemove', () => showUI());
  P.shell.addEventListener('touchstart', () => showUI(), { passive: true });

  // keyboard
  document.addEventListener('keydown', onPlayerKey);
}

function onPlayerKey(e) {
  if (!P.shell.classList.contains('on')) return;
  if (e.target.matches('input, select, textarea')) return;
  const k = e.key.toLowerCase();
  if (k === ' ' || k === 'k') { e.preventDefault(); togglePlay(); }
  else if (k === 'arrowright') { e.preventDefault(); skip(10); }
  else if (k === 'arrowleft') { e.preventDefault(); skip(-10); }
  else if (k === 'j') skip(-10);
  else if (k === 'l') skip(10);
  else if (k === 'arrowup') { e.preventDefault(); setVol(P.video.volume + 0.05); }
  else if (k === 'arrowdown') { e.preventDefault(); setVol(P.video.volume - 0.05); }
  else if (k === 'm') P.video.muted = !P.video.muted;
  else if (k === 'f') toggleFullscreen();
  else if (k === 'escape' && !document.fullscreenElement) closePlayer();
  else if (k === 'p') { try { P.video.requestPictureInPicture(); } catch {} }
  else if (/^[0-9]$/.test(k) && P.video.duration) {
    P.video.currentTime = P.video.duration * (parseInt(k, 10) / 10);
  }
}

function updatePlayIcon() {
  const playing = !P.video.paused;
  P.playIcon.innerHTML = playing
    ? '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>'
    : '<path d="M8 5v14l11-7z"/>';
  P.bigPlay.innerHTML = playing
    ? '<svg viewBox="0 0 24 24" fill="currentColor" width="36" height="36"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="currentColor" width="36" height="36"><path d="M8 5v14l11-7z"/></svg>';
}

function togglePlay() {
  if (P.video.paused) P.video.play().catch(()=>{});
  else P.video.pause();
}

function skip(sec) {
  if (!P.video.duration) return;
  P.video.currentTime = Math.max(0, Math.min(P.video.duration, P.video.currentTime + sec));
  showToast(sec > 0 ? `+${sec}s` : `${sec}s`);
}

function setVol(v) {
  v = Math.max(0, Math.min(1, v));
  P.video.volume = v;
  P.volSlider.value = v;
  P.video.muted = false;
}

function updateProgress() {
  if (!P.video.duration) return;
  const pct = (P.video.currentTime / P.video.duration) * 100;
  P.progress.style.width = pct + '%';
  P.scrub.style.left = pct + '%';
  P.timeCur.textContent = fmtDur(P.video.currentTime);
}

function updateBuffer() {
  if (!P.video.duration || !P.video.buffered.length) return;
  const b = P.video.buffered.end(P.video.buffered.length - 1);
  P.buffer.style.width = ((b / P.video.duration) * 100) + '%';
}

function updateVolIcon() {
  const v = P.video.muted ? 0 : P.video.volume;
  P.volSlider.value = v;
  if (v === 0) P.volIcon.innerHTML = '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M22 9l-6 6M16 9l6 6"/>';
  else if (v < 0.5) P.volIcon.innerHTML = '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>';
  else P.volIcon.innerHTML = '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/>';
}

function showToast(msg) {
  P.toastEl.textContent = msg;
  P.toastEl.classList.add('on');
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => P.toastEl.classList.remove('on'), 900);
}
function hideToast() { P.toastEl.classList.remove('on'); }

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    (P.shell.requestFullscreen?.() || P.shell.webkitRequestFullscreen?.() || Promise.resolve()).catch(()=>{});
  } else {
    (document.exitFullscreen?.() || document.webkitExitFullscreen?.() || Promise.resolve()).catch(()=>{});
  }
}

function showUI(sticky) {
  P.shell.classList.add('show-ui');
  clearTimeout(P.hideTimer);
  if (!sticky) {
    P.hideTimer = setTimeout(() => {
      if (!P.video.paused && !P.scrubbing) P.shell.classList.remove('show-ui');
    }, 2800);
  }
}

function closePlayer() {
  try { P.video.pause(); } catch {}
  P.video.removeAttribute('src');
  try { P.video.load(); } catch {}
  P.shell.classList.remove('on', 'paused', 'show-ui');
  if (document.fullscreenElement) document.exitFullscreen?.().catch(()=>{});
  if (document.pictureInPictureElement) document.exitPictureInPicture?.().catch(()=>{});
}

async function loadQualities(url) {
  P.quality.innerHTML = '<option value="">Auto</option>';
  P.quality.disabled = true;
  try {
    const r = await api('/api/formats', {
      url,
      cookies: $('#cookies').value.trim() || null,
      user_agent: $('#ua').value.trim() || null,
      proxy: $('#proxy').value.trim() || null,
      referer: $('#referer').value.trim() || null,
    });
    const d = await r.json();
    if (d.error) return;
    const list = [...(d.combined || [])];
    // append the best of video-only as "needs ffmpeg" hints (not playable directly)
    const seen = new Set();
    const options = [];
    for (const f of list) {
      const h = f.height || 0;
      if (seen.has(h)) continue;
      seen.add(h);
      options.push({ value: f.format_id, label: `${h}p${f.fps ? ' ' + Math.round(f.fps) + 'fps' : ''} · ${f.ext}${f.filesize ? ' · ' + humanSize(f.filesize) : ''}` });
    }
    options.sort((a,b) => parseInt(b.label) - parseInt(a.label));
    for (const o of options) {
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.label;
      P.quality.appendChild(opt);
    }
    if (options.length) P.quality.value = options[0].value;
  } catch (e) { /* ignore */ }
  finally { P.quality.disabled = false; }
}

function humanSize(n) {
  if (!n) return '';
  const units = ['B','KB','MB','GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + units[i];
}

async function playOnline(url, reload) {
  if (!url) url = firstUrl();
  if (!url) return;
  P.currentUrl = url;
  P.streamFormatId = null;

  P.shell.classList.add('on', 'paused', 'show-ui');
  P.title.textContent = (activePreview && activePreview.title) || 'Stream';
  P.quality.innerHTML = '<option value="">Auto</option>';
  P.quality.disabled = true;
  showToast('Resolving…');

  if (!P.qualitiesLoaded || reload) {
    P.qualitiesLoaded = true;
    loadQualities(url); // non-blocking
  }

  try {
    const r = await api('/api/stream', {
      url,
      cookies: $('#cookies').value.trim() || null,
      user_agent: $('#ua').value.trim() || null,
      proxy: $('#proxy').value.trim() || null,
      referer: $('#referer').value.trim() || null,
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    P.video.src = d.url;
    if (d.thumbnail) P.video.poster = d.thumbnail;
    P.streamFormatId = d.format_id;
    P.video.play().catch(()=>{});
    showUI();
    hideToast();
  } catch (e) {
    showToast('Cannot play: ' + e.message);
    setTimeout(() => closePlayer(), 2400);
  }
}

async function switchQuality(formatId) {
  if (!formatId || !P.currentUrl) return;
  const t = P.video.currentTime;
  const wasPlaying = !P.video.paused;
  showToast('Switching quality…');
  try {
    const r = await api('/api/stream', {
      url: P.currentUrl,
      format_id: formatId,
      cookies: $('#cookies').value.trim() || null,
      user_agent: $('#ua').value.trim() || null,
      proxy: $('#proxy').value.trim() || null,
      referer: $('#referer').value.trim() || null,
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    P.video.src = d.url;
    P.streamFormatId = d.format_id;
    const onLoaded = () => {
      P.video.currentTime = t;
      if (wasPlaying) P.video.play().catch(()=>{});
      P.video.removeEventListener('loadedmetadata', onLoaded);
    };
    P.video.addEventListener('loadedmetadata', onLoaded);
    hideToast();
  } catch (e) { showToast('Switch failed'); }
}

/* ===== SSE ===== */
function setFfmpegStatus(ok){
  const dot = $('#ffDot'), txt = $('#ffText');
  if (ok) { dot.className = 'w-2 h-2 rounded-full bg-ok shadow-[0_0_8px_#4ade80]'; txt.textContent = 'ffmpeg ready'; }
  else { dot.className = 'w-2 h-2 rounded-full bg-err shadow-[0_0_8px_#ff6b81]'; txt.textContent = 'ffmpeg missing'; }
}

function connect(){
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'snapshot') {
      setFfmpegStatus(!!msg.ffmpeg_ok);
      state.jobs.clear();
      msg.jobs.forEach(j => state.jobs.set(j.id, j));
      state.history = msg.history || [];
      renderAll();
    } else if (msg.type === 'job') {
      const prev = state.jobs.get(msg.job.id);
      state.jobs.set(msg.job.id, msg.job);
      renderJob(msg.job);
      if (msg.note) toast(msg.note);
      if (msg.job.status === 'done' && (!prev || prev.status !== 'done'))
        toast('✓ ' + (msg.job.title || 'Download complete'), 'ok');
      if (msg.job.status === 'error' && (!prev || prev.status !== 'error'))
        toast('✗ ' + (msg.job.title || 'Download failed'), 'err');
      if (overlayJobId === msg.job.id) {
        setArc(msg.job.progress);
        $('#dlTitle').textContent = msg.job.title || msg.job.filename || 'Downloading…';
        $('#dlSub').textContent = `${msg.job.downloaded_h} / ${msg.job.total_h} · ${msg.job.speed_h}` +
          (msg.job.eta ? ` · ETA ${msg.job.eta}s` : '');
        if (msg.job.status === 'done') { setArc(100); setTimeout(closeOverlay, 700); }
        if (msg.job.status === 'error') {
          $('#dlSub').textContent = 'error — ' + (msg.job.error || '').slice(0,80);
          setTimeout(closeOverlay, 1600);
        }
        if (msg.job.status === 'cancelled') {
          $('#dlSub').textContent = 'cancelled'; setTimeout(closeOverlay, 700);
        }
      }
    } else if (msg.type === 'history') {
      state.history = msg.history;
      renderHistory();
    }
  };
  es.onerror = () => { es.close(); setTimeout(connect, 2000); };
}

/* ===== rendering ===== */
function renderAll(){ renderActive(); renderHistory(); }

function renderActive(){
  const list = $('#activeList');
  const active = [...state.jobs.values()]
    .filter(j => ['queued','downloading','processing'].includes(j.status))
    .sort((a,b) => a.created_at - b.created_at);
  $('#activeCount').textContent = active.length ? `· ${active.length}` : '';
  if (!active.length) {
    list.innerHTML = '<div class="rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-500 text-sm">Nothing downloading — paste a link above.</div>';
    return;
  }
  list.innerHTML = '';
  active.forEach(j => list.appendChild(jobEl(j, false)));
}

function renderJob(j){
  const list = $('#activeList');
  const existing = list.querySelector(`[data-id="${j.id}"]`);
  if (['queued','downloading','processing'].includes(j.status)) {
    const el = jobEl(j, false);
    if (existing) existing.replaceWith(el);
    else { list.querySelector('div.text-center')?.remove(); list.appendChild(el); }
  } else {
    if (existing) existing.remove();
    renderActive();
  }
}

function jobEl(j, isHistory){
  const el = document.createElement('div');
  el.className = 'rounded-2xl border border-white/10 glass p-4';
  el.dataset.id = j.id;
  const pct = Math.min(100, Math.max(0, j.progress || 0));
  const statusColors = {
    downloading: 'text-brand-400 border-brand-400/40 bg-brand-400/10',
    processing: 'text-warn border-warn/40 bg-warn/10',
    done: 'text-ok border-ok/40 bg-ok/10',
    error: 'text-err border-err/40 bg-err/10',
    cancelled: 'text-slate-500 border-white/10',
    queued: 'text-slate-400 border-white/10',
  };
  const thumb = j.thumbnail
    ? `<img src="${j.thumbnail}" alt="" class="w-24 h-14 rounded-lg object-cover bg-black border border-white/10 flex-shrink-0" onerror="this.style.display='none'">`
    : `<div class="w-24 h-14 rounded-lg thumb-fallback border border-white/10 flex-shrink-0"></div>`;

  const actions = [];
  if (!isHistory) {
    if (['queued','downloading','processing'].includes(j.status))
      actions.push(btn('cancel', 'Cancel'));
    if (['error','cancelled'].includes(j.status))
      actions.push(btn('retry', 'Retry'));
  }
  if (j.filepath) {
    actions.push(btn('reveal', 'Show in folder'));
    actions.push(btn('open', 'Open file'));
  }
  const attemptTag = (j.attempt && j.attempt > 1) ? ` <span class="text-warn">· retry ${j.attempt}/3</span>` : '';

  el.innerHTML = `
    <div class="flex gap-4 items-start">
      ${thumb}
      <div class="flex-1 min-w-0">
        <div class="flex items-start gap-3">
          <div class="flex-1 min-w-0">
            <div class="font-medium text-[14px] truncate">${esc(j.title || j.filename || 'Resolving…')}</div>
            <div class="text-[12px] text-slate-500 truncate mt-0.5 font-mono">${esc(j.uploader || j.url || '')}${attemptTag}</div>
          </div>
          <span class="text-[10.5px] font-mono uppercase tracking-wider px-2 py-1 rounded-md border flex-shrink-0 ${statusColors[j.status] || statusColors.queued}">${j.status}</span>
        </div>
        <div class="mt-3 h-2 rounded-full bg-white/5 border border-white/10 overflow-hidden relative">
          <div class="h-full rounded-full transition-all duration-300 ${j.status === 'done' ? 'bg-ok' : j.status === 'error' ? 'bg-err' : ''}"
               style="width:${pct}%; ${['downloading','processing','queued'].includes(j.status) ? 'background:linear-gradient(90deg,#6cc4ff,#7b8cff,#b07bff);background-size:200% 100%' : ''}"></div>
        </div>
        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2.5 text-[11.5px] font-mono text-slate-400">
          <span class="text-slate-200 font-semibold">${pct.toFixed(1)}%</span>
          <span>${j.downloaded_h} / ${j.total_h}</span>
          <span>${j.speed_h}</span>
          ${j.eta ? `<span>ETA ${j.eta}s</span>` : ''}
          ${j.duration ? `<span>${fmtDur(j.duration)}</span>` : ''}
        </div>
        ${actions.length ? `<div class="flex flex-wrap gap-2 mt-3">${actions.join('')}</div>` : ''}
        ${j.status === 'error' && j.error ? `<div class="mt-2 text-err text-[11.5px] font-mono break-all">${esc(j.error)}</div>` : ''}
      </div>
    </div>`;

  el.querySelectorAll('[data-act]').forEach(b => b.onclick = () => {
    const act = b.dataset.act;
    if (act === 'cancel') api(`/api/cancel/${j.id}`);
    if (act === 'retry') api(`/api/retry/${j.id}`);
    if (act === 'open') api('/api/open-file', {path:j.filepath});
    if (act === 'reveal') api('/api/reveal-file', {path:j.filepath});
  });
  return el;
}

function btn(act, label){
  return `<button data-act="${act}" class="text-[12px] px-3 py-1.5 rounded-lg border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">${label}</button>`;
}

function renderHistory(){
  const list = $('#historyList');
  $('#histCount').textContent = state.history.length ? `· ${state.history.length}` : '';
  if (!state.history.length) {
    list.innerHTML = '<div class="rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-500 text-sm">No history yet.</div>';
    return;
  }
  list.innerHTML = '';
  state.history.slice(0, 50).forEach(j => list.appendChild(jobEl(j, true)));
}

/* ===== header ===== */
$('#openFolder').onclick = () => api('/api/open-folder', {path: $('#outDir').value.trim() || null});
$('#clearDone').onclick = () => {
  for (const [id, j] of state.jobs)
    if (['done','error','cancelled'].includes(j.status)) state.jobs.delete(id);
  renderActive();
};
$('#clearHistory').onclick = async () => {
  await api('/api/history/clear');
  state.history = []; renderHistory();
};

/* ===== play buttons ===== */
$('#playBtn').onclick = () => playOnline(firstUrl());
$('#pvPlayInline').onclick = () => playOnline(firstUrl());

/* ===== boot ===== */
(async function boot(){
  initPlayer();
  try {
    const r = await fetch('/api/health');
    const d = await r.json();
    setFfmpegStatus(d.ffmpeg_ok);
    serverAuthRequired = !!d.auth_required;
    if (d.out_dir) {
      if (!$('#outDir').value) $('#outDir').value = d.out_dir;
    }
    $('#serverInfo').textContent = `${d.host}:${d.port} · v${d.version} · ${d.platform}`;
  } catch {}
  connect();
})();
</script>
</body>
</html>
"""


# ============================= bootstrap =================================
def open_browser(url: str) -> None:
    if not IS_LOCAL:
        return
    candidates = []
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "termux-open-url"):
            p = shutil.which(name)
            if p: candidates.append(p)
    for path in candidates:
        if path and Path(path).exists():
            try:
                subprocess.Popen([path, url]); return
            except Exception:
                pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    display_host = "127.0.0.1" if HOST in ("0.0.0.0", "") else HOST
    url = f"http://{display_host}:{PORT}/"
    print(f"\n  Hamster v3.0.0")
    print(f"  Bind:   {HOST}:{PORT}")
    print(f"  Output: {OUT_DIR}")
    print(f"  ffmpeg: {FFMPEG_PATH or 'NOT FOUND — pip install imageio-ffmpeg'}")
    print(f"  Auth:   {'ON (X-API-Key required)' if API_KEY else 'off'}\n")
    if IS_LOCAL:
        threading.Timer(1.2, lambda: open_browser(url)).start()
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()