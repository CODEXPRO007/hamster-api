<div align="center">

# 🐹 Hamster

**A self-hosted video downloader with a built-in pro player.**
Flask · yt-dlp · SSE · Tailwind UI · Render-ready · Termux-friendly.

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.x-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-red)](https://github.com/yt-dlp/yt-dlp)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](#-license)
[![Render](https://img.shields.io/badge/deploy-render-46E3B7?logo=render&logoColor=white)](#-deploy-on-render)

[Features](#-features) · [Quick start](#-quick-start) · [Deploy](#-deploy-on-render) · [API](#-rest-api) · [Screenshots](#-screenshots)

</div>

---

## ✨ Features

|  |  |
|---|---|
| **Download** | Any site yt-dlp supports — YouTube, Vimeo, Twitter/X, Reddit, TikTok, Instagram, and thousands more |
| **Batch queue** | Paste many URLs, they run through a worker pool (default 3 concurrent) |
| **Pro player** | Custom HTML5 player with quality switch, speed, PiP, fullscreen, keyboard shortcuts |
| **Live progress** | Server-Sent Events stream every byte of progress in real time |
| **Formats probe** | Preview title, thumbnail, duration, and available heights before downloading |
| **Subtitles** | 19 languages including Tamil, Hindi, Telugu, Malayalam, Kannada |
| **Audio extraction** | One toggle → MP3 via ffmpeg |
| **Filenames** | Fully templatable (`%(title)s.%(ext)s` by default — no ugly IDs) |
| **Resilience** | Automatic retry on network/DNS errors, ffmpeg-merge fallback to single-stream |
| **History** | Persistent job history, clearable from the UI |
| **Cross-platform** | Windows · macOS · Linux · **Termux / Android** |
| **Render-ready** | Reads `$PORT` and `$HOST` automatically |
| **Optional auth** | Lock the API behind an `X-API-Key` header with one env var |
| **Zero build step** | Single Python file, Tailwind via CDN, no npm, no bundlers |

---

## 🚀 Quick start

### Desktop (Windows / macOS / Linux)

```bash
git clone https://github.com/CODEXPRO007/hamster-api.git
cd hamster-api
pip install -r requirements.txt
python app.py