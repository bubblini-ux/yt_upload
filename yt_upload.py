#!/usr/bin/env python3
"""
Zero-Dependency YouTube Uploader & Video Editor CLI for Termux / Android.
Uses pure Python standard library (no pip packages or C/Rust compilation required)
plus Termux ffmpeg/ffprobe for local video editing.
Supports OAuth2 authentication with automatic Android browser launch,
local FFmpeg video editing (trim, merge, Shorts 9:16, speed, audio/BGM, text, watermark, compression),
resumable chunked video uploads, custom thumbnails, and post-upload metadata editing.
"""

import argparse
import base64
import hashlib
import http.client
import http.server
import json
import mimetypes
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
SCOPES = f"{UPLOAD_SCOPE} {MANAGE_SCOPE}"
UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
OAUTH_TIMEOUT_SECONDS = 300
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB
UPLOAD_MAX_RETRIES = 10

CONFIG_DIR = Path.home() / ".config" / "youtube_uploader"
DEFAULT_TOKEN_PATH = CONFIG_DIR / "token.json"
DEFAULT_SECRET_PATH = CONFIG_DIR / "client_secret.json"
DOWNLOAD_DIR = Path("/storage/emulated/0/Download")
CAMERA_DIR = Path("/storage/emulated/0/DCIM/Camera")
DEFAULT_LOG_PATH = CAMERA_DIR / "yt_upload.log"

RESOLUTION_MAP = {
    "2160p": (3840, 2160),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (854, 480),
}

ANDROID_FONTS = [
    Path("/system/fonts/Roboto-Regular.ttf"),
    Path("/system/fonts/DroidSans.ttf"),
    Path("/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf"),
]


class DevLogger:
    """Developer & learning logger that records detailed pipeline internals to a log file."""

    def __init__(self, log_path: Path | None = DEFAULT_LOG_PATH, enabled: bool = True):
        self.enabled = enabled and (log_path is not None)
        self.log_path = log_path
        if self.enabled and self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, msg: str) -> None:
        if not self.enabled or not self.log_path:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as f:
            for line in msg.splitlines() or [""]:
                f.write(f"[{ts}] [{level}] {line}\n")

    def section(self, title: str) -> None:
        bar = "=" * 72
        self._write("SECTION", bar)
        self._write("SECTION", f"  {title}")
        self._write("SECTION", bar)

    def info(self, msg: str, console: bool = True) -> None:
        if console:
            print(msg, flush=True)
        self._write("INFO", msg)

    def step(self, step_name: str, detail: str, why: str | None = None) -> None:
        self._write("STEP", f"{step_name}: {detail}")
        if why:
            self._write("LEARN", f"  -> Why: {why}")

    def cmd(self, cmd_list: list[str]) -> None:
        quoted = " ".join(shlex.quote(str(c)) for c in cmd_list)
        self._write("COMMAND", f"Full reproducible command:\n  {quoted}")

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)


LOGGER = DevLogger(enabled=False)


def write_private(path: Path, data: str | bytes) -> None:
    """Write a file readable only by the current user (tokens and secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)
    os.chmod(path, 0o600)


def load_client_info(path: Path) -> dict:
    """Parse a client_secret.json and return its 'installed' (or 'web') section."""
    data = json.loads(path.read_text())
    info = data.get("installed") or data.get("web")
    if not info or "client_id" not in info or "client_secret" not in info:
        raise ValueError(
            f"{path} is not a valid OAuth client secret (expected an 'installed' "
            "Desktop app client with client_id and client_secret)."
        )
    return info


def find_client_secret(custom_path: str | None = None) -> dict:
    """Locate and load the OAuth client_secret.json file."""
    if custom_path:
        p = Path(custom_path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Specified client secret not found: {p}")
        return load_client_info(p)

    if DEFAULT_SECRET_PATH.exists():
        return load_client_info(DEFAULT_SECRET_PATH)

    candidates = []
    for folder in [Path.cwd(), CAMERA_DIR, DOWNLOAD_DIR]:
        if folder.exists():
            candidates.extend(
                sorted(
                    folder.glob("client_secret*.json"),
                    key=lambda x: x.stat().st_mtime,
                    reverse=True,
                )
            )

    if candidates:
        latest = candidates[0]
        info = load_client_info(latest)
        write_private(DEFAULT_SECRET_PATH, latest.read_bytes())
        print(f"Found and saved client secret from: {latest}")
        return info

    raise FileNotFoundError(
        "Could not find client_secret*.json in the current directory, "
        f"{CAMERA_DIR} or {DOWNLOAD_DIR}."
    )


def open_android_url(url: str) -> None:
    """Attempt to open a URL in the Android browser from Termux."""
    for cmd in [
        ["termux-open-url", url],
        ["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
    ]:
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
            )
            if res.returncode == 0:
                return
        except Exception:
            pass


def post_form(url: str, data: dict) -> dict:
    """Send an application/x-www-form-urlencoded POST request and parse JSON."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token(
    client_secret_path: str | None = None,
    require_manage_scope: bool = False,
    force_reauth: bool = False,
) -> str:
    """Authenticate with Google OAuth2 and return a valid access_token."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    client_info = find_client_secret(client_secret_path)
    client_id = client_info["client_id"]
    client_secret = client_info["client_secret"]
    token_uri = client_info.get("token_uri", "https://oauth2.googleapis.com/token")
    auth_uri = client_info.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")

    if DEFAULT_TOKEN_PATH.exists() and not force_reauth:
        token_data = json.loads(DEFAULT_TOKEN_PATH.read_text())
        granted_scope = token_data.get("scope", "")
        scope_ok = True
        if require_manage_scope and MANAGE_SCOPE not in granted_scope.split():
            print(
                "Current token only has upload scope; re-authenticating for full YouTube manage scope..."
            )
            scope_ok = False

        if scope_ok:
            expires_at = token_data.get("expires_at", 0)
            if time.time() < expires_at - 60 and "access_token" in token_data:
                return token_data["access_token"]

            if "refresh_token" in token_data:
                try:
                    print("Refreshing OAuth2 access token...")
                    refreshed = post_form(
                        token_uri,
                        {
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "refresh_token": token_data["refresh_token"],
                            "grant_type": "refresh_token",
                        },
                    )
                    token_data["access_token"] = refreshed["access_token"]
                    token_data["expires_at"] = time.time() + refreshed.get(
                        "expires_in", 3600
                    )
                    if "scope" in refreshed:
                        token_data["scope"] = refreshed["scope"]
                    write_private(DEFAULT_TOKEN_PATH, json.dumps(token_data, indent=2))
                    return token_data["access_token"]
                except Exception as e:
                    print(f"Token refresh failed ({e}), re-authenticating...")

    # Start a local loopback HTTP server on an ephemeral port
    auth_result = {"code": None, "error": None}
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )

    class OAuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            if "code" not in params and "error" not in params:
                # Stray request (browser prefetch, etc.): ignore and keep waiting
                self.send_response(404)
                self.end_headers()
                return
            if params.get("state", [None])[0] != state:
                body = b"<html><body><h2>Invalid OAuth state. Please retry from Termux.</h2></body></html>"
                self.send_response(400)
            elif "code" in params:
                auth_result["code"] = params["code"][0]
                body = b"<html><body style='font-family:sans-serif;text-align:center;padding:40px;'><h2>Authorization Successful!</h2><p>You can close this tab and return to Termux.</p></body></html>"
                self.send_response(200)
            else:
                auth_result["error"] = params["error"][0]
                body = b"<html><body><h2>Authorization Failed.</h2></body></html>"
                self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", 8080), OAuthHandler)
    except OSError:
        server = http.server.HTTPServer(("127.0.0.1", 0), OAuthHandler)
    port = server.server_port
    redirect_uri = f"http://127.0.0.1:{port}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{auth_uri}?{urllib.parse.urlencode(params)}"

    print("\n========================================================")
    print("Opening browser for Google YouTube authorization...")
    print("If the browser does not open automatically, tap/open:")
    print(authorize_url)
    print("========================================================\n", flush=True)

    open_android_url(authorize_url)
    server.timeout = 1
    deadline = time.time() + OAUTH_TIMEOUT_SECONDS
    try:
        while auth_result["code"] is None and auth_result["error"] is None:
            if time.time() > deadline:
                raise RuntimeError(
                    f"Timed out after {OAUTH_TIMEOUT_SECONDS}s waiting for browser authorization."
                )
            server.handle_request()
    finally:
        server.server_close()

    if auth_result["error"] or not auth_result["code"]:
        raise RuntimeError(f"OAuth authorization failed: {auth_result['error']}")

    token_resp = post_form(
        token_uri,
        {
            "code": auth_result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
    )
    token_resp["expires_at"] = time.time() + token_resp.get("expires_in", 3600)
    write_private(DEFAULT_TOKEN_PATH, json.dumps(token_resp, indent=2))
    print(f"Authentication successful! Token saved to {DEFAULT_TOKEN_PATH}")
    return token_resp["access_token"]


def find_latest_videos(count: int = 1, directory: Path = CAMERA_DIR) -> list[Path]:
    """Return the N most recently modified video files in the directory (chronological order)."""
    extensions = ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.3gp")
    files = []
    for ext in extensions:
        files.extend(directory.glob(ext))
        files.extend(directory.glob(ext.upper()))
    # Exclude already-edited output files if they end with _edited.mp4
    files = [f for f in set(files) if not f.stem.endswith("_edited")]
    if not files:
        raise FileNotFoundError(f"No video files found in {directory}")
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    selected = files[:count]
    # Return oldest-to-newest so merged clips play in chronological order
    selected.reverse()
    return selected


def find_latest_video(directory: Path = CAMERA_DIR) -> Path:
    """Return the most recently modified video file in the directory."""
    return find_latest_videos(1, directory)[0]


# =====================================================================
# FFmpeg Video Editing Pipeline
# =====================================================================


def ensure_ffmpeg(auto_install: bool = True) -> None:
    """Verify ffmpeg and ffprobe are available; optionally install via pkg in Termux."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    if auto_install and shutil.which("pkg"):
        print("ffmpeg not found. Installing ffmpeg via Termux pkg...")
        res = subprocess.run(["pkg", "install", "-y", "ffmpeg"])
        if res.returncode == 0 and shutil.which("ffmpeg"):
            return
    raise RuntimeError(
        "ffmpeg and ffprobe are required for video editing. Install with: pkg install ffmpeg"
    )


