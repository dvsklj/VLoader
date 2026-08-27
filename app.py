from __future__ import annotations

import glob
import os
import secrets
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
except ImportError:  # Older/minimal yt-dlp installations do not expose impersonation.
    ImpersonateTarget = None


BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

OUTPUT_FORMATS = {"auto", "mp4", "mkv", "webm", "mov"}
MINIMUM_YTDLP_VERSION = (2026, 8, 19)
MEDIA_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ogg", ".ogv", ".ts", ".webm",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("VLOADER_SECRET_KEY", secrets.token_hex(32))
socketio = SocketIO(app, async_mode="threading")

active_downloads: dict[str, dict] = {}
download_history: list[dict] = []
state_lock = threading.Lock()


def validate_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http:// or https:// video page URL.")
    return url


@lru_cache(maxsize=1)
def get_impersonation_target():
    """Return a supported Chrome target, or None when curl-cffi is unavailable."""
    if ImpersonateTarget is None:
        return None

    try:
        target = ImpersonateTarget.from_str("chrome")
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as probe:
            checker = getattr(probe, "_impersonate_target_available", None)
            return target if checker and checker(target) else None
    except Exception:
        app.logger.debug("Browser impersonation is unavailable", exc_info=True)
        return None


def extract_options() -> dict:
    """Options shared by metadata inspection and downloads."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    if target := get_impersonation_target():
        # When curl-cffi is installed, use a consistent browser identity for both
        # page and media requests. Unsupported installations simply use yt-dlp's
        # normal networking stack.
        options["impersonate"] = target
        options["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}
    return options


def first_video(info: dict | None) -> dict:
    """Return the first resolved video when an extractor yields a page/playlist."""
    if not info:
        raise ValueError("No downloadable video was found on this page.")

    if info.get("_type") in {"playlist", "multi_video"}:
        for entry in info.get("entries") or []:
            if entry:
                return entry
        raise ValueError("No downloadable video was found on this page.")
    return info


def quality_label(height: int) -> str:
    names = {4320: "8K", 2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD"}
    name = names.get(height)
    return f"{height}p ({name})" if name else f"{height}p"


def yt_dlp_version_tuple() -> tuple[int, ...]:
    version = getattr(yt_dlp.version, "__version__", "0")
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def ensure_supported_yt_dlp() -> None:
    if yt_dlp_version_tuple() < MINIMUM_YTDLP_VERSION:
        installed = getattr(yt_dlp.version, "__version__", "unknown")
        raise RuntimeError(
            f"yt-dlp {installed} is too old for reliable site extraction. Run "
            "`python -m pip install -U -r requirements.txt`, then restart VLoader."
        )


def available_qualities(info: dict) -> list[dict]:
    """Collapse extractor-specific formats into choices users can understand."""
    qualities: dict[int, dict] = {}
    unknown_resolution_formats = []
    for media_format in info.get("formats") or []:
        extension = str(media_format.get("ext") or "").lower()
        if media_format.get("vcodec") == "none":
            continue
        height = media_format.get("height")
        if not isinstance(height, (int, float)) or height <= 0:
            if extension and f".{extension}" in MEDIA_EXTENSIONS:
                unknown_resolution_formats.append(media_format)
            continue

        height = int(height)
        item = qualities.setdefault(
            height,
            {
                "value": str(height),
                "height": height,
                "display_height": 0,
                "fps": 0,
                "containers": set(),
            },
        )
        width = media_format.get("width")
        # Use the shorter edge for familiar labels on portrait videos, while
        # retaining the extractor's height as the actual selector value.
        display_height = int(min(width, height)) if isinstance(width, (int, float)) and width > 0 else height
        item["display_height"] = max(item["display_height"], display_height)
        fps = media_format.get("fps")
        if isinstance(fps, (int, float)):
            item["fps"] = max(item["fps"], int(round(fps)))
        if extension:
            item["containers"].add(extension)

    if not qualities and isinstance(info.get("height"), (int, float)):
        height = int(info["height"])
        qualities[height] = {
            "value": str(height),
            "height": height,
            "fps": int(info.get("fps") or 0),
            "containers": {str(info.get("ext") or "").lower()} - {""},
        }

    if not qualities and unknown_resolution_formats:
        containers = sorted(
            {str(media_format.get("ext")).lower() for media_format in unknown_resolution_formats}
        )
        return [
            {
                "value": "source",
                "height": None,
                "label": "Source quality (resolution not published)",
                "fps": None,
                "containers": containers,
            }
        ]

    result = []
    for height in sorted(qualities, reverse=True):
        item = qualities[height]
        result.append(
            {
                "value": item["value"],
                "height": height,
                "label": quality_label(item["display_height"]),
                "fps": item["fps"] or None,
                "containers": sorted(item["containers"]),
            }
        )
    return result


def select_available_format(info: dict, height: int | None) -> str:
    """Choose concrete extractor-provided IDs, avoiding selector ambiguity."""
    formats = info.get("formats") or []
    video_formats = [
        media_format
        for media_format in formats
        if media_format.get("format_id")
        and media_format.get("vcodec") != "none"
        and f".{str(media_format.get('ext') or '').lower()}" in MEDIA_EXTENSIONS
    ]

    if height is not None:
        exact = [media_format for media_format in video_formats if media_format.get("height") == height]
        capped = [
            media_format
            for media_format in video_formats
            if isinstance(media_format.get("height"), (int, float))
            and media_format["height"] <= height
        ]
        video_formats = exact or capped

    if not video_formats:
        raise ValueError(
            "This extractor returned no playable video formats. Update yt-dlp, "
            "restart VLoader, and check whether the page is DRM-protected or restricted."
        )

    # yt-dlp publishes formats from worst to best after processing.
    video = video_formats[-1]
    video_id = str(video["format_id"])
    if video.get("acodec") not in {None, "none"}:
        return video_id

    audio_formats = [
        media_format
        for media_format in formats
        if media_format.get("format_id")
        and media_format.get("vcodec") == "none"
        and media_format.get("acodec") not in {None, "none"}
    ]
    return f"{video_id}+{audio_formats[-1]['format_id']}" if audio_formats else video_id


def friendly_download_error(error: Exception) -> str:
    message = str(error)
    if "Requested format is not available" in message:
        return (
            "No playable formats were returned for this video. Update the active "
            "environment with `python -m pip install -U -r requirements.txt`, "
            "restart VLoader, and try again."
        )
    return message


def update_active(job_id: str, **changes: object) -> dict:
    with state_lock:
        current = active_downloads.setdefault(job_id, {})
        current.update(changes)
        return dict(current)


def emit_progress(job_id: str, **changes: object) -> None:
    progress = update_active(job_id, **changes)
    socketio.emit("download_progress", {"job_id": job_id, "progress": progress})


def make_progress_hook(job_id: str):
    def progress_hook(data: dict) -> None:
        try:
            info = data.get("info_dict") or {}
            title = info.get("title") or active_downloads.get(job_id, {}).get("title", "Video")
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0
                emit_progress(
                    job_id,
                    title=title,
                    status="Downloading",
                    progress=percent,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=data.get("speed") or 0,
                    eta=data.get("eta"),
                )
            elif data.get("status") == "finished":
                emit_progress(job_id, title=title, status="Processing", progress=100)
        except Exception:
            app.logger.exception("Unable to publish progress for job %s", job_id)

    return progress_hook


def make_postprocessor_hook(job_id: str):
    def postprocessor_hook(data: dict) -> None:
        if data.get("status") in {"started", "processing"}:
            emit_progress(job_id, status="Converting / merging", progress=100)

    return postprocessor_hook


def locate_output_file(ydl: yt_dlp.YoutubeDL, info: dict, output_format: str) -> Path:
    prepared = Path(ydl.prepare_filename(info))
    raw_filepath = info.get("filepath")
    candidates = ([Path(raw_filepath)] if raw_filepath else []) + [prepared]
    if output_format != "auto":
        candidates.insert(0, prepared.with_suffix(f".{output_format}"))

    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() in MEDIA_EXTENSIONS:
            return candidate.resolve()

    pattern = glob.escape(str(prepared.with_suffix(""))) + ".*"
    matches = [
        Path(path)
        for path in glob.glob(pattern)
        if Path(path).is_file() and Path(path).suffix.lower() in MEDIA_EXTENSIONS
    ]
    if not matches:
        raise RuntimeError("The download finished, but the output file could not be located.")
    return max(matches, key=lambda path: path.stat().st_mtime).resolve()


def transcode_for_compatibility(source: Path, output_format: str) -> Path:
    """Create an output with codecs broadly supported by the chosen container."""
    target_format = "mp4" if output_format == "auto" else output_format
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to create a playable video file.")

    destination = source.with_suffix(f".{target_format}")
    temporary = source.with_name(f"{source.stem}.vloader-temp.{target_format}")
    codec_options = {
        "mp4": [
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
        ],
        "mov": [
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
        ],
        "mkv": [
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        ],
        "webm": [
            "-c:v", "libvpx-vp9", "-crf", "28", "-b:v", "0",
            "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "160k",
        ],
    }

    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
        *codec_options[target_format], str(temporary),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        temporary.unlink(missing_ok=True)
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(f"FFmpeg could not create a playable {target_format.upper()} file: {detail[-1] if detail else 'unknown error'}")

    os.replace(temporary, destination)
    if source != destination:
        source.unlink(missing_ok=True)
    return destination.resolve()


def detected_resolution(info: dict) -> int | None:
    resolutions = []
    for media_format in (info.get("requested_formats") or []) + [info]:
        height = media_format.get("height") if isinstance(media_format, dict) else None
        if isinstance(height, (int, float)):
            width = media_format.get("width")
            resolutions.append(int(min(width, height)) if isinstance(width, (int, float)) else int(height))
    return max(resolutions) if resolutions else None


def download_video(job_id: str, url: str, height: int | None, output_format: str) -> None:
    try:
        ensure_supported_yt_dlp()
        emit_progress(job_id, status="Inspecting page", progress=0)
        inspection_options = extract_options()
        with yt_dlp.YoutubeDL(inspection_options) as inspector:
            inspected_info = first_video(inspector.extract_info(url, download=False))
        selected_format = select_available_format(inspected_info, height)
        emit_progress(job_id, title=inspected_info.get("title") or "Video", status="Preparing download")

        options = extract_options()
        options.update(
            {
                "format": selected_format,
                "outtmpl": str(DOWNLOAD_DIR / "%(title).180B [%(id)s].%(ext)s"),
                "progress_hooks": [make_progress_hook(job_id)],
                "postprocessor_hooks": [make_postprocessor_hook(job_id)],
                "concurrent_fragment_downloads": 4,
                # Repeated downloads may leave a postprocessor temp file after an
                # interrupted conversion. Let yt-dlp/FFmpeg replace those safely.
                "overwrites": True,
                "continuedl": True,
                "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
            }
        )

        # MKV is used only as a permissive intermediate for separate streams.
        # A final explicit compatibility encode runs after yt-dlp completes.
        options["merge_output_format"] = "mkv"

        with yt_dlp.YoutubeDL(options) as ydl:
            raw_info = ydl.extract_info(url, download=True)
            info = first_video(raw_info)
            source_path = locate_output_file(ydl, info, "auto")

        emit_progress(job_id, status="Making file playable", progress=100)
        output_path = transcode_for_compatibility(source_path, output_format)

        actual_height = detected_resolution(info) or height
        file_name = output_path.name
        video_info = {
            "id": job_id,
            "source_id": info.get("id"),
            "title": info.get("title") or file_name,
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "download_time": datetime.now(timezone.utc).isoformat(),
            "filename": file_name,
            "download_url": f"/downloads/{quote(file_name)}",
            "resolution": quality_label(actual_height) if actual_height else "Source quality",
            "file_format": output_path.suffix.lstrip(".").upper(),
            "url": url,
        }

        with state_lock:
            download_history.append(video_info)
            active_downloads.pop(job_id, None)
        socketio.emit("download_complete", video_info)
    except Exception as error:
        app.logger.exception("Download job %s failed", job_id)
        with state_lock:
            active_downloads.pop(job_id, None)
        socketio.emit("download_error", {"job_id": job_id, "error": friendly_download_error(error)})


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/formats")
def get_formats():
    try:
        payload = request.get_json(silent=True) or {}
        url = validate_url(payload.get("url"))
        with yt_dlp.YoutubeDL(extract_options()) as ydl:
            info = first_video(ydl.extract_info(url, download=False))

        qualities = available_qualities(info)
        if not qualities:
            raise ValueError("A video was found, but it did not publish selectable resolutions.")
        return jsonify(
            {
                "id": info.get("id"),
                "title": info.get("title") or "Untitled video",
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "qualities": qualities,
            }
        )
    except (ValueError, yt_dlp.utils.DownloadError) as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        app.logger.exception("Format inspection failed")
        return jsonify({"error": str(error)}), 500


@app.post("/api/download")
def start_download():
    try:
        ensure_supported_yt_dlp()
        payload = request.get_json(silent=True) or {}
        url = validate_url(payload.get("url"))
        output_format = str(payload.get("file_format") or "mp4").lower()
        if output_format not in OUTPUT_FORMATS:
            raise ValueError("Unsupported output format.")

        quality = str(payload.get("quality") or "auto").lower()
        height = None
        if quality not in {"auto", "source"}:
            try:
                height = int(quality)
            except ValueError as error:
                raise ValueError("Choose a valid quality or use Auto.") from error
            if not 1 <= height <= 10000:
                raise ValueError("Choose a valid video resolution.")

        job_id = uuid.uuid4().hex
        initial = {
            "title": "Inspecting video…",
            "status": "Queued",
            "progress": 0,
            "url": url,
            "resolution": (
                "auto" if quality == "auto" else "Source quality" if quality == "source" else quality_label(height)
            ),
            "file_format": output_format,
        }
        with state_lock:
            active_downloads[job_id] = initial

        socketio.start_background_task(download_video, job_id, url, height, output_format)
        return jsonify({"status": "success", "job_id": job_id, **initial}), 202
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        app.logger.exception("Unable to start download")
        return jsonify({"error": str(error)}), 500


@app.get("/api/downloads")
def get_downloads():
    with state_lock:
        return jsonify({"active": dict(active_downloads), "history": list(download_history)})


@app.get("/downloads/<path:filename>")
def serve_download(filename: str):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


@app.get("/api/test")
def test_endpoint():
    return jsonify(
        {
            "status": "ok",
            "message": "Server is running",
            "python": sys.executable,
            "yt_dlp": yt_dlp.version.__version__,
            "impersonation": bool(get_impersonation_target()),
        }
    )


if __name__ == "__main__":
    print(f"VLoader Python: {sys.executable}")
    print(f"VLoader yt-dlp: {yt_dlp.version.__version__}")
    ensure_supported_yt_dlp()
    debug_mode = os.environ.get("VLOADER_DEBUG") == "1"
    socketio.run(
        app,
        debug=debug_mode,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
        log_output=debug_mode,
        port=5001,
    )
