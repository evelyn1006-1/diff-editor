"""
Diff Editor - A web-based side-by-side file diff and editing tool.
"""

import difflib
import fcntl
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, url_for, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from openai import OpenAI

load_dotenv()

# Set up access logging (rotation handled externally by rotatelog)
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

access_logger = logging.getLogger("diff_editor.access")
access_logger.setLevel(logging.INFO)
access_logger.propagate = False

_access_handler = logging.FileHandler(LOG_DIR / "access.log")
_access_handler.setFormatter(logging.Formatter(
    '%(message)s'  # We'll format the message ourselves for gunicorn-style output
))
access_logger.addHandler(_access_handler)

from utils.file_ops import read_file_bytes, write_file, write_file_bytes, is_writable_by_user
from utils.git_ops import find_git_root, get_head_content_bytes, is_tracked_by_git, get_directory_git_status, get_tracked_files


TEXT_CONTROL_WHITESPACE_BYTES = {7, 8, 9, 10, 11, 12, 13, 27}


def resolve_request_path(raw_path: str, field_name: str = "path") -> tuple[Path | None, str | None]:
    """Resolve a user-provided path while handling invalid inputs safely."""
    if raw_path is None:
        return None, f"No {field_name} specified"

    raw_path = str(raw_path)
    if not raw_path:
        return None, f"No {field_name} specified"
    if "\x00" in raw_path:
        return None, f"Invalid {field_name}"

    try:
        return Path(raw_path).resolve(), None
    except (OSError, RuntimeError, ValueError):
        return None, f"Invalid {field_name}"


def is_likely_binary(data: bytes) -> bool:
    """Best-effort binary detection for editor rendering."""
    if not data:
        return False

    if b"\x00" in data:
        return True

    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        # Not valid UTF-8. Keep checking for control-byte density so non-UTF text
        # doesn't get mislabeled as binary too aggressively.
        pass

    control_count = sum(1 for b in data if b < 32 and b not in TEXT_CONTROL_WHITESPACE_BYTES)
    return (control_count / len(data)) > 0.30


def bytes_to_hex_view(data: bytes, bytes_per_line: int = 16) -> str:
    """Render bytes as a readable hex dump grouped by bytes."""
    if not data:
        return ""

    lines: list[str] = []
    hex_width = (bytes_per_line * 3) - 1

    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset:offset + bytes_per_line]
        hex_bytes = " ".join(f"{b:02x}" for b in chunk)
        ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_bytes.ljust(hex_width)}  |{ascii_preview}|")

    return "\n".join(lines)