def parse_time_to_seconds(val: str | float | None) -> float | None:
    """Convert a time string ('1:30', '00:01:30.5', or '90') into seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid time format: {val}")


def probe_video(path: Path) -> dict:
    """Inspect video metadata using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)

    video_stream = None
    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not video_stream:
            video_stream = stream
        elif stream.get("codec_type") == "audio" and not audio_stream:
            audio_stream = stream

    if not video_stream:
        raise ValueError(f"No video stream found in {path}")

    raw_width = int(video_stream.get("width", 1920))
    raw_height = int(video_stream.get("height", 1080))
    width, height = raw_width, raw_height

    # Account for rotation metadata in mobile camera recordings
    rotation = 0
    signed_rotation = 0
    tags = video_stream.get("tags", {}) or {}
    if "rotate" in tags:
        try:
            signed_rotation = int(tags["rotate"])
            rotation = abs(signed_rotation)
        except ValueError:
            pass
    for sd in video_stream.get("side_data_list", []):
        if "rotation" in sd:
            try:
                signed_rotation = int(sd["rotation"])
                rotation = abs(signed_rotation)
            except ValueError:
                pass
    if rotation in (90, 270):
        width, height = height, width

    duration = float(
        video_stream.get("duration")
        or data.get("format", {}).get("duration")
        or 0.0
    )

    fps = 30.0
    r_frame_rate = video_stream.get("r_frame_rate", "30/1")
    if "/" in r_frame_rate:
        num, den = r_frame_rate.split("/", 1)
        if float(den) > 0:
            fps = float(num) / float(den)
    elif r_frame_rate:
        fps = float(r_frame_rate)

    fmt_info = data.get("format", {}) or {}
    video_bitrate = int(video_stream.get("bit_rate") or fmt_info.get("bit_rate") or 0)
    file_size = int(fmt_info.get("size") or (path.stat().st_size if path.exists() else 0))

    return {
        "width": width,
        "height": height,
        "raw_width": raw_width,
        "raw_height": raw_height,
        "rotation": signed_rotation,
        "duration": duration,
        "fps": round(fps, 2) if fps > 0 else 30.0,
        "video_codec": video_stream.get("codec_name", "unknown"),
        "profile": video_stream.get("profile", "unknown"),
        "pix_fmt": video_stream.get("pix_fmt", "unknown"),
        "video_bitrate": video_bitrate,
        "file_size": file_size,
        "has_audio": audio_stream is not None,
        "audio_codec": audio_stream.get("codec_name", "none") if audio_stream else "none",
        "sample_rate": int(audio_stream.get("sample_rate", 48000)) if audio_stream else 0,
        "channels": int(audio_stream.get("channels", 0)) if audio_stream else 0,
        "audio_bitrate": int(audio_stream.get("bit_rate", 0)) if audio_stream else 0,
    }


def format_probe_summary(path: Path, meta: dict) -> str:
    """Format a human-readable developer summary of probed video metadata."""
    size_mb = meta["file_size"] / (1024 * 1024)
    v_mbps = meta["video_bitrate"] / 1_000_000
    rot_note = (
        f" (stored {meta['raw_width']}x{meta['raw_height']} with {meta['rotation']}° display matrix)"
        if meta["rotation"]
        else ""
    )
    aud_note = (
        f"{meta['audio_codec']} {meta['sample_rate']}Hz {meta['channels']}ch ({meta['audio_bitrate'] // 1000} kbps)"
        if meta["has_audio"]
        else "No audio"
    )
    return (
        f"File: {path.name} ({size_mb:.1f} MB, {meta['duration']:.2f}s)\n"
        f"  Video: {meta['video_codec']} ({meta['profile']}, {meta['pix_fmt']}) "
        f"{meta['width']}x{meta['height']}{rot_note} @ {meta['fps']} fps, {v_mbps:.2f} Mbps\n"
        f"  Audio: {aud_note}"
    )


def build_atempo_chain(speed: float) -> str:
    """Build chained atempo filters for audio speed outside [0.5, 2.0]."""
    if speed <= 0:
        raise ValueError("Speed factor must be > 0")
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-4 or not filters:
        filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def escape_filter_path(path: Path) -> str:
    """Escape a file path for use inside an FFmpeg filter argument (e.g. subtitles, fontfile)."""
    s = str(path.resolve())
    s = s.replace("\\", "/")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


