# hamster_server.py — local video downloader (Flask + yt-dlp + SSE)
# Android/Termux + desktop. Run: python hamster_server.py
from __future__ import annotations

import glob as _glob
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
HOST, PORT = "127.0.0.1", 7823

def _pick_out_dir() -> Path:
    for cand in (
        Path("/storage/emulated/0/BDSEER"),
        Path("/sdcard/BDSEER"),
        Path.home() / "Downloads" / "Hamster",
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
MAX_WORKERS = 3
HISTORY_LIMIT = 200

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

def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ") or "video"

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
    filename_tpl: str = "%(title)s [%(id)s].%(ext)s"
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
        "ffmpeg": FFMPEG_PATH or None,
        "ffmpeg_ok": bool(FFMPEG_PATH),
        "out_dir": str(OUT_DIR),
        "workers": MAX_WORKERS,
        "platform": sys.platform,
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
            filename_tpl=data.get("filename_tpl") or "%(title)s [%(id)s].%(ext)s",
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

@app.post("/api/probe")
def api_probe():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    try:
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "no_color": True, "nocheckcertificate": True,
            "socket_timeout": 30,
            "http_headers": {"User-Agent": data.get("user_agent") or DEFAULT_UA},
        }
        if data.get("cookies"):
            opts["cookiefile"] = data["cookies"]
        if data.get("proxy"):
            opts["proxy"] = data["proxy"]
        with yt_dlp.YoutubeDL(opts) as ydl:
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