def detect_image_mime(path: Path, data: bytes) -> str | None:
    """Detect common image types from bytes with extension fallback."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return "image/tiff"

    # SVG can be text-based.
    head = data[:2048].decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").lower()
    if head.startswith("<svg") or ("<svg" in head and head.startswith("<?xml")):
        return "image/svg+xml"
    if path.suffix.lower() == ".svg":
        return "image/svg+xml"

    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed

    return None


def parse_hex_view(content: str) -> tuple[bool, bytes | str]:
    """
    Parse hex dump text back into bytes.
    Accepts lines like:
    00000000  aa bb cc dd ...  |....|
    and also plain whitespace-separated byte pairs.
    """
    if not content.strip():
        return True, b""

    parsed = bytearray()
    hex_chars = set("0123456789abcdefABCDEF")

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        # Drop ASCII preview column if present.
        if "|" in line:
            line = line.split("|", 1)[0].rstrip()

        parts = line.split()
        if not parts:
            continue

        # Optional 8-digit hex offset at start of line.
        if len(parts[0]) == 8 and all(ch in hex_chars for ch in parts[0]):
            parts = parts[1:]

        for part in parts:
            if len(part) != 2 or any(ch not in hex_chars for ch in part):
                return False, f"Invalid hex byte '{part}' on line {line_no}"
            parsed.append(int(part, 16))

    return True, bytes(parsed)




def choose_ai_review_cwd(file_path: str) -> Path:
    """Choose a codex working directory by preferring the nearest git repo root."""
    resolved_file_path, _ = resolve_request_path(file_path, "file_path")
    start_dir = (
        resolved_file_path.parent
        if resolved_file_path is not None and resolved_file_path.parent.exists()
        else Path.cwd()
    )

    home = Path.home().resolve()
    current = start_dir.resolve()

    while True:
        if (current / ".git").exists():
            return current

        if current == home:
            # If we reached home without finding a git repo, use the file parent.
            return start_dir

        if current.parent == current:
            return start_dir

        current = current.parent

def create_app() -> Flask:
    app = Flask(__name__)

    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32)),
        SESSION_COOKIE_NAME="diff_session",  # Avoid conflict with auth app's "session" cookie
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    DEFAULT_ROOT = os.environ.get("DEFAULT_ROOT", "/home/evelyn")
    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 10 * 1024 * 1024))
    AI_REVIEW_COOLDOWN_SECONDS = max(0.0, float(os.environ.get("AI_REVIEW_COOLDOWN_SECONDS", "10")))
    AI_REVIEW_COOLDOWN_FILE = Path(
        os.environ.get("AI_REVIEW_COOLDOWN_FILE", "/tmp/diff-editor-ai-review-cooldown.txt")
    )
    AI_REVIEW_PROVIDER = os.environ.get("AI_REVIEW_PROVIDER", "codex_cli").strip().lower()
    AI_REVIEW_CACHE_DIR = Path(
        os.environ.get("AI_REVIEW_CACHE_DIR", "/tmp/diff-editor-ai-review-cache")
    )
    AI_REVIEW_CACHE_TTL_SECONDS = max(
        60.0,
        float(os.environ.get("AI_REVIEW_CACHE_TTL_SECONDS", str(24 * 60 * 60)))
    )

    def new_review_cache_key() -> str:
        """Generate a unique review id for a user-initiated review run."""
        return secrets.token_hex(8)

    REVIEW_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

    def normalize_review_id(raw_review_id: object) -> str | None:
        review_id = str(raw_review_id or "").strip()
        if not review_id:
            return None
        if not REVIEW_ID_PATTERN.fullmatch(review_id):
            return None
        return review_id

    def get_review_session_namespace() -> str:
        """Per-session namespace used to isolate review ids across users."""
        namespace = session.get("ai_review_namespace")
        if isinstance(namespace, str) and namespace:
            return namespace
        namespace = secrets.token_hex(12)
        session["ai_review_namespace"] = namespace
        return namespace

    def to_scoped_cache_key(review_id: str) -> str:
        """Map a client-facing review_id to a session-scoped cache key."""
        namespace = get_review_session_namespace()
        key_input = f"{namespace}:{review_id}"
        return hashlib.sha256(key_input.encode("utf-8")).hexdigest()[:32]

    def get_review_cache_paths(cache_key: str) -> tuple[Path, Path, Path, Path]:
        """Return (output_file, status_file, lock_file, pid_file) paths for a cache key."""
        AI_REVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return (
            AI_REVIEW_CACHE_DIR / f"{cache_key}.txt",
            AI_REVIEW_CACHE_DIR / f"{cache_key}.status",
            AI_REVIEW_CACHE_DIR / f"{cache_key}.lock",
            AI_REVIEW_CACHE_DIR / f"{cache_key}.pid",
        )

    def get_latest_review_index_path(file_path: str) -> Path:
        """Return path for latest-review index entry scoped by session + file path."""
        AI_REVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        namespace = get_review_session_namespace()
        key_input = f"{namespace}:{file_path}"
        digest = hashlib.sha256(key_input.encode("utf-8")).hexdigest()[:32]
        return AI_REVIEW_CACHE_DIR / f"latest-{digest}.txt"

    def set_latest_review_for_file(file_path: str, review_id: str):
        if not file_path or not review_id:
            return
        try:
            get_latest_review_index_path(file_path).write_text(review_id, encoding="utf-8")
        except Exception:
            pass

    def get_latest_review_for_file(file_path: str) -> str | None:
        if not file_path:
            return None
        path = get_latest_review_index_path(file_path)
        if not path.exists():
            return None
        try:
            value = path.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        return normalize_review_id(value)

    def clear_latest_review_for_file(file_path: str):
        if not file_path:
            return
        try:
            get_latest_review_index_path(file_path).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def purge_review_cache(cache_key: str, *, keep_lock: bool = False):
        """Delete cache artifacts for a review id."""
        output_file, status_file, lock_file, pid_file = get_review_cache_paths(cache_key)
        paths = [output_file, status_file, pid_file]
        if not keep_lock:
            paths.append(lock_file)
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def review_cache_age_seconds(cache_key: str, now: float | None = None) -> float | None:
        """Return cache age in seconds based on newest status/output mtime."""
        output_file, status_file, _, _ = get_review_cache_paths(cache_key)
        candidates: list[Path] = []
        if status_file.exists():
            candidates.append(status_file)
        if output_file.exists():
            candidates.append(output_file)
        if not candidates:
            return None
        try:
            newest_mtime = max(path.stat().st_mtime for path in candidates)
        except Exception:
            return None
        now_ts = time.time() if now is None else now
        return max(0.0, now_ts - newest_mtime)

    def is_review_cache_expired(cache_key: str, now: float | None = None) -> bool:
        age = review_cache_age_seconds(cache_key, now=now)
        if age is None:
            return False
        return age > AI_REVIEW_CACHE_TTL_SECONDS

    def cleanup_expired_review_cache():
        """Delete all cache entries older than the TTL."""
        try:
            AI_REVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            now_ts = time.time()
            review_ids: set[str] = set()
            for path in AI_REVIEW_CACHE_DIR.iterdir():
                if path.is_file():
                    if path.name.startswith("latest-"):
                        continue
                    review_ids.add(path.stem)
            for review_id in review_ids:
                if is_review_cache_expired(review_id, now=now_ts):
                    purge_review_cache(review_id)
        except Exception:
            # Best-effort cleanup only.
            pass

    def get_review_status(cache_key: str) -> str | None:
        """Get review status: 'running', 'completed', 'error', or None if not found."""
        _, status_file, _, _ = get_review_cache_paths(cache_key)
        if is_review_cache_expired(cache_key):
            purge_review_cache(cache_key)
            return None
        if status_file.exists():
            return status_file.read_text().strip()
        return None

    def set_review_status(cache_key: str, status: str):
        """Set review status."""
        _, status_file, _, _ = get_review_cache_paths(cache_key)
        status_file.write_text(status)

    def try_start_review(cache_key: str) -> tuple[bool, str | None]:
        """
        Atomically check if review exists and mark as starting if not.
        Returns (should_start, existing_status).
        Uses file locking to prevent race conditions across workers.

        Only "running" or "completed" status blocks new reviews.
        "cancelled" or "error" status allows regeneration.
        """
        output_file, status_file, lock_file, pid_file = get_review_cache_paths(cache_key)

        try:
            with lock_file.open("w") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)

                # Check status while holding lock
                if status_file.exists():
                    if is_review_cache_expired(cache_key):
                        purge_review_cache(cache_key, keep_lock=True)
                    elif status_file.exists():
                        existing = status_file.read_text().strip()
                        # Only block if running or completed - allow retry on error/cancelled
                        if existing in ("running", "completed"):
                            return False, existing
                        # Clear old failed/cancelled state files before restart
                        if output_file.exists():
                            output_file.unlink()
                        if pid_file.exists():
                            pid_file.unlink()

                # Mark as running while holding lock
                status_file.write_text("running")
                return True, None  # We should start the review
        except Exception:
            # On error, check status without lock
            if status_file.exists():
                existing = status_file.read_text().strip()
                if existing in ("running", "completed"):
                    return False, existing
            return True, None  # Assume we should start

    def run_review_in_background(
        cache_key: str,
        cmd: list[str],
        review_prompt: str,
        review_cwd: Path,
        review_case: str,
        debug_file_path: str,
        timeout: int,
    ):
        """Run codex review in background, writing output to cache file."""
        import datetime

        output_file, _, _, pid_file = get_review_cache_paths(cache_key)
        # Note: status already set to "running" by try_start_review()

        def set_status_if_not_cancelled(status: str):
            if get_review_status(cache_key) != "cancelled":
                set_review_status(cache_key, status)

        # Open files for writing
        debug_file = None
        try:
            debug_file = open(debug_file_path, "a")
            debug_file.write(f"\n{'=' * 60}\n")
            debug_file.write(f"=== AI Review Debug Log ===\n")
            debug_file.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
            debug_file.write(f"Cache key: {cache_key}\n")
            debug_file.write(f"Review case: {review_case}\n")
            debug_file.write(f"Working dir: {review_cwd}\n")
            debug_file.write(f"Command: {' '.join(cmd)}\n")
            debug_file.write(f"\n=== Input Prompt ===\n{review_prompt}\n")
            debug_file.write(f"\n=== Codex Events ===\n")
            debug_file.flush()
        except Exception:
            debug_file = None

        try:
            cache_file = open(output_file, "w")
        except Exception as e:
            set_status_if_not_cancelled("error")
            if debug_file:
                debug_file.write(f"ERROR: Failed to open cache output file: {str(e)}\n")
                debug_file.close()
            return

        def write_output(text: str):
            cache_file.write(text)
            cache_file.flush()

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(review_cwd),
            )
            # Save PID for cancellation support
            pid_file.write_text(str(process.pid))
            process.stdin.write(review_prompt)
            process.stdin.close()
        except FileNotFoundError:
            write_output("**Error:** codex command not found on server")
            set_status_if_not_cancelled("error")
            cache_file.close()
            if debug_file:
                debug_file.write("ERROR: codex command not found\n")
                debug_file.close()
            return
        except Exception as e:
            write_output(f"**Error:** Failed to start codex: {str(e)}")
            set_status_if_not_cancelled("error")
            cache_file.close()
            if debug_file:
                debug_file.write(f"ERROR: {str(e)}\n")
                debug_file.close()
            return

        # Watchdog for timeout
        timed_out = threading.Event()

        def watchdog():
            time.sleep(timeout)
            if process.poll() is None:
                timed_out.set()
                process.kill()

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        output_received = False
        last_reasoning = None

        try:
            for line in process.stdout:
                if timed_out.is_set():
                    break
                line = line.strip()
                if not line:
                    continue

                if debug_file:
                    debug_file.write(f"{line}\n")
                    debug_file.flush()

                try:
                    event = json.loads(line)
                    event_type = event.get("type")

                    if event_type == "item.completed":
                        item = event.get("item", {})
                        item_type = item.get("type")

                        if item_type == "reasoning":
                            text = item.get("text", "").strip()
                            if text and text != last_reasoning:
                                last_reasoning = text
                                write_output(f"{text}\n\n")

                        elif item_type == "command_execution":
                            write_output("*Done*\n\n")

                        elif item_type == "agent_message":
                            text = item.get("text", "")
                            if text:
                                output_received = True
                                write_output(f"\n---\n\n{text}")

                    elif event_type == "item.started":
                        item = event.get("item", {})
                        if item.get("type") == "command_execution":
                            cmd_text = item.get("command", "")
                            if cmd_text:
                                if "-lc " in cmd_text or "-c " in cmd_text:
                                    for flag in ["-lc ", "-c "]:
                                        if flag in cmd_text:
                                            after_flag = cmd_text.split(flag, 1)[1]
                                            if after_flag and after_flag[0] in "\"'":
                                                delim = after_flag[0]
                                                inner = after_flag[1:].split(delim)[0]
                                                cmd_text = inner
                                            break
                                if len(cmd_text) > 60:
                                    cmd_text = cmd_text[:57] + "..."
                                write_output(f"`{cmd_text}`... ")

                except json.JSONDecodeError:
                    continue

            process.wait(timeout=5)

            if timed_out.is_set():
                write_output("\n\n**Error:** Review timed out")
                set_status_if_not_cancelled("error")
                if debug_file:
                    debug_file.write("\n=== Result: TIMEOUT ===\n")
            elif process.returncode != 0:
                stderr = process.stderr.read() if process.stderr else ""
                write_output(f"\n\n**Error:** codex exited with code {process.returncode}: {stderr}")
                set_status_if_not_cancelled("error")
                if debug_file:
                    debug_file.write(f"\n=== Result: ERROR (exit {process.returncode}) ===\n{stderr}\n")
            elif not output_received:
                write_output("**Error:** codex returned no review output")
                set_status_if_not_cancelled("error")
                if debug_file:
                    debug_file.write("\n=== Result: NO OUTPUT ===\n")
            else:
                set_status_if_not_cancelled("completed")
                if debug_file:
                    debug_file.write("\n=== Result: SUCCESS ===\n")

        except Exception as e:
            process.kill()
            write_output(f"\n\n**Error:** {str(e)}")
            set_status_if_not_cancelled("error")
            if debug_file:
                debug_file.write(f"\n=== Result: EXCEPTION ===\n{str(e)}\n")
        finally:
            cache_file.close()
            if debug_file:
                debug_file.close()
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def stream_from_cache(cache_key: str):
        """Stream review output from cache file, following new content while running."""
        output_file, _, _, _ = get_review_cache_paths(cache_key)
        position = 0

        while True:
            status = get_review_status(cache_key)

            if output_file.exists():
                with open(output_file, "r") as f:
                    f.seek(position)
                    new_content = f.read()
                    if new_content:
                        position = f.tell()
                        yield new_content

            if status in ("completed", "error", "cancelled"):
                break
            elif status == "running":
                time.sleep(0.1)  # Poll interval
            else:
                # No status means no active review state to follow.
                break

    def consume_ai_review_cooldown() -> float:
        """
        Apply a global cooldown for AI review requests across gunicorn workers.
        Returns seconds remaining if currently on cooldown, else 0.0.
        """
        if AI_REVIEW_COOLDOWN_SECONDS <= 0:
            return 0.0

        try:
            AI_REVIEW_COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
            with AI_REVIEW_COOLDOWN_FILE.open("a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()

                now = time.time()
                last_run = float(raw) if raw else 0.0
                elapsed = now - last_run
                if elapsed < AI_REVIEW_COOLDOWN_SECONDS:
                    return AI_REVIEW_COOLDOWN_SECONDS - elapsed

                f.seek(0)
                f.truncate()
                f.write(f"{now:.6f}")
                f.flush()
                os.fsync(f.fileno())
                return 0.0
        except Exception:
            # Fail open on cooldown storage errors instead of blocking reviews.
            return 0.0

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'"
        )
        response.headers["Content-Security-Policy"] = csp
        return response

    @app.after_request
    def log_request(response):
        # Log in gunicorn-style format
        size = response.content_length or "-"
        access_logger.info(
            '%s - - [%s] "%s %s %s" %s %s "%s" "%s"',
            request.remote_addr or "-",
            time.strftime("%d/%b/%Y:%H:%M:%S %z"),
            request.method,
            request.path,
            request.environ.get("SERVER_PROTOCOL", "HTTP/1.1"),
            response.status_code,
            size,
            request.referrer or "-",
            request.user_agent.string or "-",
        )
        return response

    def generate_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]

    def validate_csrf_token(token: str) -> bool:
        return token and token == session.get("csrf_token")

    app.jinja_env.globals["csrf_token"] = generate_csrf_token
    app.jinja_env.filters["basename"] = lambda p: Path(p).name

    # Cache-busting for static files using content hash
    _static_hashes: dict[str, str] = {}

    def static_url(filename: str) -> str:
        """Generate a cache-busted URL for a static file using content hash."""
        if filename not in _static_hashes:
            static_path = Path(app.static_folder) / filename
            if static_path.exists():
                content = static_path.read_bytes()
                _static_hashes[filename] = hashlib.md5(content).hexdigest()[:8]
            else:
                _static_hashes[filename] = "unknown"
        return url_for("static", filename=filename) + "?h=" + _static_hashes[filename]

    app.jinja_env.globals["static_url"] = static_url

    @app.get("/")
    def index():
        # Allow ?path= parameter to override the default starting directory
        start_path = request.args.get("path", DEFAULT_ROOT)
        path, error = resolve_request_path(start_path, "path")
        # Fall back to default if path doesn't exist or isn't a directory
        if error or not path.exists() or not path.is_dir():
            start_path = DEFAULT_ROOT
        else:
            start_path = str(path)
        return render_template("index.html", default_root=start_path)

    @app.get("/diff")
    def diff_view():
        file_path = request.args.get("file", "")
        if not file_path:
            return render_template("index.html", default_root=DEFAULT_ROOT, error="No file specified")

        path, error = resolve_request_path(file_path, "file path")
        if error:
            return render_template("index.html", default_root=DEFAULT_ROOT, error=error)

        if not path.exists():
            return render_template("index.html", default_root=DEFAULT_ROOT, error=f"File not found: {file_path}")
        if not path.is_file():
            return render_template("index.html", default_root=DEFAULT_ROOT, error=f"Not a file: {file_path}")

        return render_template("diff.html", file_path=str(path))

    @app.get("/api/browse")
    def browse_directory():
        path_str = request.args.get("path", DEFAULT_ROOT)
        show_hidden = request.args.get("hidden", "false").lower() == "true"

        path, error = resolve_request_path(path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "Path does not exist"}), 404

        if not path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        # Get git status and tracked files for the directory
        git_status = get_directory_git_status(path)
        tracked_files = get_tracked_files(path)

        items = []
        try:
            for entry in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if not show_hidden and entry.name.startswith("."):
                    continue

                try:
                    is_dir = entry.is_dir()
                    entry_path = str(entry)
                    file_git_status = git_status.get(entry_path) if not is_dir else None
                    # Only mark as git-tracked if actually in git's index (not just in a repo folder)
                    is_tracked = entry_path in tracked_files if not is_dir else False

                    items.append({
                        "name": entry.name,
                        "path": entry_path,
                        "is_dir": is_dir,
                        "is_git": is_tracked,
                        "git_status": file_git_status,
                        "writable": is_writable_by_user(entry) if not is_dir else None,
                    })
                except PermissionError:
                    items.append({
                        "name": entry.name,
                        "path": str(entry),
                        "is_dir": False,
                        "is_git": False,
                        "git_status": None,
                        "writable": False,
                        "error": "Permission denied",
                    })
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403

        parent = str(path.parent) if path != path.parent else None

        return jsonify({
            "path": str(path),
            "parent": parent,
            "items": items,
        })

    @app.get("/api/file")
    def get_file():
        file_path = request.args.get("path", "")
        if not file_path:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(file_path, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "File not found"}), 404

        if not path.is_file():
            return jsonify({"error": "Not a file"}), 400

        if path.stat().st_size > MAX_FILE_SIZE:
            return jsonify({"error": f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)"}), 400

        success, content_bytes = read_file_bytes(path)
        if not success:
            return jsonify({"error": content_bytes}), 500

        image_mime = detect_image_mime(path, content_bytes)
        if image_mime:
            git_root = find_git_root(path)
            is_git_tracked = is_tracked_by_git(path, git_root) if git_root else False
            return jsonify({
                "path": str(path),
                "content": "",
                "original": "",
                "language": "plaintext",
                "is_image": True,
                "is_binary": False,
                "image_url": url_for("file_image", path=str(path)),
                "is_git": is_git_tracked,
                "writable": False,
            })

        git_root = find_git_root(path)
        original_bytes = content_bytes
        is_git_tracked = False

        if git_root:
            head_success, head_content = get_head_content_bytes(path, git_root)
            if head_success:
                original_bytes = head_content
                is_git_tracked = True

        is_binary = is_likely_binary(content_bytes) or is_likely_binary(original_bytes)
        content = bytes_to_hex_view(content_bytes) if is_binary else content_bytes.decode("utf-8", errors="replace")
        original_content = (
            bytes_to_hex_view(original_bytes) if is_binary else original_bytes.decode("utf-8", errors="replace")
        )

        suffix = path.suffix.lower()
        language_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".jsx": "javascript", ".tsx": "typescript", ".html": "html",
            ".css": "css", ".scss": "scss", ".json": "json", ".md": "markdown",
            ".yaml": "yaml", ".yml": "yaml", ".xml": "xml", ".sql": "sql",
            ".sh": "shell", ".bash": "shell", ".zsh": "shell",
            ".rs": "rust", ".go": "go", ".java": "java", ".c": "c",
            ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".rb": "ruby",
            ".php": "php", ".swift": "swift", ".kt": "kotlin",
            ".nginx": "nginx", ".conf": "ini", ".ini": "ini",
            ".toml": "toml", ".env": "dotenv", ".txt": "plaintext",
        }
        language = language_map.get(suffix)

        # Fallback: detect language from shebang if no extension match
        if not is_binary and not language and content:
            first_line = content.split("\n", 1)[0]
            if first_line.startswith("#!"):
                shebang_map = {
                    "python": "python", "python3": "python", "python2": "python",
                    "bash": "shell", "sh": "shell", "zsh": "shell", "fish": "shell",
                    "node": "javascript", "nodejs": "javascript",
                    "ruby": "ruby", "perl": "perl", "php": "php",
                    "lua": "lua", "awk": "shell", "sed": "shell",
                }
                # Extract interpreter: handle both /usr/bin/env X and /usr/bin/X
                parts = first_line[2:].strip().split()
                if parts:
                    interpreter = parts[-1] if parts[0].endswith("env") and len(parts) > 1 else parts[0]
                    interpreter = interpreter.split("/")[-1]  # Get basename
                    language = shebang_map.get(interpreter)

        language = "plaintext" if is_binary else (language or "plaintext")

        return jsonify({
            "path": str(path),
            "content": content,
            "original": original_content,
            "language": language,
            "is_image": False,
            "is_binary": is_binary,
            "is_git": is_git_tracked,
            "writable": is_writable_by_user(path),
        })

    @app.get("/api/file/image")
    def file_image():
        file_path = request.args.get("path", "")
        if not file_path:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(file_path, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "File not found"}), 404

        if not path.is_file():
            return jsonify({"error": "Not a file"}), 400

        if path.stat().st_size > MAX_FILE_SIZE:
            return jsonify({"error": f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)"}), 400

        success, content_bytes = read_file_bytes(path)
        if not success:
            return jsonify({"error": content_bytes}), 500

        image_mime = detect_image_mime(path, content_bytes)
        if not image_mime:
            return jsonify({"error": "Not an image file"}), 400

        return Response(
            content_bytes,
            mimetype=image_mime,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="{path.name}"',
            },
        )

    @app.post("/api/file/create")
    def create_file():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        directory = data.get("directory", "")
        name = str(data.get("name", "")).strip()

        if not directory:
            return jsonify({"error": "No directory specified"}), 400
        if not name:
            return jsonify({"error": "No file name provided"}), 400
        if "\x00" in name:
            return jsonify({"error": "Invalid file name"}), 400
        if "/" in name or "\\" in name:
            return jsonify({"error": "File name cannot include path separators"}), 400
        if name in {".", ".."}:
            return jsonify({"error": "Invalid file name"}), 400

        dir_path, error = resolve_request_path(directory, "directory")
        if error:
            return jsonify({"error": error}), 400
        if not dir_path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not dir_path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        target = (dir_path / name).resolve()
        if target.exists():
            return jsonify({"error": "File already exists"}), 409

        success, message = write_file_bytes(target, b"")
        if not success:
            return jsonify({"error": message}), 500

        return jsonify({
            "success": True,
            "message": "File created",
            "path": str(target),
        })

    @app.post("/api/file")
    def save_file():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        file_path = data.get("path", "")
        content = data.get("content")

        if not file_path:
            return jsonify({"error": "No path specified"}), 400
        if content is None:
            return jsonify({"error": "No content provided"}), 400

        path, error = resolve_request_path(file_path, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "File not found"}), 404

        read_ok, existing_bytes = read_file_bytes(path)
        if not read_ok:
            return jsonify({"error": existing_bytes}), 500

        if detect_image_mime(path, existing_bytes):
            return jsonify({"error": "Image files are preview-only"}), 400

        if is_likely_binary(existing_bytes):
            parse_ok, parsed_or_error = parse_hex_view(content)
            if not parse_ok:
                return jsonify({"error": parsed_or_error}), 400
            success, message = write_file_bytes(path, parsed_or_error)
        else:
            success, message = write_file(path, content)

        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"error": message}), 500

    @app.get("/api/ai-review")
    def ai_review_stream_existing():
        """Stream an existing AI review by review_id without starting a new run."""
        cleanup_expired_review_cache()

        review_id = normalize_review_id(request.args.get("review_id"))
        if not review_id:
            return jsonify({"error": "No review_id provided"}), 400

        cache_key = to_scoped_cache_key(review_id)
        status = get_review_status(cache_key)
        if status is None:
            return jsonify({"error": "Review not found or expired"}), 404

        return Response(
            stream_from_cache(cache_key),
            mimetype="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-AI-Review-Id": review_id,
                "X-AI-Review-Status": status,
            },
        )

    @app.get("/api/ai-review/status")
    def ai_review_status():
        """Return status for an existing AI review id."""
        cleanup_expired_review_cache()

        review_id = normalize_review_id(request.args.get("review_id"))
        if not review_id:
            return jsonify({"error": "No review_id provided"}), 400

        cache_key = to_scoped_cache_key(review_id)
        status = get_review_status(cache_key)
        if status is None:
            return jsonify({"error": "Review not found or expired"}), 404

        return jsonify({"review_id": review_id, "status": status})

    @app.get("/api/ai-review/latest")
    def ai_review_latest():
        """Return latest known review id for a file in the current session scope."""
        cleanup_expired_review_cache()

        file_path = str(request.args.get("file_path", "")).strip()
        if not file_path:
            return jsonify({"error": "No file_path provided"}), 400

        review_id = get_latest_review_for_file(file_path)
        if not review_id:
            return jsonify({"error": "No saved review found"}), 404

        cache_key = to_scoped_cache_key(review_id)
        status = get_review_status(cache_key)
        if status is None:
            clear_latest_review_for_file(file_path)
            return jsonify({"error": "Review not found or expired"}), 404

        return jsonify({"review_id": review_id, "status": status})

    @app.post("/api/ai-review")
    def ai_review():
        """Get AI review of code changes using Codex CLI or OpenAI SDK."""
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        original = data.get("original", "")
        modified = data.get("modified", "")
        file_path = data.get("file_path", "unknown")
        language = data.get("language", "plaintext")
        requested_review_id = normalize_review_id(data.get("review_id"))

        if data.get("review_id") and not requested_review_id:
            return jsonify({"error": "Invalid review_id"}), 400

        if original == modified:
            return jsonify({"error": "No changes to review"}), 400

        cleanup_expired_review_cache()

        # This request would start a new review run, so apply cooldown.
        cooldown_remaining = consume_ai_review_cooldown()
        if cooldown_remaining > 0:
            retry_after = max(1, math.ceil(cooldown_remaining))
            response = jsonify({
                "error": f"AI review is on cooldown. Try again in {retry_after}s.",
                "retry_after_seconds": retry_after,
            })
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response

        review_cwd = choose_ai_review_cwd(file_path)

        # Generate unified diff; keep codex context short and let it read files directly.
        context_lines = 5 if AI_REVIEW_PROVIDER != "openai_sdk" else 200
        original_lines = original.splitlines(keepends=True)
        modified_lines = modified.splitlines(keepends=True)
        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{Path(file_path).name}",
            tofile=f"b/{Path(file_path).name}",
            n=context_lines,
        )
        unified_diff = "".join(diff)

        if not unified_diff.strip():
            return jsonify({"error": "No changes to review"}), 400

        # Determine review case based on git status and unsaved changes
        # Case 2: No uncommitted changes, user edited → send diff, --skip-git-repo-check
        # Case 3: Uncommitted changes, no further edits → use --uncommitted, no diff
        # Case 4: Uncommitted + user edited → send diff, instruct to use git show HEAD
        resolved_path, _ = resolve_request_path(file_path, "file_path")
        git_root = find_git_root(resolved_path) if resolved_path else None
        is_git_tracked = git_root and resolved_path and is_tracked_by_git(resolved_path, git_root)

        review_case = "non_git"  # Default: not in git, send diff with --skip-git-repo-check
        if is_git_tracked and resolved_path:
            success, disk_content = read_file_bytes(resolved_path)
            if success and isinstance(disk_content, bytes):
                try:
                    disk_text = disk_content.decode("utf-8")
                except UnicodeDecodeError:
                    disk_text = None

                if disk_text is not None:
                    if modified == disk_text:
                        # Editor matches disk → Case 3: uncommitted changes only
                        review_case = "uncommitted_only"
                    elif original == disk_text:
                        # Disk matches HEAD → Case 2: user edits only (no uncommitted)
                        review_case = "user_edits_only"
                    else:
                        # Disk differs from both HEAD and editor → Case 4
                        review_case = "uncommitted_plus_edits"

        # Build prompt based on case
        if review_case == "uncommitted_only":
            # Case 3: No diff needed, tell codex to review uncommitted changes
            review_prompt = f"Review uncommitted changes for `{file_path}` ({language}). Use `git diff` to see what changed."
        elif review_case == "uncommitted_plus_edits":
            # Case 4: Diff needed + instruction to check HEAD
            review_prompt = f"""Review this diff for `{file_path}` ({language}).

