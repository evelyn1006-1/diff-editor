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

        if original == modified:
            return jsonify({"error": "No changes to review"}), 400

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

        CODEX_TIMEOUT = int(os.environ.get("AI_REVIEW_TIMEOUT", "120"))
        CODEX_DEBUG_FILE = os.environ.get("AI_REVIEW_DEBUG_FILE", "/tmp/diff-editor-ai-review-debug.log")

        cmd = [
            "codex",
            "exec",
            "review",
            "-m",
            "gpt-5.3-codex",
            "--skip-git-repo-check",
            "--json",
            "-",  # Read prompt from stdin
        ]

        def generate_codex():
            # Open debug file for logging
            import datetime
            debug_file = None
            try:
                debug_file = open(CODEX_DEBUG_FILE, "w")
                debug_file.write(f"=== AI Review Debug Log ===\n")
                debug_file.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
                debug_file.write(f"Working dir: {review_cwd}\n")
                debug_file.write(f"Command: {' '.join(cmd)}\n")
                debug_file.write(f"\n=== Input Prompt ===\n{review_prompt}\n")
                debug_file.write(f"\n=== Codex Events ===\n")
                debug_file.flush()
            except Exception:
                debug_file = None  # Continue without debug logging

            try:
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(review_cwd),
                )
                process.stdin.write(review_prompt)
                process.stdin.close()
            except FileNotFoundError:
                if debug_file:
                    debug_file.write("ERROR: codex command not found\n")
                    debug_file.close()
                yield "**Error:** codex command not found on server"
                return
            except Exception as e:
                if debug_file:
                    debug_file.write(f"ERROR: {str(e)}\n")
                    debug_file.close()
                yield f"**Error:** Failed to start codex: {str(e)}"
                return

            # Watchdog thread to enforce timeout
            timed_out = threading.Event()

            def watchdog():
                time.sleep(CODEX_TIMEOUT)
                if process.poll() is None:  # Still running
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

                    # Log raw event to debug file
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
                                # Show reasoning (dedupe consecutive identical ones)
                                text = item.get("text", "").strip()
                                if text and text != last_reasoning:
                                    last_reasoning = text
                                    yield f"*{text}*\n\n"

                            elif item_type == "command_execution":
                                # Command finished
                                yield "*done*\n\n"

                            elif item_type == "agent_message":
                                text = item.get("text", "")
                                if text:
                                    output_received = True
                                    yield f"\n---\n\n{text}"

                        elif event_type == "item.started":
                            item = event.get("item", {})
                            if item.get("type") == "command_execution":
                                cmd_text = item.get("command", "")
                                if cmd_text:
                                    # Strip bash wrapper: /bin/bash -lc 'cmd' or "cmd"
                                    if "-lc " in cmd_text or "-c " in cmd_text:
                                        # Find position after -c/-lc and get the quoted content
                                        for flag in ["-lc ", "-c "]:
                                            if flag in cmd_text:
                                                after_flag = cmd_text.split(flag, 1)[1]
                                                # First char should be quote
                                                if after_flag and after_flag[0] in "\"'":
                                                    delim = after_flag[0]
                                                    # Extract content between quotes
                                                    inner = after_flag[1:].split(delim)[0]
                                                    cmd_text = inner
                                                break
                                    # Truncate long commands
                                    if len(cmd_text) > 60:
                                        cmd_text = cmd_text[:57] + "..."
                                    yield f"`{cmd_text}`... "

                    except json.JSONDecodeError:
                        continue

                process.wait(timeout=5)

                if timed_out.is_set():
                    if debug_file:
                        debug_file.write("\n=== Result: TIMEOUT ===\n")
                    yield "\n\n**Error:** Review timed out"
                elif process.returncode != 0:
                    stderr = process.stderr.read() if process.stderr else ""
                    if debug_file:
                        debug_file.write(f"\n=== Result: ERROR (exit {process.returncode}) ===\n{stderr}\n")
                    yield f"\n\n**Error:** codex exited with code {process.returncode}: {stderr}"
                elif not output_received:
                    if debug_file:
                        debug_file.write("\n=== Result: NO OUTPUT ===\n")
                    yield "**Error:** codex returned no review output"
                else:
                    if debug_file:
                        debug_file.write("\n=== Result: SUCCESS ===\n")

            except Exception as e:
                process.kill()
                if debug_file:
                    debug_file.write(f"\n=== Result: EXCEPTION ===\n{str(e)}\n")
                yield f"\n\n**Error:** {str(e)}"
            finally:
                if debug_file:
                    debug_file.close()

        return Response(
            generate_codex(),
            mimetype="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=8005)