def find_default_font() -> Path | None:
    """Find a readable TrueType font on Android/Termux."""
    for font in ANDROID_FONTS:
        if font.exists():
            return font
    return None


def estimate_output_duration(probes: list[dict], args: argparse.Namespace) -> float:
    """Estimate the edited output length in seconds (after merge, trim and speed)."""
    total = sum(p["duration"] for p in probes)
    start = parse_time_to_seconds(args.trim_start) or 0.0
    end = parse_time_to_seconds(args.trim_end)
    dur = parse_time_to_seconds(args.duration)
    stop = total
    if end is not None:
        stop = min(stop, end)
    elif dur is not None:
        stop = min(stop, start + dur)
    length = max(stop - start, 0.1)
    if args.speed and args.speed > 0:
        length /= args.speed
    return length


def has_editing_options(args: argparse.Namespace, num_inputs: int) -> bool:
    """Return True if any video/audio editing flag or multi-file merge was requested."""
    if num_inputs > 1:
        return True
    edit_attrs = [
        "trim_start",
        "trim_end",
        "duration",
        "shorts",
        "rotate",
        "resolution",
        "fps",
        "crf",
        "bitrate",
        "hwaccel",
        "encoder",
        "speed",
        "text",
        "subtitles",
        "watermark",
        "mute",
        "volume",
        "normalize_audio",
        "bgm",
        "no_upload",
        "output",
    ]
    return any(getattr(args, attr, None) is not None and getattr(args, attr) is not False for attr in edit_attrs)


def can_use_fast_copy(args: argparse.Namespace, num_inputs: int) -> bool:
    """Determine if single-file stream copy (-c copy) can be used."""
    if not args.fast_copy or num_inputs > 1:
        return False
    reencode_attrs = [
        "shorts",
        "rotate",
        "resolution",
        "fps",
        "crf",
        "bitrate",
        "hwaccel",
        "encoder",
        "speed",
        "text",
        "subtitles",
        "watermark",
        "volume",
        "normalize_audio",
        "bgm",
    ]
    return not any(getattr(args, attr, None) for attr in reencode_attrs)


