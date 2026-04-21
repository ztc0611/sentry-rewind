"""Sentry Rewind - Flask web app. HLS streaming, no temp files."""

import mimetypes
import os
import platform
import re
import shutil
import string
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from teslacam import CAMERAS, scan_teslacam


def _find_bin(name: str) -> str:
    """Find a bundled binary (ffmpeg/ffprobe), or fall back to PATH."""
    if getattr(sys, "frozen", False):
        for candidate in [name, name + ".exe"]:
            bundled = Path(sys._MEIPASS) / candidate
            if bundled.exists():
                return str(bundled)
    return shutil.which(name) or name


FFMPEG = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")

# On Windows, prevent ffmpeg/ffprobe from flashing a console window each call
# (the app is built with --windowed, so there's no parent console to inherit).
_NO_WINDOW = 0x08000000 if platform.system() == "Windows" else 0


def find_teslacam() -> Path | None:
    """Auto-detect a TeslaCam folder on any mounted drive. None if no drive."""
    env = os.environ.get("TESLACAM_PATH")
    if env:
        return Path(env)

    system = platform.system()
    candidates = []

    if system == "Darwin":
        volumes = Path("/Volumes")
        if volumes.exists():
            candidates = [v / "TeslaCam" for v in volumes.iterdir() if v.is_dir()]
    elif system == "Windows":
        for letter in string.ascii_uppercase[3:]:  # D-Z
            candidates.append(Path(f"{letter}:\\TeslaCam"))
    else:
        for base in [Path("/media"), Path("/mnt")]:
            if base.exists():
                for d in base.rglob("TeslaCam"):
                    if d.is_dir():
                        return d

    for c in candidates:
        if c.is_dir() and (c / "SentryClips").exists():
            return c

    return None

if getattr(sys, "frozen", False):
    app = Flask(__name__, static_folder=str(Path(sys._MEIPASS) / "static"))
else:
    app = Flask(__name__)

events = []
_events_lock = threading.Lock()
_last_scanned_path: Path | None = None
_duration_cache: dict[str, list[float]] = {}
_duration_lock = threading.Lock()
_active_ffmpeg: set = set()
_ffmpeg_lock = threading.Lock()


def drive_connected() -> bool:
    """Check if a TeslaCam drive is currently accessible."""
    path = find_teslacam()
    return path is not None and path.is_dir() and (path / "SentryClips").exists()


def get_events():
    global events, _last_scanned_path
    path = find_teslacam()
    with _events_lock:
        if path is None or not (path / "SentryClips").exists():
            events = []
            _last_scanned_path = None
            return events
        if path != _last_scanned_path:
            events = []
            _last_scanned_path = path
        if not events:
            events = scan_teslacam(path)
            events.sort(key=lambda e: e.folder_timestamp, reverse=True)
        return events


def _probe_duration(path: Path) -> float:
    """Get duration of an mp4 file via ffprobe."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    try:
        dur = float(result.stdout.strip())
        return dur if dur > 1.0 else 0  # filter out corrupt/empty files
    except ValueError:
        return 0


def _get_durations(ev, camera: str) -> list[float]:
    """Get durations for all segments of a camera in an event. Cached."""
    key = f"{ev.source}_{ev.folder_timestamp}:{camera}"
    with _duration_lock:
        if key in _duration_cache:
            return _duration_cache[key]
    # Probe outside the lock (slow I/O, don't block other threads)
    durations = []
    for seg in ev.segments:
        if camera in seg.cameras:
            durations.append(_probe_duration(seg.cameras[camera]))
        else:
            durations.append(0)
    with _duration_lock:
        _duration_cache[key] = durations
    return durations


@app.after_request
def no_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def api_status():
    path = find_teslacam()
    return jsonify({"connected": drive_connected(), "path": str(path) if path else None})


def _hls_trigger_offset(ev, camera: str = "front") -> float | None:
    """Map the wall-clock trigger time to the correct HLS timeline position.

    Segment filenames give real-world start times, ffprobe gives actual durations.
    The HLS timeline is the concatenation of segment durations with no gaps,
    but real-world time has small gaps between segments. This maps accurately.
    """
    if ev.trigger_offset_seconds is None:
        return None
    if not ev.metadata:
        return None
    try:
        trigger_dt = datetime.fromisoformat(ev.metadata["timestamp"])
    except (KeyError, ValueError):
        return None

    durations = _get_durations(ev, camera)
    hls_pos = 0.0

    for i, seg in enumerate(ev.segments):
        if camera not in seg.cameras or durations[i] <= 0:
            continue
        seg_start = datetime.strptime(seg.timestamp, "%Y-%m-%d_%H-%M-%S")
        seg_end_real = seg_start + timedelta(seconds=durations[i])

        if seg_start <= trigger_dt < seg_end_real:
            # Trigger falls within this segment
            into_seg = (trigger_dt - seg_start).total_seconds()
            return hls_pos + into_seg
        elif trigger_dt < seg_start:
            # Trigger is in a gap before this segment — snap to segment start
            return hls_pos

        hls_pos += durations[i]

    # Trigger is past all segments
    return None


@app.route("/api/events")
def api_events():
    if not drive_connected():
        return jsonify([])
    evts = get_events()
    return jsonify([
        {
            "index": i,
            "source": e.source,
            "timestamp": e.folder_timestamp,
            "display_name": e.display_name,
            "segments": len(e.segments),
            "duration": e.duration_seconds,
            "has_thumb": e.thumb is not None,
            "trigger_offset": e.trigger_offset_seconds,
            "first_segment_ts": e.segments[0].timestamp if e.segments else e.folder_timestamp,
            "cameras": list({cam for seg in e.segments for cam in seg.cameras}),
            "metadata": e.metadata,
        }
        for i, e in enumerate(evts)
    ])


@app.route("/api/events/<int:idx>/hls/<camera>.m3u8")
def api_hls_playlist(idx, camera):
    """Generate an HLS playlist for a camera's segments."""
    if camera not in CAMERAS:
        return "Invalid camera", 404
    ev = get_events()[idx]

    durations = _get_durations(ev, camera)

    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:61", "#EXT-X-MEDIA-SEQUENCE:0"]
    first = True
    for i, seg in enumerate(ev.segments):
        if camera in seg.cameras and durations[i] > 0:
            # Each Tesla dashcam segment is an independent MP4 file with its own
            # timestamps (PTS starting near 0). Without #EXT-X-DISCONTINUITY,
            # hls.js assumes PTS values are continuous across segments and maps
            # later segments to the wrong timeline position when seeking.
            if not first:
                lines.append("#EXT-X-DISCONTINUITY")
            first = False
            lines.append(f"#EXTINF:{durations[i]:.3f},")
            lines.append(f"/api/events/{idx}/hls/{camera}/{i}.ts")
    lines.append("#EXT-X-ENDLIST")

    return Response("\n".join(lines) + "\n", mimetype="application/vnd.apple.mpegurl")