Note: The file on disk has uncommitted changes not reflected in this diff.
This diff shows changes from HEAD. Use `git show HEAD:{file_path}` to see the baseline.

```diff
{unified_diff}
```"""
        else:
            # Cases 2 and non_git: Simple diff prompt
            review_prompt = f"""Review this diff for `{file_path}` ({language}):

```diff
{unified_diff}
```"""

        if AI_REVIEW_PROVIDER == "openai_sdk":
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return jsonify({"error": "OpenAI API key not configured"}), 500

            def generate_openai():
                try:
                    client = OpenAI(api_key=api_key)
                    stream = client.responses.create(
                        model="gpt-5.2-codex",
                        reasoning={"effort": "medium"},
                        input=review_prompt,
                        stream=True,
                    )
                    for event in stream:
                        if event.type == "response.output_text.delta":
                            yield event.delta
                except Exception as e:
                    yield f"\n\n**Error:** {str(e)}"

            return Response(
                generate_openai(),
                mimetype="text/plain",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",  # Disable nginx buffering
                },
            )

        # Each user request gets a unique review id. We only stream/reuse for this exact run.
        cache_key = ""
        review_id_for_response = requested_review_id or ""
        if requested_review_id:
            cache_key = to_scoped_cache_key(requested_review_id)
            should_start, _ = try_start_review(cache_key)
            if not should_start:
                return jsonify({"error": "Review session already exists"}), 409
        else:
            for _ in range(10):
                candidate = new_review_cache_key()
                candidate_key = to_scoped_cache_key(candidate)
                should_start, _ = try_start_review(candidate_key)
                if should_start:
                    cache_key = candidate_key
                    review_id_for_response = candidate
                    break

        if not cache_key:
            return jsonify({"error": "Failed to initialize review session"}), 500

        canonical_file_path = str(resolved_path) if resolved_path else str(file_path)
        set_latest_review_for_file(canonical_file_path, review_id_for_response)

        CODEX_TIMEOUT = int(os.environ.get("AI_REVIEW_TIMEOUT", "900"))  # 15 minutes
        CODEX_DEBUG_FILE = os.environ.get(
            "AI_REVIEW_DEBUG_FILE",
            str(Path(__file__).parent / "logs" / "ai-review-debug.log")
        )

        # Build command based on review case
        cmd = ["codex", "exec", "review", "-m", "gpt-5.3-codex", "--json"]

        if review_case in ("uncommitted_only", "uncommitted_plus_edits"):
            # Cases 3 & 4: Let codex use git context, read prompt from stdin
            cmd.append("-")
        else:
            # Cases 2 and non_git: Skip git repo check, read prompt from stdin
            cmd.extend(["--skip-git-repo-check", "-"])

        # Start review in background thread
        review_thread = threading.Thread(
            target=run_review_in_background,
            args=(cache_key, cmd, review_prompt, review_cwd, review_case, CODEX_DEBUG_FILE, CODEX_TIMEOUT),
            daemon=True,
        )
        review_thread.start()

        # Stream from cache file
        response = Response(
            stream_from_cache(cache_key),
            mimetype="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
                "X-AI-Review-Id": review_id_for_response,
                "X-AI-Review-Status": "running",
            },
        )
        return response

    @app.post("/api/ai-review/cancel")
    def ai_review_cancel():
        """Cancel a running AI review by sending SIGINT to the subprocess."""
        import signal

        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        cleanup_expired_review_cache()

        raw_review_id = data.get("review_id")
        review_id = normalize_review_id(raw_review_id)
        if raw_review_id and not review_id:
            return jsonify({"error": "Invalid review_id"}), 400
        if not review_id:
            return jsonify({"error": "No review_id provided"}), 400

        cache_key = to_scoped_cache_key(review_id)
        _, _, _, pid_file = get_review_cache_paths(cache_key)
        status = get_review_status(cache_key)

        if status != "running":
            return jsonify({"error": "No running review found"}), 404

        if not pid_file.exists():
            set_review_status(cache_key, "error")
            return jsonify({"error": "No running review found"}), 404

        try:
            pid = int(pid_file.read_text().strip())

            # Verify this still looks like a codex review process before signaling.
            proc_cmdline = Path(f"/proc/{pid}/cmdline")
            if proc_cmdline.exists():
                cmdline = proc_cmdline.read_bytes().decode("utf-8", errors="ignore")
                if "codex" not in cmdline or "review" not in cmdline:
                    set_review_status(cache_key, "error")
                    try:
                        pid_file.unlink()
                    except FileNotFoundError:
                        pass
                    return jsonify({"error": "No running review found"}), 404

            os.kill(pid, signal.SIGINT)
            set_review_status(cache_key, "cancelled")
            return jsonify({"success": True, "message": "Review cancelled"})
        except ProcessLookupError:
            # Process already finished
            set_review_status(cache_key, "cancelled")
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
            return jsonify({"success": True, "message": "Process already finished"})
        except ValueError:
            set_review_status(cache_key, "error")
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
            return jsonify({"error": "Invalid PID in file"}), 500
        except PermissionError:
            return jsonify({"error": "Permission denied to kill process"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=8005)