def edit_video(
    inputs: list[Path], output_path: Path, args: argparse.Namespace
) -> Path:
    """Execute the FFmpeg editing pipeline on one or more input videos."""
    ensure_ffmpeg()
    LOGGER.section(f"VIDEO EDITING PIPELINE -> {output_path.name}")

    if can_use_fast_copy(args, len(inputs)):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]
        if args.trim_start is not None:
            cmd.extend(["-ss", str(args.trim_start)])
        if args.trim_end is not None:
            cmd.extend(["-to", str(args.trim_end)])
        elif args.duration is not None:
            cmd.extend(["-t", str(args.duration)])
        cmd.extend(["-i", str(inputs[0])])
        if args.mute:
            cmd.append("-an")
        else:
            cmd.extend(["-c:a", "copy"])
        cmd.extend(["-c:v", "copy", "-movflags", "+faststart", str(output_path)])
        LOGGER.step(
            "Fast Stream Copy (-c copy)",
            f"Trimming/copying {inputs[0].name} without re-encoding",
            "Stream copy avoids decoding/encoding packets, preserving 100% original quality in seconds.",
        )
        LOGGER.cmd(cmd)
        LOGGER.info(f"Running fast stream-copy edit -> {output_path.name}...")
        subprocess.run(cmd, check=True)
        return output_path

    probes = [probe_video(p) for p in inputs]
    for p, meta in zip(inputs, probes):
        LOGGER.info(format_probe_summary(p, meta), console=True)

    first_probe = probes[0]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]

    # Apply input-level trim when editing a single file
    if len(inputs) == 1:
        if args.trim_start is not None:
            cmd.extend(["-ss", str(args.trim_start)])
            LOGGER.step(
                "Input Trim Start (-ss)",
                f"Seeking to {args.trim_start} before input -i",
                "Placing -ss before -i enables fast keyframe input seeking in FFmpeg.",
            )
        if args.trim_end is not None:
            cmd.extend(["-to", str(args.trim_end)])
            LOGGER.step("Input Trim End (-to)", f"Stopping at {args.trim_end}")
        elif args.duration is not None:
            cmd.extend(["-t", str(args.duration)])
            LOGGER.step("Input Duration (-t)", f"Capturing {args.duration} duration")

    for p in inputs:
        cmd.extend(["-i", str(p)])

    watermark_idx = None
    if args.watermark:
        wm_path = Path(args.watermark).expanduser().resolve()
        if not wm_path.exists():
            raise FileNotFoundError(f"Watermark file not found: {wm_path}")
        watermark_idx = len(inputs)
        cmd.extend(["-i", str(wm_path)])

    bgm_idx = None
    if args.bgm:
        bgm_path = Path(args.bgm).expanduser().resolve()
        if not bgm_path.exists():
            raise FileNotFoundError(f"Background music file not found: {bgm_path}")
        bgm_idx = len(inputs) + (1 if watermark_idx is not None else 0)
        cmd.extend(["-stream_loop", "-1", "-i", str(bgm_path)])

    filter_parts: list[str] = []
    need_audio_from_inputs = not args.mute

    # Step 1: Concatenate multiple inputs or select single input
    if len(inputs) > 1:
        target_w = first_probe["width"]
        target_h = first_probe["height"]
        target_fps = args.fps or first_probe["fps"] or 30
        # Ensure even dimensions
        target_w = target_w - (target_w % 2)
        target_h = target_h - (target_h % 2)

        LOGGER.step(
            "Multi-Clip Concat",
            f"Normalizing {len(inputs)} clips to {target_w}x{target_h} @ {target_fps}fps before concat filter",
            "FFmpeg's concat filter requires all input segments to share identical resolution, SAR, pixel format, and audio sample rate.",
        )

        concat_inputs = []
        for idx, meta in enumerate(probes):
            filter_parts.append(
                f"[{idx}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
                f"fps={target_fps},format=yuv420p[vcat{idx}]"
            )
            if need_audio_from_inputs:
                if meta["has_audio"]:
                    filter_parts.append(
                        f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[acat{idx}]"
                    )
                else:
                    dur = max(meta["duration"], 0.1)
                    filter_parts.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000:d={dur}[acat{idx}]"
                    )
                concat_inputs.append(f"[vcat{idx}][acat{idx}]")
            else:
                concat_inputs.append(f"[vcat{idx}]")

        if need_audio_from_inputs:
            filter_parts.append(
                f"{''.join(concat_inputs)}concat=n={len(inputs)}:v=1:a=1[v_base][a_base]"
            )
            has_source_audio = True
        else:
            filter_parts.append(
                f"{''.join(concat_inputs)}concat=n={len(inputs)}:v=1:a=0[v_base]"
            )
            has_source_audio = False

        # If trim is requested on merged output, apply trim filter after concat
        if any(x is not None for x in (args.trim_start, args.trim_end, args.duration)):
            start_s = parse_time_to_seconds(args.trim_start) or 0.0
            end_s = parse_time_to_seconds(args.trim_end)
            dur_s = parse_time_to_seconds(args.duration)
            trim_args = [f"start={start_s}"]
            if end_s is not None:
                trim_args.append(f"end={end_s}")
            elif dur_s is not None:
                trim_args.append(f"duration={dur_s}")
            t_str = ":".join(trim_args)
            filter_parts.append(f"[v_base]trim={t_str},setpts=PTS-STARTPTS[v_trimmed]")
            v_curr = "v_trimmed"
            if has_source_audio:
                filter_parts.append(
                    f"[a_base]atrim={t_str},asetpts=PTS-STARTPTS[a_trimmed]"
                )
                a_curr = "a_trimmed"
            else:
                a_curr = None
        else:
            v_curr = "v_base"
            a_curr = "a_base" if has_source_audio else None
    else:
        v_curr = "0:v"
        has_source_audio = first_probe["has_audio"] and need_audio_from_inputs
        a_curr = "0:a" if has_source_audio else None
        if first_probe["rotation"]:
            LOGGER.step(
                "Auto-Rotation",
                f"Input has {first_probe['rotation']}° display matrix ({first_probe['raw_width']}x{first_probe['raw_height']} -> {first_probe['width']}x{first_probe['height']})",
                "FFmpeg automatically rotates decoded frames into display orientation before running -filter_complex and clears the rotation tag on output.",
            )

    v_step = 0

    def next_v(filter_expr: str) -> str:
        nonlocal v_curr, v_step
        v_step += 1
        out_label = f"v{v_step}"
        filter_parts.append(f"[{v_curr}]{filter_expr}[{out_label}]")
        v_curr = out_label
        return out_label

    # Step 2: Rotation
    if args.rotate == 90:
        next_v("transpose=1")
        LOGGER.step("Rotate", "90° clockwise (transpose=1)")
    elif args.rotate == 180:
        next_v("transpose=2,transpose=2")
        LOGGER.step("Rotate", "180° (transpose=2,transpose=2)")
    elif args.rotate == 270:
        next_v("transpose=2")
        LOGGER.step("Rotate", "270° clockwise (transpose=2)")

    # Step 3: Shorts (9:16 1080x1920) OR Resolution scaling
    if args.shorts:
        LOGGER.step(
            "YouTube Shorts 9:16",
            f"Formatting to 1080x1920 vertical using mode='{args.shorts_mode}'",
            "YouTube Shorts require a 9:16 vertical aspect ratio (1080x1920) and <= 60s (or <= 3m) duration.",
        )
        if args.shorts_mode == "crop":
            next_v(
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
            )
        elif args.shorts_mode == "pad":
            next_v(
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
            )
        else:  # blur background (default)
            v_step += 1
            out_label = f"v{v_step}"
            filter_parts.append(
                f"[{v_curr}]split=2[vbg{v_step}][vfg{v_step}];"
                f"[vbg{v_step}]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,boxblur=20:10[bg{v_step}];"
                f"[vfg{v_step}]scale=1080:1920:force_original_aspect_ratio=decrease[fg{v_step}];"
                f"[bg{v_step}][fg{v_step}]overlay=(W-w)/2:(H-h)/2,setsar=1[{out_label}]"
            )
            v_curr = out_label
    elif args.resolution:
        max_w, max_h = RESOLUTION_MAP[args.resolution]
        # Handle portrait vs landscape orientation gracefully
        is_portrait = first_probe["height"] > first_probe["width"]
        if args.rotate in (90, 270):
            is_portrait = not is_portrait
        if is_portrait:
            max_w, max_h = max_h, max_w
        next_v(
            f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
        )
        LOGGER.step(
            "Resolution Scaling",
            f"Bounding box {max_w}x{max_h} ({'portrait' if is_portrait else 'landscape'} {args.resolution})",
            "Swapping width/height bounds when portrait prevents accidentally shrinking a 1080x1920 vertical video down to 608x1080.",
        )

    # Step 4: Frame rate
    if args.fps:
        next_v(f"fps={args.fps}")
        LOGGER.step("Frame Rate", f"Target {args.fps} fps")

    # Step 5: Watermark overlay
    if watermark_idx is not None:
        v_step += 1
        out_label = f"v{v_step}"
        wm_scale = args.watermark_scale
        pos_map = {
            "top-left": "20:20",
            "top-right": "W-w-20:20",
            "bottom-left": "20:H-h-20",
            "bottom-right": "W-w-20:H-h-20",
            "center": "(W-w)/2:(H-h)/2",
        }
        coords = pos_map.get(args.watermark_pos, "W-w-20:20")
        filter_parts.append(
            f"[{watermark_idx}:v]scale={wm_scale}:-1[wm{v_step}];"
            f"[{v_curr}][wm{v_step}]overlay={coords}[{out_label}]"
        )
        v_curr = out_label
        LOGGER.step("Watermark Overlay", f"Position={args.watermark_pos}, width={wm_scale}px")

    # Step 6: Burn-in Subtitles (.srt / .ass)
    if args.subtitles:
        sub_path = Path(args.subtitles).expanduser().resolve()
        if not sub_path.exists():
            raise FileNotFoundError(f"Subtitles file not found: {sub_path}")
        escaped_sub = escape_filter_path(sub_path)
        next_v(f"subtitles='{escaped_sub}'")
        LOGGER.step("Subtitles", f"Burn-in from {sub_path.name}")

    # Step 7: Text Overlay (drawtext). The text is passed via a temp file with
    # expansion disabled so characters like % : ' \\ need no escaping.
    text_file: Path | None = None
    if args.text:
        font_path = find_default_font()
        font_opt = f"fontfile='{escape_filter_path(font_path)}':" if font_path else ""
        fd, tmp_txt = tempfile.mkstemp(prefix="yt_text_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            tf.write(args.text)
        text_file = Path(tmp_txt)
        y_map = {
            "top": "60",
            "center": "(h-text_h)/2",
            "bottom": "h-text_h-80",
        }
        y_expr = y_map.get(args.text_pos, "h-text_h-80")
        next_v(
            f"drawtext={font_opt}textfile='{escape_filter_path(text_file)}':expansion=none:"
            f"fontsize={args.font_size}:fontcolor={args.font_color}:"
            f"x=(w-text_w)/2:y={y_expr}:"
            f"box=1:boxcolor=black@0.55:boxborderw=12"
        )
        LOGGER.step("Text Overlay", f"'{args.text}' at {args.text_pos} (via temp textfile)")

    # Step 8: Speed adjustment (video + audio)
    if args.speed and abs(args.speed - 1.0) > 1e-4:
        next_v(f"setpts=PTS/{args.speed:.4f}")
        LOGGER.step(
            "Video Speed",
            f"Multiplier {args.speed}x (setpts=PTS/{args.speed:.4f})",
            "Dividing presentation timestamps (PTS) by speed factor speeds up (>1) or slows down (<1) video playback.",
        )

    # Ensure yuv420p pixel format and even dimensions for universal playback
    next_v("scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")
    LOGGER.step(
        "Pixel Format & Alignment",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "H.264/HEVC encoders and web players require even width/height and 4:2:0 planar chroma subsampling (yuv420p).",
    )

    # Step 9: Audio processing pipeline
    a_step = 0

    def next_a(filter_expr: str) -> str:
        nonlocal a_curr, a_step
        a_step += 1
        out_label = f"a{a_step}"
        filter_parts.append(f"[{a_curr}]{filter_expr}[{out_label}]")
        a_curr = out_label
        return out_label

    if a_curr is not None:
        if args.speed and abs(args.speed - 1.0) > 1e-4:
            atempo_chain = build_atempo_chain(args.speed)
            next_a(atempo_chain)
            LOGGER.step(
                "Audio Speed (atempo)",
                atempo_chain,
                "Each FFmpeg atempo filter only accepts [0.5, 2.0], so speeds outside that range are chained.",
            )
        if args.volume and abs(args.volume - 1.0) > 1e-4:
            next_a(f"volume={args.volume}")
            LOGGER.step("Audio Volume", f"Multiplier {args.volume}x")
        if args.normalize_audio:
            target_sr = first_probe.get("sample_rate") or 48000
            if target_sr not in (44100, 48000):
                target_sr = 48000
            next_a(f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample={target_sr}")
            LOGGER.step(
                "Audio Loudness Normalization (EBU R128)",
                f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample={target_sr}",
                f"Targets -16 LUFS integrated loudness & -1.5 dBTP true peak (YouTube standard). "
                f"Appending aresample={target_sr} is critical because FFmpeg's loudnorm filter internally upsamples to 192 kHz, "
                f"which would otherwise cause the AAC encoder to output 96 kHz instead of {target_sr} Hz.",
            )

    if bgm_idx is not None:
        a_step += 1
        bgm_label = f"abgm{a_step}"
        # The looped BGM input is infinite; bound it to the output length here
        # instead of relying on -shortest (which fails on some ffmpeg builds).
        out_dur = estimate_output_duration(probes, args)
        filter_parts.append(
            f"[{bgm_idx}:a]volume={args.bgm_volume},aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=duration={out_dur:.3f}[{bgm_label}]"
        )
        if a_curr is not None:
            a_step += 1
            mixed_label = f"a{a_step}"
            filter_parts.append(
                f"[{a_curr}][{bgm_label}]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[{mixed_label}]"
            )
            a_curr = mixed_label
        else:
            a_curr = bgm_label
        LOGGER.step("Background Music (BGM)", f"Mixed at volume={args.bgm_volume}, bounded to {out_dur:.2f}s")

    filter_complex = ";".join(filter_parts)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(["-map", f"[{v_curr}]"])

    if a_curr is not None:
        a_map = f"[{a_curr}]" if not a_curr.endswith(":a") else a_curr
        cmd.extend(["-map", a_map, "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")

    encoder = getattr(args, "encoder", None) or (
        "h264_mediacodec" if getattr(args, "hwaccel", False) else "libx264"
    )
    if encoder in ("h264_mediacodec", "hevc_mediacodec"):
        bitrate = getattr(args, "bitrate", None) or "8M"
        cmd.extend(
            [
                "-c:v",
                encoder,
                "-b:v",
                str(bitrate),
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        LOGGER.step(
            "Video Encoder (Hardware MediaCodec)",
            f"codec={encoder}, target bitrate={bitrate}, +faststart",
            "Android MediaCodec offloads H.264/HEVC encoding to the SoC hardware video block, running ~6x faster with ~19x lower CPU usage than software libx264.",
        )
    else:
        crf = args.crf if args.crf is not None else 23
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                args.preset,
                "-crf",
                str(crf),
            ]
        )
        if getattr(args, "bitrate", None):
            cmd.extend(["-maxrate", str(args.bitrate), "-bufsize", str(args.bitrate)])
        cmd.extend(["-movflags", "+faststart", str(output_path)])
        LOGGER.step(
            "Video Encoder (Software libx264)",
            f"preset={args.preset}, crf={crf}, +faststart",
            "CRF (Constant Rate Factor) adjusts bitrate dynamically per frame complexity; +faststart moves the MP4 moov atom to the front for instant streaming.",
        )

    LOGGER.cmd(cmd)
    LOGGER.info(f"Editing video with FFmpeg ({encoder}) -> {output_path.name}...")
    t0 = time.time()
    try:
        subprocess.run(cmd, check=True)
    finally:
        if text_file is not None:
            text_file.unlink(missing_ok=True)
    elapsed = max(time.time() - t0, 0.001)

    out_probe = probe_video(output_path)
    in_total_bytes = sum(m["file_size"] for m in probes)
    out_bytes = out_probe["file_size"]
    in_mb = in_total_bytes / (1024 * 1024)
    out_mb = out_bytes / (1024 * 1024)
    pct_change = ((out_bytes - in_total_bytes) / in_total_bytes * 100) if in_total_bytes > 0 else 0.0
    realtime_speed = out_probe["duration"] / elapsed if elapsed > 0 else 0.0

    LOGGER.info(format_probe_summary(output_path, out_probe), console=True)
    summary_line = (
        f"Edited video saved: {output_path} | "
        f"{in_mb:.1f} MB -> {out_mb:.1f} MB ({pct_change:+.1f}%) | "
        f"Encoded in {elapsed:.1f}s ({realtime_speed:.2f}x realtime)"
    )
    LOGGER.info(summary_line, console=True)
    return output_path


# =====================================================================
# YouTube Data API v3 Upload, Thumbnails & Metadata Update
# =====================================================================


def parse_publish_at(value: str) -> str:
    """Convert a --publish-at value into the RFC 3339 UTC string YouTube expects.

    Accepts local times ('2026-11-20 18:00'), ISO 8601 with an offset
    ('2026-11-20T18:00:00+01:00', '...Z'), or relative times ('+30m', '+2h', '+1d').
    """
    s = value.strip()
    now = datetime.now(timezone.utc)
    rel = re.fullmatch(r"\+\s*(\d+)\s*([mhd])", s, re.IGNORECASE)
    if rel:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[rel.group(2).lower()]
        dt = now + timedelta(**{unit: int(rel.group(1))})
    else:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"Invalid --publish-at '{value}'. Use 'YYYY-MM-DD HH:MM' (local time), "
                "ISO 8601 with an offset (e.g. 2026-11-20T18:00:00Z), or a relative "
                "time like +30m, +2h, +1d."
            )
        if dt.tzinfo is None:
            dt = dt.astimezone()  # naive times are the phone's local time
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    if dt <= now:
        raise ValueError(
            f"--publish-at must be in the future (got {format_publish_time(dt)})."
        )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_publish_time(value: datetime | str) -> str:
    """Render a publish time as 'local time (UTC time)' for display."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    local = value.astimezone()
    utc_text = f"{value.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"
    if local.utcoffset() == timedelta(0):
        return utc_text
    return f"{local:%Y-%m-%d %H:%M %Z} ({utc_text})"


RETRYABLE_STATUS = (500, 502, 503, 504)


def _received_bytes(headers) -> int:
    """Parse the 'Range: bytes=0-N' header of a 308 response into a resume offset."""
    rng = headers.get("Range") if headers else None
    if not rng or "-" not in rng:
        return 0
    return int(rng.rsplit("-", 1)[1]) + 1


def _query_upload_status(upload_url: str, file_size: int) -> tuple[int, dict | None]:
    """Ask the resumable session how many bytes it has. Returns (offset, final_response)."""
    req = urllib.request.Request(
        upload_url,
        data=b"",
        headers={"Content-Length": "0", "Content-Range": f"bytes */{file_size}"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
        return file_size, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        if e.code == 308:
            return _received_bytes(e.headers), None
        if e.code in RETRYABLE_STATUS:
            raise  # HTTPError is an OSError: the caller retries it
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Upload session is no longer valid (HTTP {e.code}): {err_body}"
        )


def _recover_upload(
    upload_url: str, file_size: int, retries: int, reason: str
) -> tuple[int, dict | None, int]:
    """Back off exponentially and re-sync the offset after a transient failure."""
    while True:
        retries += 1
        if retries > UPLOAD_MAX_RETRIES:
            raise RuntimeError(
                f"Upload failed after {UPLOAD_MAX_RETRIES} retries (last error: {reason})."
            )
        wait = min(2**retries, 60)
        print(f"Upload interrupted ({reason}); retry {retries}/{UPLOAD_MAX_RETRIES} in {wait}s...", flush=True)
        time.sleep(wait)
        try:
            offset, final = _query_upload_status(upload_url, file_size)
            return offset, final, retries
        except (OSError, http.client.HTTPException) as e:
            reason = f"{type(e).__name__}: {e}"


def upload_video(
    access_token: str,
    file_path: Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str,
    privacy_status: str,
    made_for_kids: bool,
    publish_at: str | None = None,
) -> str:
    """Upload a video file using YouTube Data API v3 Resumable Upload."""
    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }
    if publish_at:
        metadata["status"]["publishAt"] = publish_at
    metadata_bytes = json.dumps(metadata).encode("utf-8")
    file_size = file_path.stat().st_size

    init_req = urllib.request.Request(
        UPLOAD_INIT_URL,
        data=metadata_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(file_size),
            "X-Upload-Content-Type": "video/*",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(init_req, timeout=60) as resp:
            upload_url = resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Failed to initiate upload (HTTP {e.code}): {err_body}")

    if not upload_url:
        raise RuntimeError(
            "YouTube API did not return a resumable upload Location header."
        )

    print(f"Uploading '{file_path.name}' ({file_size / (1024 * 1024):.1f} MB)...")
    offset = 0
    retries = 0
    response_data = None

    with file_path.open("rb") as f:
        while offset < file_size:
            f.seek(offset)
            chunk = f.read(UPLOAD_CHUNK_SIZE)
            end_byte = offset + len(chunk) - 1
            put_req = urllib.request.Request(
                upload_url,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                },
                method="PUT",
            )
            try:
                with urllib.request.urlopen(put_req, timeout=300) as resp:
                    resp_body = resp.read().decode("utf-8")
                response_data = json.loads(resp_body) if resp_body else {}
                print("Progress: 100%", flush=True)
                break
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    # Resume Incomplete: the server tells us how much it actually stored
                    offset = _received_bytes(e.headers)
                    retries = 0
                    print(f"Progress: {int(offset / file_size * 100)}%", flush=True)
                    continue
                if e.code not in RETRYABLE_STATUS:
                    err_body = e.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"Chunk upload failed (HTTP {e.code}): {err_body}"
                    )
                reason = f"HTTP {e.code}"
            except (OSError, http.client.HTTPException) as e:
                reason = f"{type(e).__name__}: {e}"

            # Transient failure: back off, ask the server where to resume, retry
            offset, response_data, retries = _recover_upload(
                upload_url, file_size, retries, reason
            )
            if response_data is not None:
                print("Progress: 100%", flush=True)
                break

    video_id = (response_data or {}).get("id", "unknown")
    print("\nUpload Complete!")
    print(f"Video ID:   {video_id}")
    print(f"Watch URL:  https://youtu.be/{video_id}")
    if publish_at:
        print(f"Scheduled:  goes public {format_publish_time(publish_at)}")
    print(f"Studio URL: https://studio.youtube.com/video/{video_id}/edit")
    return video_id


def upload_thumbnail(access_token: str, video_id: str, thumbnail_path: Path) -> None:
    """Upload and set a custom thumbnail image for a YouTube video."""
    p = thumbnail_path.expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Thumbnail file not found: {p}")

    mime_type, _ = mimetypes.guess_type(str(p))
    if mime_type not in ("image/jpeg", "image/png", "image/webp"):
        mime_type = "image/jpeg"

    img_bytes = p.read_bytes()
    url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={urllib.parse.quote(video_id)}&uploadType=media"
    req = urllib.request.Request(
        url,
        data=img_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": mime_type,
            "Content-Length": str(len(img_bytes)),
        },
        method="POST",
    )
    print(f"Uploading custom thumbnail '{p.name}' for video {video_id}...")
    try:
        with urllib.request.urlopen(req) as resp:
            resp.read()
        print("Thumbnail updated successfully!")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Thumbnail upload failed (HTTP {e.code}): {err_body}")


def update_video_metadata(
    access_token: str,
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str | None = None,
    made_for_kids: bool | None = None,
    publish_at: str | None = None,
) -> dict:
    """Fetch and update snippet/status metadata of an existing YouTube video."""
    list_url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?part=snippet,status&id={urllib.parse.quote(video_id)}"
    )
    get_req = urllib.request.Request(
        list_url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(get_req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Failed to fetch video {video_id} (HTTP {e.code}): {err_body}")

    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"Video ID '{video_id}' not found on your YouTube channel.")

    item = items[0]
    snippet = item.get("snippet", {})
    status = item.get("status", {})

    if title is not None:
        snippet["title"] = title
    if description is not None:
        snippet["description"] = description
    if tags is not None:
        snippet["tags"] = tags
    if category_id is not None:
        snippet["categoryId"] = category_id
    if privacy_status is not None:
        status["privacyStatus"] = privacy_status
    if made_for_kids is not None:
        status["selfDeclaredMadeForKids"] = made_for_kids
    if publish_at is not None:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    payload = {
        "id": video_id,
        "snippet": {
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "categoryId": snippet.get("categoryId", "22"),
        },
        "status": {
            "privacyStatus": status.get("privacyStatus", "unlisted"),
            "selfDeclaredMadeForKids": status.get("selfDeclaredMadeForKids", False),
        },
    }
    # Keep an existing schedule unless the video is being made public/unlisted now;
    # omitting publishAt from the PUT would otherwise cancel it.
    if status.get("publishAt") and payload["status"]["privacyStatus"] == "private":
        payload["status"]["publishAt"] = status["publishAt"]
    if "tags" in snippet:
        payload["snippet"]["tags"] = snippet["tags"]

    put_url = "https://www.googleapis.com/youtube/v3/videos?part=snippet,status"
    body_bytes = json.dumps(payload).encode("utf-8")
    put_req = urllib.request.Request(
        put_url,
        data=body_bytes,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method="PUT",
    )
    print(f"Updating metadata for YouTube video {video_id}...")
    try:
        with urllib.request.urlopen(put_req) as resp:
            updated = json.loads(resp.read().decode("utf-8"))
        print(f"Metadata updated successfully! Watch URL: https://youtu.be/{video_id}")
        scheduled = (updated.get("status") or {}).get("publishAt")
        if scheduled:
            print(f"Scheduled to go public {format_publish_time(scheduled)}")
        return updated
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Failed to update video metadata (HTTP {e.code}): {err_body}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Edit and upload videos to YouTube from Termux."
    )

    # Authentication & Post-Upload Mode
    auth_grp = parser.add_argument_group("Authentication & Post-Upload Management")
    auth_grp.add_argument(
        "--auth-only",
        action="store_true",
        help="Authenticate and save OAuth token without uploading.",
    )
    auth_grp.add_argument(
        "--reauth",
        action="store_true",
        help="Force OAuth2 re-authentication (grants upload + full YouTube manage scopes).",
    )
    auth_grp.add_argument(
        "--client-secret", type=str, help="Custom path to client_secret.json."
    )
    auth_grp.add_argument(
        "--update-video",
        type=str,
        metavar="VIDEO_ID",
        help="Update metadata or thumbnail of an existing YouTube video ID.",
    )
    auth_grp.add_argument(
        "--thumbnail",
        type=str,
        metavar="IMAGE_PATH",
        help="Upload a custom thumbnail (.jpg/.png) for the uploaded or updated video.",
    )

    # Input Selection
    in_grp = parser.add_argument_group("Input Video Selection")
    in_grp.add_argument(
        "--file", "-f", type=str, help="Path to a single video file to edit/upload."
    )
    in_grp.add_argument(
        "--files",
        nargs="+",
        type=str,
        metavar="PATH",
        help="Multiple video files to merge/concatenate (or edit individually with --batch).",
    )
    in_grp.add_argument(
        "--latest",
        action="store_true",
        help="Select the most recent video in DCIM/Camera.",
    )
    in_grp.add_argument(
        "--latest-n",
        type=int,
        metavar="N",
        help="Select the N most recent videos in DCIM/Camera (in chronological order).",
    )
    in_grp.add_argument(
        "--batch",
        action="store_true",
        help="When multiple files are selected (--files or --latest-n), edit each video individually instead of merging.",
    )

    # Developer Logging Options
    log_grp = parser.add_argument_group("Developer Logging & Diagnostics")
    log_grp.add_argument(
        "--log-file",
        type=str,
        default=str(DEFAULT_LOG_PATH),
        help=f"Path to developer/learning log file (default: {DEFAULT_LOG_PATH}).",
    )
    log_grp.add_argument(
        "--no-log",
        action="store_true",
        help="Disable writing to the developer log file.",
    )

    # Video Editing Options
    edit_grp = parser.add_argument_group("Local Video Editing (FFmpeg)")
    edit_grp.add_argument(
        "--output",
        "-o",
        type=str,
        help="Output file path for the edited video (default: <stem>_edited.mp4 when --no-upload).",
    )
    edit_grp.add_argument(
        "--no-upload",
        action="store_true",
        help="Edit video locally without uploading to YouTube.",
    )
    edit_grp.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary edited video file after uploading to YouTube.",
    )
    edit_grp.add_argument(
        "--trim-start",
        type=str,
        metavar="TIME",
        help="Start timestamp to trim from (e.g. 5, 00:00:05, 1:30).",
    )
    edit_grp.add_argument(
        "--trim-end",
        type=str,
        metavar="TIME",
        help="End timestamp to trim at (e.g. 45, 00:00:45).",
    )
    edit_grp.add_argument(
        "--duration",
        type=str,
        metavar="TIME",
        help="Duration to keep from trim-start (e.g. 15, 00:00:15).",
    )
    edit_grp.add_argument(
        "--fast-copy",
        action="store_true",
        help="Use lossless stream copy (-c copy) when only trimming/muting a single video.",
    )
    edit_grp.add_argument(
        "--shorts",
        action="store_true",
        help="Format video as a 9:16 vertical YouTube Short (1080x1920).",
    )
    edit_grp.add_argument(
        "--shorts-mode",
        choices=["blur", "crop", "pad"],
        default="blur",
        help="How to fit video into 9:16 Shorts aspect ratio (default: blur).",
    )
    edit_grp.add_argument(
        "--rotate",
        type=int,
        choices=[90, 180, 270],
        help="Rotate video clockwise by 90, 180, or 270 degrees.",
    )
    edit_grp.add_argument(
        "--resolution",
        choices=["2160p", "1080p", "720p", "480p"],
        help="Scale video to target resolution while preserving aspect ratio.",
    )
    edit_grp.add_argument(
        "--fps", type=int, help="Target frame rate (e.g. 30, 60)."
    )
    edit_grp.add_argument(
        "--crf",
        type=int,
        help="H.264 Constant Rate Factor quality (0-51, lower is higher quality, default: 23).",
    )
    edit_grp.add_argument(
        "--preset",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
        default="fast",
        help="FFmpeg x264 encoding speed/compression preset (default: fast).",
    )
    edit_grp.add_argument(
        "--hwaccel",
        action="store_true",
        help="Use Android hardware MediaCodec encoder (h264_mediacodec) for ~6x faster encoding on Termux.",
    )
    edit_grp.add_argument(
        "--encoder",
        choices=["libx264", "h264_mediacodec", "hevc_mediacodec"],
        default=None,
        help="Explicitly select video encoder (libx264, h264_mediacodec, hevc_mediacodec).",
    )
    edit_grp.add_argument(
        "--bitrate",
        type=str,
        default=None,
        help="Target video bitrate (e.g. '8M', '5M'; default '8M' when using MediaCodec).",
    )
    edit_grp.add_argument(
        "--speed",
        type=float,
        help="Video & audio playback speed multiplier (e.g. 0.5 for slow-mo, 1.5, 2.0).",
    )
    edit_grp.add_argument(
        "--text", type=str, help="Burn a text overlay onto the video."
    )
    edit_grp.add_argument(
        "--text-pos",
        choices=["top", "center", "bottom"],
        default="bottom",
        help="Vertical position for text overlay (default: bottom).",
    )
    edit_grp.add_argument(
        "--font-size",
        type=int,
        default=48,
        help="Font size for text overlay (default: 48).",
    )
    edit_grp.add_argument(
        "--font-color",
        type=str,
        default="white",
        help="Font color for text overlay (default: white).",
    )
    edit_grp.add_argument(
        "--subtitles",
        type=str,
        metavar="SRT_PATH",
        help="Burn-in subtitles from a .srt or .ass file.",
    )
    edit_grp.add_argument(
        "--watermark",
        type=str,
        metavar="IMG_PATH",
        help="Overlay an image watermark onto the video.",
    )
    edit_grp.add_argument(
        "--watermark-pos",
        choices=["top-left", "top-right", "bottom-left", "bottom-right", "center"],
        default="top-right",
        help="Corner/position for the watermark overlay (default: top-right).",
    )
    edit_grp.add_argument(
        "--watermark-scale",
        type=int,
        default=160,
        help="Watermark width in pixels (default: 160).",
    )

    # Audio Editing Options
    aud_grp = parser.add_argument_group("Audio Editing (FFmpeg)")
    aud_grp.add_argument(
        "--mute", action="store_true", help="Remove the original audio track."
    )
    aud_grp.add_argument(
        "--volume",
        type=float,
        help="Multiply original audio volume (e.g. 1.5 for louder, 0.5 for quieter).",
    )
    aud_grp.add_argument(
        "--normalize-audio",
        action="store_true",
        help="Apply EBU R128 loudness normalization to the audio track.",
    )
    aud_grp.add_argument(
        "--bgm",
        type=str,
        metavar="AUDIO_PATH",
        help="Add background music file (mixed with original audio, or replaces it if --mute).",
    )
    aud_grp.add_argument(
        "--bgm-volume",
        type=float,
        default=0.25,
        help="Background music volume multiplier (default: 0.25).",
    )

    # YouTube Metadata Options
    meta_grp = parser.add_argument_group("YouTube Video Metadata")
    meta_grp.add_argument(
        "--title", "-t", type=str, help="Video title (defaults to filename stem)."
    )
    meta_grp.add_argument(
        "--description", "-d", type=str, default=None, help="Video description."
    )
    meta_grp.add_argument(
        "--tags", type=str, default=None, help="Comma-separated list of tags."
    )
    meta_grp.add_argument(
        "--privacy",
        "-p",
        choices=["private", "unlisted", "public"],
        default=None,
        help="Privacy status (default: unlisted for new uploads).",
    )
    meta_grp.add_argument(
        "--publish-at",
        type=str,
        metavar="WHEN",
        help=(
            "Schedule the video to go public later (uploaded as private until then). "
            "Accepts local time '2026-11-20 18:00', ISO 8601 '2026-11-20T18:00:00Z', "
            "or relative '+30m', '+2h', '+1d'. Also works with --update-video."
        ),
    )
    meta_grp.add_argument(
        "--category",
        "-c",
        type=str,
        default=None,
        help="YouTube category ID (default: 22 - People & Blogs).",
    )
    meta_grp.add_argument(
        "--made-for-kids",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark (or with --no-made-for-kids, unmark) the video as made for kids.",
    )

    return parser


def resolve_input_files(args: argparse.Namespace) -> list[Path]:
    """Resolve input video file list from --file, --files, --latest, or --latest-n."""
    inputs: list[Path] = []
    if args.files:
        for f in args.files:
            p = Path(f).expanduser().resolve()
            if not p.exists():
                sys.exit(f"Error: File not found: {p}")
            inputs.append(p)
    elif args.file:
        p = Path(args.file).expanduser().resolve()
        if not p.exists():
            sys.exit(f"Error: File not found: {p}")
        inputs.append(p)
    elif args.latest_n:
        if args.latest_n < 1:
            sys.exit("Error: --latest-n must be >= 1")
        inputs = find_latest_videos(args.latest_n)
        LOGGER.info(f"Selected {len(inputs)} latest video(s):")
        for p in inputs:
            LOGGER.info(f"  - {p}")
    elif args.latest:
        p = find_latest_video()
        LOGGER.info(f"Selected latest video: {p}")
        inputs.append(p)
    return inputs


def main():
    global LOGGER
    parser = build_parser()
    args = parser.parse_args()

    log_path = Path(args.log_file).expanduser().resolve() if args.log_file else None
    LOGGER = DevLogger(log_path=log_path, enabled=not args.no_log)
    LOGGER.section("YT_UPLOAD SESSION START")
    LOGGER.info(
        f"CLI Invocation: {' '.join(shlex.quote(a) for a in sys.argv)}\n"
        f"Python: {sys.version.split()[0]} on {platform.platform()}",
        console=False,
    )

    publish_at = None
    if args.publish_at:
        try:
            publish_at = parse_publish_at(args.publish_at)
        except ValueError as e:
            parser.error(str(e))
        if args.privacy not in (None, "private"):
            parser.error(
                "--publish-at requires the video to stay private until the scheduled time; "
                f"remove --privacy {args.privacy}."
            )
        LOGGER.info(f"Will schedule publishing for {format_publish_time(publish_at)}")

    try:
        # Mode 1: Auth-only
        if args.auth_only:
            get_access_token(
                args.client_secret,
                require_manage_scope=args.reauth,
                force_reauth=args.reauth,
            )
            return

        # Mode 2: Post-upload metadata / thumbnail update on existing video
        if args.update_video:
            access_token = get_access_token(
                args.client_secret,
                require_manage_scope=True,
                force_reauth=args.reauth,
            )
            tags = (
                [t.strip() for t in args.tags.split(",") if t.strip()]
                if args.tags is not None
                else None
            )
            has_meta_changes = any(
                x is not None
                for x in (
                    args.title,
                    args.description,
                    tags,
                    args.category,
                    args.privacy,
                    args.made_for_kids,
                    publish_at,
                )
            )
            if has_meta_changes:
                update_video_metadata(
                    access_token=access_token,
                    video_id=args.update_video,
                    title=args.title,
                    description=args.description,
                    tags=tags,
                    category_id=args.category,
                    privacy_status=args.privacy,
                    made_for_kids=args.made_for_kids,
                    publish_at=publish_at,
                )
            if args.thumbnail:
                upload_thumbnail(
                    access_token=access_token,
                    video_id=args.update_video,
                    thumbnail_path=Path(args.thumbnail),
                )
            if not has_meta_changes and not args.thumbnail:
                sys.exit(
                    "Error: With --update-video, specify at least one metadata flag (--title, --description, --privacy, --tags, --publish-at, etc.) or --thumbnail."
                )
            return

        # Mode 3: Local Edit and/or Upload
        inputs = resolve_input_files(args)
        if not inputs:
            parser.print_help()
            sys.exit(
                "\nError: Provide --file, --files, --latest, --latest-n, --update-video, or --auth-only."
            )

        # If --batch is set with multiple files, process each file individually
        batches: list[list[Path]] = (
            [[p] for p in inputs] if (args.batch and len(inputs) > 1) else [inputs]
        )

        for batch_idx, batch_inputs in enumerate(batches, 1):
            primary_video = batch_inputs[0]
            if len(batches) > 1:
                LOGGER.info(
                    f"\n--- Batch [{batch_idx}/{len(batches)}]: {primary_video.name} ---"
                )

            needs_edit = has_editing_options(args, len(batch_inputs))
            temp_file_to_cleanup: Path | None = None

            try:
                if needs_edit:
                    if args.output and len(batches) == 1:
                        output_path = Path(args.output).expanduser().resolve()
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                    elif args.no_upload:
                        output_path = primary_video.with_name(
                            f"{primary_video.stem}_edited.mp4"
                        )
                    else:
                        fd, tmp_name = tempfile.mkstemp(
                            prefix=f"{primary_video.stem}_edit_", suffix=".mp4"
                        )
                        os.close(fd)
                        output_path = Path(tmp_name)
                        if not args.keep_temp:
                            temp_file_to_cleanup = output_path

                    video_to_upload = edit_video(batch_inputs, output_path, args)
                else:
                    video_to_upload = primary_video

                if args.no_upload:
                    LOGGER.info(f"Done! Edited video available at: {video_to_upload}")
                    continue

                access_token = get_access_token(
                    args.client_secret,
                    require_manage_scope=False,
                    force_reauth=args.reauth,
                )
                title = args.title or primary_video.stem
                description = args.description or ""
                if (
                    args.shorts
                    and "#Shorts" not in title
                    and "#Shorts" not in description
                ):
                    description = (description + "\n\n#Shorts").strip()
                tags = (
                    [t.strip() for t in args.tags.split(",") if t.strip()]
                    if args.tags
                    else []
                )
                category_id = args.category or "22"
                privacy_status = (
                    "private" if publish_at else (args.privacy or "unlisted")
                )
                made_for_kids = bool(args.made_for_kids)

                video_id = upload_video(
                    access_token=access_token,
                    file_path=video_to_upload,
                    title=title,
                    description=description,
                    tags=tags,
                    category_id=category_id,
                    privacy_status=privacy_status,
                    made_for_kids=made_for_kids,
                    publish_at=publish_at,
                )

                if args.thumbnail and video_id and video_id != "unknown":
                    upload_thumbnail(
                        access_token=access_token,
                        video_id=video_id,
                        thumbnail_path=Path(args.thumbnail),
                    )
            finally:
                if temp_file_to_cleanup and temp_file_to_cleanup.exists():
                    try:
                        temp_file_to_cleanup.unlink()
                    except OSError:
                        pass

        if LOGGER.enabled and LOGGER.log_path:
            print(f"\nDeveloper log written to: {LOGGER.log_path}")

    except subprocess.CalledProcessError as e:
        LOGGER.error(f"FFmpeg Error: Command failed with exit code {e.returncode}")
        sys.exit(f"FFmpeg Error: Command failed with exit code {e.returncode}")
    except Exception as e:
        LOGGER.error(f"Error: {e}")
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