@app.route("/api/events/<int:idx>/hls/<camera>/<int:seg_idx>.ts")
def api_hls_segment(idx, camera, seg_idx):
    """Transmux a single segment to MPEG-TS on the fly."""
    if camera not in CAMERAS:
        return "Invalid camera", 404
    ev = get_events()[idx]
    if seg_idx < 0 or seg_idx >= len(ev.segments):
        return "Invalid segment", 404
    seg = ev.segments[seg_idx]
    if camera not in seg.cameras:
        return "Camera not available", 404

    path = seg.cameras[camera]

    # Reap finished ffmpeg processes lazily.
    with _ffmpeg_lock:
        _active_ffmpeg.difference_update({p for p in _active_ffmpeg if p.poll() is not None})

    proc = subprocess.Popen(
        [FFMPEG, "-i", str(path), "-c", "copy", "-f", "mpegts", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    with _ffmpeg_lock:
        _active_ffmpeg.add(proc)

    def cleanup():
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        with _ffmpeg_lock:
            _active_ffmpeg.discard(proc)

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            cleanup()

    response = Response(generate(), mimetype="video/mp2t")
    # call_on_close fires when the WSGI server closes the response — reliable
    # even if the client disconnects before the generator is garbage-collected.
    response.call_on_close(cleanup)
    return response


@app.route("/api/events/<int:idx>/trigger_offset")
def api_trigger_offset(idx):
    """Get the accurate HLS-mapped trigger offset for an event."""
    ev = get_events()[idx]
    offset = _hls_trigger_offset(ev)
    return jsonify({"offset": offset})


@app.route("/api/events/<int:idx>/open", methods=["POST"])
def api_open_folder(idx):
    """Open the event's folder in the OS file manager."""
    ev = get_events()[idx]
    folder = ev.folder
    system = platform.system()

    if ev.source == "RecentClips" and ev.segments:
        # For drives, select the first file
        first_file = None
        for cam in ["front", "back"]:
            if cam in ev.segments[0].cameras:
                first_file = ev.segments[0].cameras[cam]
                break
        if first_file:
            if system == "Darwin":
                subprocess.Popen(["open", "-R", str(first_file)])
            elif system == "Windows":
                subprocess.Popen(["explorer", "/select,", str(first_file)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
            return jsonify({"ok": True})

    if system == "Darwin":
        subprocess.Popen(["open", str(folder)])
    elif system == "Windows":
        subprocess.Popen(["explorer", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return jsonify({"ok": True})


@app.route("/api/events/<int:idx>/thumb")
def api_thumb(idx):
    ev = get_events()[idx]
    if ev.thumb and ev.thumb.exists():
        return send_file(ev.thumb)
    return "", 404


if __name__ == "__main__":
    path = find_teslacam()
    print(f"Scanning {path}..." if path else "No TeslaCam drive detected.")
    get_events()
    print(f"Found {len(events)} events. Starting server...")
    app.run(host="127.0.0.1", port=5555, debug=False)