@app.post("/api/stream")
def api_stream():
    """Return a direct stream URL for in-browser playback."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "no url"}), 400
    try:
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "no_color": True, "nocheckcertificate": True,
            "socket_timeout": 30,
            "format": "b[ext=mp4]/b[ext=webm]/bv*+ba/b",
            "http_headers": {"User-Agent": data.get("user_agent") or DEFAULT_UA},
        }
        if data.get("cookies"):
            opts["cookiefile"] = data["cookies"]
        if data.get("proxy"):
            opts["proxy"] = data["proxy"]
        if data.get("referer"):
            opts["http_headers"]["Referer"] = data["referer"]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if isinstance(info, dict) and info.get("entries"):
            info = info["entries"][0] if info["entries"] else info

        formats = info.get("formats") or []
        progressive = [f for f in formats if f.get("vcodec") not in (None, "none")
                       and f.get("acodec") not in (None, "none") and f.get("url")]
        picked = None
        if progressive:
            picked = max(progressive, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        elif formats:
            with_url = [f for f in formats if f.get("url")]
            if with_url:
                picked = max(with_url, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        stream_url = (picked or {}).get("url") or info.get("url")
        if not stream_url:
            return jsonify({"error": "no playable stream found"}), 502
        return jsonify({
            "url": stream_url,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "ext": (picked or {}).get("ext") or info.get("ext"),
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
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
<html lang="en" class="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Hamster · Video Downloader</title>
<script src="https://cdn.tailwindcss.com?plugins=forms"></script>
<script>
  tailwind.config = {
    darkMode: 'class',
    theme: {
      extend: {
        colors: {
          ink: { 900:'#05070b', 800:'#0a0e14', 700:'#101620', 600:'#161e2a', 500:'#1e2836' },
          brand: { 50:'#eff6ff', 300:'#93c5fd', 400:'#6cc4ff', 500:'#3ba9dd', 600:'#2563eb', 700:'#1d4ed8', 900:'#0b1a3a' },
          violet2: '#b07bff',
          ok:'#4ade80', err:'#ff6b81', warn:'#ffcc66'
        },
        fontFamily: { mono: ['ui-monospace','SFMono-Regular','Menlo','Consolas','monospace'] },
        animation: {
          'gradient-pan': 'gradient-pan 8s ease infinite',
          'float-slow': 'float-slow 6s ease-in-out infinite',
          'shimmer': 'shimmer 1.6s linear infinite',
          'fade-up': 'fade-up .35s cubic-bezier(.2,.7,.3,1) both',
        },
        keyframes: {
          'gradient-pan': {
            '0%,100%': { 'background-position': '0% 50%' },
            '50%': { 'background-position': '100% 50%' },
          },
          'float-slow': {
            '0%,100%': { transform: 'translateY(0)' },
            '50%': { transform: 'translateY(-6px)' },
          },
          'shimmer': { '0%': { transform: 'translateX(-100%)' }, '100%': { transform: 'translateX(100%)' } },
          'fade-up': { '0%': { opacity: '0', transform: 'translateY(8px)' }, '100%': { opacity: '1', transform: 'none' } },
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
  }
  .glass { background: rgba(20,26,34,.65); backdrop-filter: blur(20px) saturate(140%); -webkit-backdrop-filter: blur(20px) saturate(140%); }
  .glass-2 { background: rgba(16,22,32,.78); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px); }
  .ring-gradient { background: conic-gradient(from 180deg, #6cc4ff, #7b8cff, #b07bff, #6cc4ff); }
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:#1e2836;border-radius:6px}
  ::-webkit-scrollbar-thumb:hover{background:#2a3648}
  ::-webkit-scrollbar-track{background:transparent}
  .scroll-smooth-area{scroll-behavior:smooth}
  video::-webkit-media-controls-panel { background-image: linear-gradient(transparent, rgba(0,0,0,.7)); }
  .thumb-fallback{
    background: linear-gradient(135deg, #101620 0%, #1e2836 100%);
  }
</style>
</head>
<body class="min-h-screen text-slate-100 font-sans antialiased selection:bg-brand-400/30">

<!-- ===================== TOP NAV ===================== -->
<header class="sticky top-0 z-30 border-b border-white/5 glass">
  <div class="max-w-6xl mx-auto px-5 sm:px-8 py-3.5 flex items-center gap-4">
    <div class="flex items-center gap-3">
      <div class="relative w-10 h-10 rounded-xl ring-gradient p-[1.5px] shadow-lg shadow-brand-400/20">
        <div class="w-full h-full rounded-[10px] bg-ink-800 grid place-items-center text-brand-400 font-black text-lg">H</div>
      </div>
      <div class="leading-tight">
        <div class="font-semibold tracking-tight">Hamster</div>
        <div class="text-[11px] text-slate-500 font-mono">local · yt-dlp backend</div>
      </div>
    </div>

    <div class="flex-1"></div>

    <div id="ffStatus" class="hidden sm:flex items-center gap-2 text-[11.5px] font-mono text-slate-400 border border-white/10 rounded-full px-3 py-1.5">
      <span id="ffDot" class="w-2 h-2 rounded-full bg-slate-500"></span>
      <span id="ffText">checking…</span>
    </div>

    <button id="openFolder" title="Open downloads folder"
      class="w-10 h-10 grid place-items-center rounded-xl border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4.5 h-4.5">
        <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
      </svg>
    </button>
    <button id="themeBtn" title="Toggle theme"
      class="w-10 h-10 grid place-items-center rounded-xl border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4.5 h-4.5">
        <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>
      </svg>
    </button>
  </div>
</header>

<!-- ===================== HERO / INPUT ===================== -->
<main class="max-w-6xl mx-auto px-5 sm:px-8 pt-8 pb-24">

  <section class="mb-8 animate-fade-up">
    <div class="relative overflow-hidden rounded-3xl border border-white/10 glass p-6 sm:p-8">
      <div class="absolute -top-24 -right-24 w-72 h-72 rounded-full bg-brand-400/10 blur-3xl pointer-events-none"></div>
      <div class="absolute -bottom-32 -left-24 w-80 h-80 rounded-full bg-violet2/10 blur-3xl pointer-events-none"></div>

      <div class="relative">
        <div class="flex items-center gap-2 mb-1.5">
          <span class="text-[10.5px] font-mono uppercase tracking-[0.18em] text-brand-400/90">New Download</span>
          <span class="h-px flex-1 bg-gradient-to-r from-brand-400/40 to-transparent"></span>
        </div>
        <h1 class="text-2xl sm:text-3xl font-semibold tracking-tight mb-1">Paste. Preview. Download.</h1>
        <p class="text-slate-400 text-sm mb-5">Drop any video or playlist URL. Preview before you commit, play it inline, or save to disk.</p>

        <div class="relative">
          <textarea id="urls" rows="3" spellcheck="false"
            placeholder="https://…&#10;https://… (one per line for batch)"
            class="w-full rounded-2xl bg-ink-900/70 border border-white/10 focus:border-brand-400 focus:ring-2 focus:ring-brand-400/20 outline-none px-4 py-3.5 font-mono text-[13.5px] leading-relaxed placeholder:text-slate-600 transition resize-y min-h-[86px]"></textarea>
          <div class="absolute right-3 bottom-3 flex gap-2">
            <button id="pasteBtn" class="text-[12px] px-2.5 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">Paste</button>
            <button id="clearBtn" class="text-[12px] px-2.5 py-1.5 rounded-lg bg-white/5 border border-white/10 hover:border-err/60 hover:text-err transition">Clear</button>
          </div>
        </div>

        <!-- preview card -->
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

        <!-- config row -->
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

        <!-- actions -->
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
                  <input id="tpl" type="text" value="%(title)s [%(id)s].%(ext)s" class="w-full rounded-xl bg-ink-900/70 border border-white/10 focus:border-brand-400 outline-none px-3 py-2.5 text-sm font-mono">
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
              </div>
            </div>
          </details>
        </div>
      </div>
    </div>
  </section>

  <!-- ===================== ACTIVE ===================== -->
  <section class="mb-10">
    <div class="flex items-center gap-3 mb-4">
      <h2 class="text-[13px] font-mono uppercase tracking-[0.18em] text-slate-400">Active</h2>
      <span id="activeCount" class="text-[11px] font-mono text-slate-600"></span>
      <div class="flex-1"></div>
      <button id="clearDone" class="text-[12.5px] px-3 py-1.5 rounded-lg border border-white/10 hover:border-brand-400/60 hover:text-brand-400 transition">Clear finished</button>
    </div>
    <div id="activeList" class="grid gap-3">
      <div class="empty-state rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-500 text-sm">
        Nothing downloading — paste a link above.
      </div>
    </div>
  </section>

  <!-- ===================== HISTORY ===================== -->
  <section>
    <div class="flex items-center gap-3 mb-4">
      <h2 class="text-[13px] font-mono uppercase tracking-[0.18em] text-slate-400">History</h2>
      <span id="histCount" class="text-[11px] font-mono text-slate-600"></span>
      <div class="flex-1"></div>
      <button id="clearHistory" class="text-[12.5px] px-3 py-1.5 rounded-lg border border-white/10 hover:border-err/60 hover:text-err transition">Clear history</button>
    </div>
    <div id="historyList" class="grid gap-3">
      <div class="empty-state rounded-2xl border border-dashed border-white/10 py-12 text-center text-slate-500 text-sm">
        No history yet.
      </div>
    </div>
  </section>
</main>

<footer class="text-center text-[11px] font-mono text-slate-600 pb-8">
  127.0.0.1 · local only · <span id="outPathFoot">…</span>
</footer>

<!-- ===================== TOASTS ===================== -->
<div id="toasts" class="fixed bottom-5 right-5 z-[100] flex flex-col gap-2.5 max-w-[360px]"></div>

<!-- ===================== DOWNLOAD OVERLAY ===================== -->
<div id="dlOverlay" class="fixed inset-0 z-[90] hidden items-center justify-center p-6 bg-black/70 backdrop-blur-xl">
  <div class="w-full max-w-[440px] rounded-3xl border border-white/10 glass-2 p-7 text-center shadow-2xl animate-fade-up">
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
    <div class="flex justify-center gap-1.5 mb-5">
      <span class="w-2 h-2 rounded-full bg-brand-400 animate-pulse"></span>
      <span class="w-2 h-2 rounded-full bg-brand-400 animate-pulse" style="animation-delay:.15s"></span>
      <span class="w-2 h-2 rounded-full bg-brand-400 animate-pulse" style="animation-delay:.30s"></span>
    </div>
    <button id="dlCancel" class="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl border border-err/40 text-err hover:bg-err/10 transition text-sm font-medium">
      Cancel download
    </button>
  </div>
</div>

<!-- ===================== PLAY MODAL ===================== -->
<div id="playModal" class="fixed inset-0 z-[95] hidden items-center justify-center p-4 bg-black/85 backdrop-blur">
  <div class="w-full max-w-[980px] rounded-2xl overflow-hidden border border-white/10 bg-black shadow-2xl">
    <div class="flex items-center gap-3 px-4 py-3 bg-ink-800 border-b border-white/10">
      <div id="playTitle" class="flex-1 text-[13.5px] font-semibold truncate">Stream</div>
      <button id="playClose" class="w-8 h-8 grid place-items-center rounded-lg border border-white/10 hover:border-err/60 hover:text-err transition">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>
    <video id="playVideo" controls playsinline preload="metadata" class="w-full max-h-[78vh] bg-black"></video>
    <div id="playErr" class="hidden px-6 py-5 text-center text-err text-[13px] font-mono"></div>
  </div>
</div>

<script>
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
const state = { jobs: new Map(), history: [] };
const opts = { playlist:false, subtitles:false, thumbnail:false, metadata:false, audio_only:false };
let activePreview = null;
let overlayJobId = null;

/* ---------- theme ---------- */
const savedTheme = localStorage.getItem('hamster-theme') || 'dark';
document.documentElement.classList.toggle('dark', savedTheme === 'dark');
$('#themeBtn').onclick = () => {
  const dark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('hamster-theme', dark ? 'dark' : 'light');
};

/* ---------- toasts ---------- */
function toast(msg, kind='') {
  const el = document.createElement('div');
  const color = kind === 'ok' ? 'border-l-ok' : kind === 'err' ? 'border-l-err' : 'border-l-brand-400';
  el.className = `glass-2 border border-white/10 border-l-2 ${color} rounded-xl px-4 py-3 text-[13px] shadow-xl animate-fade-up`;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; }, 3400);
  setTimeout(() => el.remove(), 3800);
}

/* ---------- helpers ---------- */
const fmtDur = (s) => {
  if (!s) return '';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}` : `${m}:${String(sec).padStart(2,'0')}`;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const firstUrl = () => $('#urls').value.trim().split('\n').map(s => s.trim()).filter(Boolean)[0] || '';

/* ---------- chips ---------- */
$$('.chip').forEach(c => {
  c.className = 'chip px-3.5 py-2 rounded-full text-[12.5px] font-medium border transition ' +
    (c.dataset.on === '1'
      ? 'border-brand-400/60 text-brand-400 bg-brand-400/10'
      : 'border-white/10 text-slate-400 hover:border-white/25 hover:text-slate-200');
  c.onclick = () => {
    const k = c.dataset.opt;
    opts[k] = !opts[k];
    c.dataset.on = opts[k] ? '1' : '0';
    c.className = 'chip px-3.5 py-2 rounded-full text-[12.5px] font-medium border transition ' +
      (opts[k]
        ? 'border-brand-400/60 text-brand-400 bg-brand-400/10'
        : 'border-white/10 text-slate-400 hover:border-white/25 hover:text-slate-200');
  };
});

/* ---------- paste / clear ---------- */
$('#pasteBtn').onclick = async () => {
  try {
    const t = await navigator.clipboard.readText();
    if (t) { $('#urls').value = t.trim(); probe(); }
  } catch { toast('Clipboard blocked — paste manually', 'err'); }
};
$('#clearBtn').onclick = () => { $('#urls').value = ''; hidePreview(); };

/* ---------- preview / probe ---------- */
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
  const p = $('#preview');
  p.classList.remove('hidden');
  $('#pvThumb').classList.add('hidden');
  $('#pvThumbSkel').classList.remove('hidden');
  $('#pvDurBadge').classList.add('hidden');
  $('#pvTitle').textContent = 'Loading…';
  $('#pvUploader').textContent = '';
  $('#pvExtractor').textContent = '';
  $('#pvHeights').textContent = '';
  $('#pvViews').textContent = '';
  $('#playBtn').disabled = true;
}

async function probe(){
  const u = firstUrl();
  if (!u) return hidePreview();
  showPreviewLoading();
  try {
    const r = await fetch('/api/probe', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        url: u,
        cookies: $('#cookies').value.trim() || null,
        user_agent: $('#ua').value.trim() || null,
        proxy: $('#proxy').value.trim() || null,
      })
    });
    const d = await r.json();
    if (d.error) { hidePreview(); return; }
    if (d.thumbnail) {
      $('#pvThumb').src = d.thumbnail;
      $('#pvThumb').classList.remove('hidden');
      $('#pvThumbSkel').classList.add('hidden');
    } else {
      $('#pvThumbSkel').classList.add('hidden');
    }
    if (d.duration) { $('#pvDurBadge').textContent = fmtDur(d.duration); $('#pvDurBadge').classList.remove('hidden'); }
    $('#pvTitle').textContent = d.title || '—';
    $('#pvUploader').textContent = d.uploader || '';
    $('#pvExtractor').textContent = d.extractor || '';
    $('#pvHeights').textContent = (d.heights && d.heights.length) ? ('≤' + d.heights[0] + 'p') : '';
    $('#pvViews').textContent = d.view_count ? (Intl.NumberFormat().format(d.view_count) + ' views') : '';
    $('#playBtn').disabled = false;
    activePreview = { url: u, title: d.title };
  } catch {
    hidePreview();
  }
}

/* ---------- browse folder ---------- */
$('#browseBtn').onclick = async () => {
  try {
    const r = await fetch('/api/pick-folder', { method:'POST' });
    const d = await r.json();
    if (d.path) $('#outDir').value = d.path;
  } catch { toast('Folder picker unavailable', 'err'); }
};

/* ---------- download overlay ---------- */
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
  if (overlayJobId) {
    await fetch(`/api/cancel/${overlayJobId}`, {method:'POST'});
    toast('Cancelling…');
  } else {
    closeOverlay();
  }
};

/* ---------- download ---------- */
$('#goBtn').onclick = async () => {
  const urls = $('#urls').value.trim();
  if (!urls) { $('#urls').focus(); return; }
  const btn = $('#goBtn'), label = $('#goLabel');
  btn.disabled = true;
  const original = label.textContent;
  label.textContent = 'Starting…';

  if (!activePreview || activePreview.url !== firstUrl()) {
    await probe();
  }
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
    const r = await fetch('/api/download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
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

/* ---------- online play ---------- */
async function playOnline(){
  const u = firstUrl();
  if (!u) return;
  $('#playErr').classList.add('hidden');
  const v = $('#playVideo');
  v.removeAttribute('src');
  $('#playTitle').textContent = (activePreview && activePreview.title) || 'Stream';
  const m = $('#playModal'); m.classList.remove('hidden'); m.classList.add('flex');
  toast('Resolving stream…');
  try {
    const r = await fetch('/api/stream', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        url: u,
        cookies: $('#cookies').value.trim() || null,
        user_agent: $('#ua').value.trim() || null,
        proxy: $('#proxy').value.trim() || null,
        referer: $('#referer').value.trim() || null,
      })
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    v.src = d.url;
    if (d.thumbnail) v.poster = d.thumbnail;
    v.play().catch(() => {});
  } catch (e) {
    const err = $('#playErr');
    err.textContent = 'Cannot play: ' + e.message;
    err.classList.remove('hidden');
  }
}
$('#playBtn').onclick = playOnline;
$('#pvPlayInline').onclick = playOnline;
$('#playClose').onclick = () => {
  const v = $('#playVideo');
  try { v.pause(); } catch {}
  v.removeAttribute('src'); v.load();
  const m = $('#playModal'); m.classList.add('hidden'); m.classList.remove('flex');
};
$('#playModal').addEventListener('click', e => { if (e.target === $('#playModal')) $('#playClose').click(); });

/* ---------- SSE ---------- */
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

/* ---------- rendering ---------- */
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
  el.className = 'rounded-2xl border border-white/10 glass p-4 animate-fade-up';
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
          ${['downloading','processing'].includes(j.status) ? '<div class="absolute inset-0 pointer-events-none" style="background:linear-gradient(90deg,transparent,rgba(255,255,255,.3),transparent);animation:shimmer 1.6s linear infinite"></div>' : ''}
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
    if (act === 'cancel') fetch(`/api/cancel/${j.id}`, {method:'POST'});
    if (act === 'retry') fetch(`/api/retry/${j.id}`, {method:'POST'});
    if (act === 'open') fetch('/api/open-file', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:j.filepath})});
    if (act === 'reveal') fetch('/api/reveal-file', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:j.filepath})});
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

/* ---------- header actions ---------- */
$('#openFolder').onclick = () => fetch('/api/open-folder', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path: $('#outDir').value.trim() || null})});
$('#clearDone').onclick = () => {
  for (const [id, j] of state.jobs)
    if (['done','error','cancelled'].includes(j.status)) state.jobs.delete(id);
  renderActive();
};
$('#clearHistory').onclick = async () => {
  await fetch('/api/history/clear', {method:'POST'});
  state.history = []; renderHistory();
};

/* ---------- keyboard ---------- */
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); $('#goBtn').click(); }
  if (e.key === 'Escape' && !$('#playModal').classList.contains('hidden')) $('#playClose').click();
});

/* ---------- boot ---------- */
fetch('/api/health').then(r=>r.json()).then(d => {
  setFfmpegStatus(d.ffmpeg_ok);
  if (d.out_dir) {
    if (!$('#outDir').value) $('#outDir').value = d.out_dir;
    $('#outPathFoot').textContent = d.out_dir;
  }
}).catch(()=>{});
connect();
</script>
</body>
</html>
"""

# ============================= bootstrap =================================
def open_browser(url: str) -> None:
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
    url = f"http://{HOST}:{PORT}/"
    print(f"\n  Hamster running at {url}")
    print(f"  Output: {OUT_DIR}")
    print(f"  ffmpeg: {FFMPEG_PATH or 'NOT FOUND — pip install imageio-ffmpeg'}\n")
    threading.Timer(1.2, lambda: open_browser(url)).start()
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()