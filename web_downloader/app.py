"""Flask web app for YouTube → MP4 downloading via yt-dlp + ffmpeg."""

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request, send_file

# ── tool paths ──────────────────────────────────────────────────────────────
YT_DLP_BIN = shutil.which("yt-dlp") or os.path.expanduser(
    "~/Library/Python/3.9/bin/yt-dlp"
)
FFMPEG_BIN = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

BASE_DIR = Path(__file__).parent.parent          # project root
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

QUALITY_MAP = {
    "best":  ("best",  "Best available"),
    "2160":  ("2160",  "4K · 2160p"),
    "1440":  ("1440",  "2K · 1440p"),
    "1080":  ("1080",  "Full HD · 1080p"),
    "720":   ("720",   "HD · 720p"),
    "480":   ("480",   "SD · 480p"),
    "360":   ("360",   "Low · 360p"),
}

# in-memory job store  {job_id: {...}}
jobs = {}  # type: dict
jobs_lock = threading.Lock()

# ── app ─────────────────────────────────────────────────────────────────────
_APP_DIR = Path(__file__).parent.resolve()
(_APP_DIR / "instance").mkdir(exist_ok=True)
app = Flask(
    __name__,
    root_path=str(_APP_DIR),
    template_folder=str(_APP_DIR / "templates"),
    instance_path=str(_APP_DIR / "instance"),
)


# ── helpers ──────────────────────────────────────────────────────────────────
def build_format_selector(height: str) -> str:
    if height == "best":
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio/best[ext=mp4]/best"
        )
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={height}]+bestaudio"
        f"/best[height<={height}][ext=mp4]"
        f"/best[height<={height}]"
        f"/best[ext=mp4]/best"
    )


def parse_progress_line(line: str) -> Optional[dict]:
    """Parse a yt-dlp --newline progress line into a dict, or None."""
    # e.g.: [download]  62.5% of  123.45MiB at    8.30MiB/s ETA 00:12
    m = re.search(
        r"\[download\]\s+([\d.]+)%\s+of\s+([\d.]+\s*\S+)\s+at\s+([\S]+)\s+ETA\s+(\S+)",
        line,
    )
    if m:
        return {
            "type": "progress",
            "percent": float(m.group(1)),
            "total": m.group(2).strip(),
            "speed": m.group(3),
            "eta": m.group(4),
        }
    # Merger / already-downloaded lines → treat as 100%
    if "[Merger]" in line or "has already been downloaded" in line:
        return {"type": "progress", "percent": 99.0, "total": "", "speed": "", "eta": "…"}
    return None


def worker(job_id: str, url: str, height: str) -> None:
    """Background thread: run yt-dlp, push events to a queue."""
    job = jobs[job_id]
    queue: Queue = job["queue"]

    fmt = build_format_selector(height)
    out_template = str(DOWNLOADS_DIR / "%(title)s [%(height)sp].%(ext)s")

    cmd = [
        YT_DLP_BIN,
        "--ffmpeg-location", FFMPEG_BIN,
        "--format", fmt,
        "--merge-output-format", "mp4",
        "--output", out_template,
        "--progress", "--newline",
        "--no-playlist",
        "--extractor-args", "youtube:player_client=ios,web_creator",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with jobs_lock:
            job["proc"] = proc

        filepath = None
        output_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)

            # Capture the destination file path
            m = re.search(r'\[(?:download|Merger)\].*Destination:\s+(.+\.mp4)', line)
            if m:
                filepath = Path(m.group(1).strip())

            parsed = parse_progress_line(line)
            if parsed:
                queue.put(parsed)

        proc.wait()

        if proc.returncode == 0:
            # Discover the file if we missed the destination line
            if filepath is None or not filepath.exists():
                mp4s = sorted(DOWNLOADS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
                filepath = mp4s[-1] if mp4s else None

            with jobs_lock:
                job["status"] = "done"
                job["filepath"] = filepath

            queue.put({
                "type": "done",
                "filename": filepath.name if filepath else "video.mp4",
            })
        else:
            error_lines = [l for l in output_lines if "ERROR" in l or "error" in l.lower()]
            msg = error_lines[-1] if error_lines else (output_lines[-1] if output_lines else "Download failed.")
            with jobs_lock:
                job["status"] = "error"
                job["error"] = msg
            queue.put({"type": "error", "message": msg})

    except Exception as exc:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = str(exc)
        queue.put({"type": "error", "message": str(exc)})
    finally:
        queue.put(None)  # sentinel


# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    tools_ok = os.path.isfile(YT_DLP_BIN) and (
        os.path.isfile(FFMPEG_BIN) or bool(shutil.which("ffmpeg"))
    )
    return render_template("index.html", tools_ok=tools_ok)


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    height = (data.get("quality") or "best").strip()

    if not url:
        return jsonify(error="No URL provided."), 400
    if height not in QUALITY_MAP:
        height = "best"

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "proc": None,
            "status": "running",
            "filepath": None,
            "error": None,
            "queue": Queue(),
        }

    t = threading.Thread(target=worker, args=(job_id, url, height), daemon=True)
    t.start()

    return jsonify(job_id=job_id)


@app.route("/progress/<job_id>")
def progress(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job."), 404

    def generate():
        queue: Queue = job["queue"]
        while True:
            try:
                event = queue.get(timeout=30)
            except Empty:
                yield "data: {\"type\":\"heartbeat\"}\n\n"
                continue

            if event is None:          # sentinel → stream ends
                break

            yield f"data: {json.dumps(event)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job."), 404
    if job["status"] != "done" or not job["filepath"]:
        return jsonify(error="File not ready."), 400

    fp: Path = job["filepath"]
    return send_file(
        fp,
        as_attachment=True,
        download_name=fp.name,
        mimetype="video/mp4",
    )


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id: str):
    job = jobs.get(job_id)
    if job and job.get("proc"):
        job["proc"].kill()
        with jobs_lock:
            job["status"] = "error"
            job["error"] = "Cancelled."
    return jsonify(ok=True)


if __name__ == "__main__":
    print(f"  yt-dlp : {YT_DLP_BIN}")
    print(f"  ffmpeg : {FFMPEG_BIN}")
    print(f"  output : {DOWNLOADS_DIR}")
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, threaded=True, port=port, host="0.0.0.0")
