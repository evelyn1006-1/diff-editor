"""
Diff Editor - A web-based side-by-side file diff and editing tool.
"""

import difflib
import fcntl
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, url_for, Response, send_file
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

from utils.file_ops import read_file_bytes, write_file, write_file_bytes, is_writable_by_user, create_dir, create_symlink, ensure_directory, copy_directory, copy_file, make_executable, delete_path, delete_directory, rename_path, zip_directory, human_size, stat_path, read_file_head, count_lines, get_directory_info
from utils.git_ops import find_git_root, get_head_content_bytes, is_tracked_by_git, get_directory_git_status, get_tracked_files
from utils.constants import COMPILE_LANGUAGE_CONFIGS


TEXT_CONTROL_WHITESPACE_BYTES = {7, 8, 9, 10, 11, 12, 13, 27}

EDITOR_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".html": "html",
    ".css": "css", ".scss": "scss", ".json": "json", ".md": "markdown",
    ".yaml": "yaml", ".yml": "yaml", ".xml": "xml", ".sql": "sql",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".h": "c", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rb": "ruby", ".pl": "perl", ".pm": "perl", ".t": "perl",
    ".cs": "csharp", ".csx": "csharp",
    ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".nginx": "nginx", ".conf": "ini", ".ini": "ini",
    ".toml": "toml", ".env": "dotenv", ".txt": "plaintext",
    ".bf": "brainfuck", ".mag": "magma",
}

COMPILE_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".cs": "csharp",
}

SHEBANG_LANGUAGE_MAP = {
    "python": "python", "python3": "python", "python2": "python",
    "bash": "shell", "sh": "shell", "zsh": "shell", "fish": "shell",
    "node": "javascript", "nodejs": "javascript",
    "ruby": "ruby", "perl": "perl", "perl5": "perl", "php": "php",
    "lua": "lua", "awk": "shell", "sed": "shell",
    "bf": "brainfuck", "magma": "magma",
}

GO_PACKAGE_PATTERN = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\b", re.MULTILINE)
JAVA_PACKAGE_PATTERN = re.compile(
    r"^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;",
    re.MULTILINE,
)
JAVA_MAIN_METHOD_PATTERN = re.compile(r"\bstatic\s+void\s+main\s*\(")


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
        # Use abspath instead of resolve() to avoid following symlinks.
        # This still normalizes ".." for path traversal prevention.
        return Path(os.path.abspath(raw_path)), None
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


IMAGE_EXIF_ORIENTATION_LABELS = {
    1: "Normal",
    2: "Mirrored horizontally",
    3: "Rotated 180 deg",
    4: "Mirrored vertically",
    5: "Mirrored horizontally, rotated 90 deg CW",
    6: "Rotated 90 deg CW",
    7: "Mirrored horizontally, rotated 90 deg CCW",
    8: "Rotated 90 deg CCW",
}


def parse_optional_float(value: object) -> float | None:
    """Parse a numeric value, returning None for empty or invalid inputs."""
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_int(value: object) -> int | None:
    """Parse an integer-like value, returning None for empty or invalid inputs."""
    parsed = parse_optional_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_ffprobe_rate(value: object) -> float | None:
    """Parse ffprobe rate values like 30000/1001 or 24."""
    if value in (None, "", "N/A", "0/0"):
        return None
    if isinstance(value, str) and "/" in value:
        num, den = value.split("/", 1)
        numerator = parse_optional_float(num)
        denominator = parse_optional_float(den)
        if numerator is None or denominator in (None, 0):
            return None
        return numerator / denominator
    return parse_optional_float(value)


def parse_pdf_version(data: bytes | None) -> str | None:
    """Extract a PDF version like 1.7 from the file header."""
    if not data:
        return None
    match = re.search(rb"%PDF-(\d+(?:\.\d+)?)", data[:32])
    if not match:
        return None
    return match.group(1).decode("ascii", errors="ignore")


def normalize_metadata_text(value: object) -> str | None:
    """Return a JSON-safe metadata string, dropping empty values."""
    if value in (None, ""):
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.strip()
    return text or None


def get_image_orientation_label(img) -> str | None:
    """Return a human-readable EXIF orientation label when available."""
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None
    return IMAGE_EXIF_ORIENTATION_LABELS.get(exif.get(274))


def build_image_info(img) -> dict:
    """Collect compact, high-value metadata from a Pillow image object."""
    try:
        bands = tuple(img.getbands() or ())
    except Exception:
        bands = ()

    return {
        "width": img.width,
        "height": img.height,
        "mode": img.mode,
        "format": img.format,
        "has_alpha": ("A" in bands) or ("transparency" in getattr(img, "info", {})),
        "orientation": get_image_orientation_label(img),
        "frame_count": max(1, int(getattr(img, "n_frames", 1) or 1)),
    }


def build_pdf_info(reader, head: bytes | None) -> dict:
    """Collect compact PDF metadata from a pypdf reader."""
    pdf_info: dict = {
        "encrypted": bool(reader.is_encrypted),
        "version": parse_pdf_version(head),
        "title": None,
        "author": None,
        "page_width_pt": None,
        "page_height_pt": None,
    }

    try:
        metadata = reader.metadata or {}
        if metadata:
            title = getattr(metadata, "title", None)
            author = getattr(metadata, "author", None)
            if title is None and hasattr(metadata, "get"):
                title = metadata.get("/Title")
            if author is None and hasattr(metadata, "get"):
                author = metadata.get("/Author")
            pdf_info["title"] = normalize_metadata_text(title)
            pdf_info["author"] = normalize_metadata_text(author)
    except Exception:
        pass

    if pdf_info["encrypted"]:
        return pdf_info

    try:
        if reader.pages:
            first_page = reader.pages[0]
            pdf_info["page_width_pt"] = parse_optional_float(first_page.mediabox.width)
            pdf_info["page_height_pt"] = parse_optional_float(first_page.mediabox.height)
    except Exception:
        pass

    return pdf_info


def build_media_info(ffprobe_data: dict) -> dict:
    """Collect compact media metadata from ffprobe output."""
    streams = ffprobe_data.get("streams", [])
    format_info = ffprobe_data.get("format", {})
    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and (stream.get("disposition") or {}).get("attached_pic") not in (1, "1", True)
    ]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]

    video_stream = video_streams[0] if video_streams else None
    audio_stream = audio_streams[0] if audio_streams else None

    bit_rate = (
        parse_optional_int(format_info.get("bit_rate"))
        or parse_optional_int(video_stream.get("bit_rate") if video_stream else None)
        or parse_optional_int(audio_stream.get("bit_rate") if audio_stream else None)
    )

    return {
        "is_audio_only": video_stream is None and audio_stream is not None,
        "width": parse_optional_int(video_stream.get("width") if video_stream else None),
        "height": parse_optional_int(video_stream.get("height") if video_stream else None),
        "duration": parse_optional_float(format_info.get("duration")),
        "video_codec": video_stream.get("codec_name") if video_stream else None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "container": format_info.get("format_long_name") or format_info.get("format_name"),
        "bit_rate": bit_rate,
        "frame_rate": parse_ffprobe_rate(
            video_stream.get("avg_frame_rate") if video_stream else None
        ) or parse_ffprobe_rate(video_stream.get("r_frame_rate") if video_stream else None),
        "sample_rate": parse_optional_int(audio_stream.get("sample_rate") if audio_stream else None),
        "channels": parse_optional_int(audio_stream.get("channels") if audio_stream else None),
        "channel_layout": audio_stream.get("channel_layout") if audio_stream else None,
        "audio_tracks": len(audio_streams),
        "subtitle_tracks": len(subtitle_streams),
    }


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


def detect_language_for_path(
    path: Path,
    *,
    content: str | None = None,
    is_binary: bool = False,
) -> str:
    """Best-effort language detection shared by editor and compile helpers."""
    language = EDITOR_LANGUAGE_BY_SUFFIX.get(path.suffix.lower())

    if not is_binary and not language and content:
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("#!"):
            parts = first_line[2:].strip().split()
            if parts:
                interpreter = parts[-1] if parts[0].endswith("env") and len(parts) > 1 else parts[0]
                interpreter = interpreter.split("/")[-1]
                language = SHEBANG_LANGUAGE_MAP.get(interpreter)

    return "plaintext" if is_binary else (language or "plaintext")


def _default_output_basename(path: Path) -> str:
    stem = path.stem.strip()
    if stem:
        return stem
    name = path.name.lstrip(".").strip()
    return name or "output"


def get_compile_config_for_path(path: Path) -> dict[str, object] | None:
    """Return compile metadata for source files supported by the browser compiler."""
    language = COMPILE_LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
    if not language:
        return None

    language_config = COMPILE_LANGUAGE_CONFIGS.get(language)
    if not language_config:
        return None

    default_extension = str(language_config.get("default_extension") or "")
    default_name = _default_output_basename(path) + default_extension

    return {
        "language": language,
        "label": language_config["label"],
        "default_directory": str(path.parent),
        "default_name": default_name,
        "supports_optimization": bool(language_config.get("supports_optimization")),
        "optimization_label": language_config.get("optimization_label"),
        "supports_warnings": bool(language_config.get("supports_warnings")),
        "warning_label": language_config.get("warning_label"),
    }


def _read_compile_source_text(path: Path, *, max_bytes: int | None = None) -> tuple[str | None, str | None]:
    """Read compile-target source text with the same sudo-aware file access as the editor."""
    if max_bytes is None:
        success, data = read_file_bytes(path)
    else:
        success, data = read_file_head(path, max_bytes=max_bytes)

    if not success:
        return None, str(data)

    content_bytes = data if isinstance(data, bytes) else str(data).encode("utf-8", errors="replace")
    if is_likely_binary(content_bytes):
        return None, "Source file does not look like text"

    return content_bytes.decode("utf-8", errors="replace"), None


def _extract_go_package_name(source_text: str) -> str | None:
    match = GO_PACKAGE_PATTERN.search(source_text or "")
    return match.group(1) if match else None


def _extract_java_package_name(source_text: str) -> str | None:
    match = JAVA_PACKAGE_PATTERN.search(source_text or "")
    return match.group(1) if match else None


def _build_go_compile_context(path: Path, source_text: str) -> tuple[dict[str, object] | None, str | None, int]:
    package_name = _extract_go_package_name(source_text)
    if not package_name:
        return None, "Unable to detect the Go package declaration.", 400
    if package_name != "main":
        return None, "Go browser compilation currently supports only package main files.", 400

    input_paths: list[str] = []
    for sibling in sorted(path.parent.glob("*.go")):
        if not sibling.is_file() or sibling.name.endswith("_test.go"):
            continue

        sibling_text, error = _read_compile_source_text(sibling, max_bytes=8192)
        if error:
            return None, f"Failed to inspect sibling Go file {sibling.name}: {error}", 500

        if _extract_go_package_name(sibling_text or "") == package_name:
            input_paths.append(str(sibling))

    if not input_paths:
        input_paths.append(str(path))

    source_count = len(input_paths)
    file_label = "file" if source_count == 1 else "files"
    return {
        "go_input_paths": input_paths,
        "artifact_note": f"Builds package main using {source_count} Go {file_label} from this directory.",
    }, None, 200


def _build_java_compile_context(path: Path, source_text: str) -> dict[str, object]:
    package_name = _extract_java_package_name(source_text)
    has_main_method = bool(JAVA_MAIN_METHOD_PATTERN.search(source_text or ""))
    main_class = None
    artifact_note = "Produces a JAR of compiled classes without a Main-Class manifest."

    if has_main_method:
        main_class = f"{package_name}.{path.stem}" if package_name else path.stem
        artifact_note = f"Produces a runnable JAR with Main-Class {main_class}."

    return {
        "java_package": package_name,
        "java_main_class": main_class,
        "artifact_note": artifact_note,
    }


def get_compile_context_for_path(path: Path) -> tuple[dict[str, object] | None, str | None, int]:
    """Return compile metadata plus language-specific compile planning details."""
    if path.suffix.lower() == ".csx":
        return None, "C# script files (.csx) are not supported by browser compilation.", 400

    compile_config = get_compile_config_for_path(path)
    if not compile_config:
        return None, "This file type is not compilable from the browser", 400

    compile_context = dict(compile_config)
    language = str(compile_context["language"])

    if language == "go":
        source_text, error = _read_compile_source_text(path)
        if error:
            return None, error, 500

        go_context, go_error, go_status = _build_go_compile_context(path, source_text or "")
        if go_error:
            return None, go_error, go_status
        compile_context.update(go_context or {})
    elif language == "java":
        source_text, error = _read_compile_source_text(path)
        if error:
            return None, error, 500
        compile_context.update(_build_java_compile_context(path, source_text or ""))

    return compile_context, None, 200


def build_compile_success_message(
    source_path: Path,
    target_path: Path,
    compile_context: dict[str, object],
) -> str:
    """Return a user-facing success message for a completed compile action."""
    language = str(compile_context.get("language") or "")
    if language == "java":
        if compile_context.get("java_main_class"):
            return f"Compiled {source_path.name} to runnable JAR {target_path.name}"
        return f"Compiled {source_path.name} to JAR {target_path.name} (no Main-Class manifest)"
    return f"Compiled {source_path.name} to {target_path.name}"


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


def command_exists(command: str) -> bool:
    """Return whether a command is currently available on PATH."""
    return shutil.which(command) is not None


def find_first_command(*commands: str) -> str | None:
    """Return the first command found on PATH from the provided candidates."""
    for command in commands:
        if command_exists(command):
            return command
    return None


def has_dotnet_sdk() -> bool:
    """Check for a usable dotnet SDK, not just the runtime host."""
    if not command_exists("dotnet"):
        return False

    try:
        result = subprocess.run(
            ["dotnet", "--list-sdks"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return bool(result.stdout.strip())


COMPILE_TOOLING_CACHE: dict[str, dict[str, object]] = {}
COMPILE_TOOLING_CACHE_LOCK = threading.Lock()
RUN_TOOLING_CACHE: dict[str, dict[str, object]] = {}
RUN_TOOLING_CACHE_LOCK = threading.Lock()


def compute_compile_tooling_status(language: str) -> tuple[dict[str, object], int]:
    """Return compile-tooling availability for browser-compilable languages."""
    normalized = (language or "").strip().lower()
    if not normalized:
        return {"error": "No language specified"}, 400

    if normalized == "c":
        compiler = find_first_command("gcc", "cc")
        if compiler:
            return {"available": True, "compiler": compiler}, 200
        return {
            "available": False,
            "error": "C compilation requires gcc.",
            "install_command": "sudo apt update && sudo apt install gcc",
        }, 200

    if normalized == "cpp":
        compiler = find_first_command("g++", "c++")
        if compiler:
            return {"available": True, "compiler": compiler}, 200
        return {
            "available": False,
            "error": "C++ compilation requires g++.",
            "install_command": "sudo apt update && sudo apt install g++",
        }, 200

    if normalized == "go":
        if command_exists("go"):
            return {"available": True, "compiler": "go"}, 200
        return {
            "available": False,
            "error": "Go compilation requires the Go toolchain.",
            "install_command": "sudo apt update && sudo apt install golang-go",
        }, 200

    if normalized == "java":
        javac = find_first_command("javac")
        jar = find_first_command("jar")
        if javac and jar:
            return {"available": True, "compiler": javac, "archiver": jar}, 200
        return {
            "available": False,
            "error": "Java compilation requires javac and jar from a JDK.",
            "install_command": "sudo apt update && sudo apt install default-jdk",
        }, 200

    if normalized == "rust":
        if command_exists("rustc"):
            return {"available": True, "compiler": "rustc"}, 200
        return {
            "available": False,
            "error": "Rust compilation requires rustc.",
            "install_command": "sudo apt update && sudo apt install rustc cargo",
        }, 200

    if normalized == "csharp":
        compiler = find_first_command("csc", "mono-csc", "cli-csc", "mcs")
        if compiler:
            return {"available": True, "compiler": compiler}, 200
        return {
            "available": False,
            "error": "C# compilation requires csc or Mono's mcs.",
            "install_command": "sudo apt update && sudo apt install mono-mcs mono-devel",
        }, 200

    return {"error": "Unsupported language"}, 400


def get_compile_tooling_status(language: str) -> tuple[dict[str, object], int]:
    """
    Return compile-tooling availability for browser-compilable languages.

    Successful detections are cached in-process until app restart. Missing-tool
    results are recomputed so newly installed compilers become visible without a
    worker restart.
    """
    normalized = (language or "").strip().lower()
    if not normalized:
        return {"error": "No language specified"}, 400

    with COMPILE_TOOLING_CACHE_LOCK:
        cached = COMPILE_TOOLING_CACHE.get(normalized)
    if cached is not None:
        return dict(cached), 200

    status, http_status = compute_compile_tooling_status(normalized)
    if http_status == 200 and status.get("available") is True:
        with COMPILE_TOOLING_CACHE_LOCK:
            COMPILE_TOOLING_CACHE[normalized] = dict(status)
    return status, http_status


def _combine_subprocess_output(*chunks: str | None) -> str:
    """Join stdout/stderr fragments into a single readable message."""
    return "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())


def _get_compile_input_paths(source_path: Path, compile_context: dict[str, object]) -> list[Path]:
    """Return the source paths a compile action reads from disk."""
    language = str(compile_context.get("language") or "")
    if language == "go":
        return [
            Path(str(path))
            for path in (compile_context.get("go_input_paths") or [])
            if str(path).strip()
        ] or [source_path]
    return [source_path]


def _compile_requires_sudo(
    source_path: Path,
    target_path: Path,
    compile_context: dict[str, object],
) -> bool:
    """Use sudo when compile inputs or the output location need elevated access."""
    input_paths = _get_compile_input_paths(source_path, compile_context)
    if any(not os.access(path, os.R_OK) for path in input_paths):
        return True
    return not is_writable_by_user(target_path)


def _run_compile_command(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    use_sudo: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a compile command, optionally through passwordless sudo."""
    run_cmd = ["sudo", "-n", *cmd] if use_sudo else cmd
    run_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if use_sudo:
        run_kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(run_cmd, **run_kwargs)


def _prepare_sudo_tempdir_for_cleanup(path: Path) -> None:
    """Relax sudo-created temp files so TemporaryDirectory cleanup can remove them."""
    try:
        subprocess.run(
            ["sudo", "-n", "chmod", "-R", "a+rwX", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def compile_source_file(
    source_path: Path,
    target_path: Path,
    compile_context: dict[str, object],
    tooling_status: dict[str, object],
    *,
    optimize: bool = False,
    warnings: bool = False,
) -> tuple[bool, str]:
    """Compile a source file to the requested output path."""
    language = str(compile_context.get("language") or "")
    compiler = str(tooling_status.get("compiler") or "")
    timeout = 300
    use_sudo = _compile_requires_sudo(source_path, target_path, compile_context)

    try:
        if language == "java":
            archiver = str(tooling_status.get("archiver") or "")
            main_class = str(compile_context.get("java_main_class") or "")
            if not compiler or not archiver:
                return False, "Java compilation requires javac and jar."

            temp_dir_obj = tempfile.TemporaryDirectory(prefix="diff-editor-java-")
            temp_dir = Path(temp_dir_obj.name)
            try:
                classes_dir = temp_dir / "classes"
                classes_dir.mkdir(parents=True, exist_ok=True)

                javac_cmd = [compiler]
                if warnings:
                    javac_cmd.append("-Xlint")
                javac_cmd.extend(["-d", str(classes_dir), str(source_path)])

                javac_result = _run_compile_command(
                    javac_cmd,
                    cwd=source_path.parent,
                    timeout=timeout,
                    use_sudo=use_sudo,
                )
                javac_output = _combine_subprocess_output(javac_result.stdout, javac_result.stderr)
                if javac_result.returncode != 0:
                    return False, javac_output or "javac failed"

                if not any(classes_dir.rglob("*.class")):
                    return False, "javac completed without producing class files"

                jar_cmd = [archiver, "--create", "--file", str(target_path)]
                if main_class:
                    jar_cmd.extend(["--main-class", main_class])
                jar_cmd.extend(["-C", str(classes_dir), "."])
                jar_result = _run_compile_command(
                    jar_cmd,
                    cwd=source_path.parent,
                    timeout=timeout,
                    use_sudo=use_sudo,
                )
                jar_output = _combine_subprocess_output(jar_result.stdout, jar_result.stderr)
                combined_output = _combine_subprocess_output(javac_output, jar_output)
                if jar_result.returncode != 0:
                    return False, combined_output or "jar failed"
                return True, combined_output
            finally:
                if use_sudo:
                    _prepare_sudo_tempdir_for_cleanup(temp_dir)
                temp_dir_obj.cleanup()

        if language == "c":
            cmd = [compiler or "gcc"]
            if optimize:
                cmd.append("-O2")
            if warnings:
                cmd.extend(["-Wall", "-Wextra"])
            cmd.extend([str(source_path), "-o", str(target_path)])
        elif language == "cpp":
            cmd = [compiler or "g++"]
            if optimize:
                cmd.append("-O2")
            if warnings:
                cmd.extend(["-Wall", "-Wextra"])
            cmd.extend([str(source_path), "-o", str(target_path)])
        elif language == "go":
            go_input_paths = [
                str(p)
                for p in (compile_context.get("go_input_paths") or [str(source_path)])
                if str(p).strip()
            ]
            cmd = [compiler or "go", "build", "-o", str(target_path), *go_input_paths]
        elif language == "rust":
            cmd = [compiler or "rustc"]
            if optimize:
                cmd.append("-O")
            cmd.extend([str(source_path), "-o", str(target_path)])
        elif language == "csharp":
            cmd = [compiler or "csc"]
            if optimize:
                cmd.append("-optimize+")
            if warnings:
                cmd.append("-warn:4")
            cmd.extend([f"-out:{target_path}", str(source_path)])
        else:
            return False, "Unsupported language"

        result = _run_compile_command(
            cmd,
            cwd=source_path.parent,
            timeout=timeout,
            use_sudo=use_sudo,
        )
        output = _combine_subprocess_output(result.stdout, result.stderr)
        if result.returncode != 0:
            return False, output or "Compilation failed"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Compile operation timed out"
    except OSError as e:
        return False, str(e)


def compute_run_tooling_status(language: str) -> tuple[dict[str, object], int]:
    """Return run-tooling availability for a supported editor language."""
    normalized = (language or "").strip().lower()
    if not normalized:
        return {"error": "No language specified"}, 400

    # These are expected on Debian/Ubuntu base systems or are implicit in running the app.
    if normalized in {"python", "shell", "perl"}:
        return {"available": True}, 200

    if normalized == "javascript":
        if command_exists("node"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "JavaScript execution requires Node.js.",
            "install_command": "sudo apt update && sudo apt install nodejs",
        }, 200

    if normalized == "go":
        if command_exists("go"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "Go execution requires the Go toolchain.",
            "install_command": "sudo apt update && sudo apt install golang-go",
        }, 200

    if normalized == "c":
        if command_exists("gcc"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "C compilation requires gcc.",
            "install_command": "sudo apt update && sudo apt install gcc",
        }, 200

    if normalized == "cpp":
        if command_exists("g++"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "C++ compilation requires g++.",
            "install_command": "sudo apt update && sudo apt install g++",
        }, 200

    if normalized == "java":
        if command_exists("java"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "Java execution requires a JDK.",
            "install_command": "sudo apt update && sudo apt install default-jdk",
        }, 200

    if normalized == "ruby":
        if command_exists("ruby"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "Ruby execution requires ruby.",
            "install_command": "sudo apt update && sudo apt install ruby",
        }, 200

    if normalized == "rust":
        if command_exists("rustc"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "Rust execution requires rustc.",
            "install_command": "sudo apt update && sudo apt install rustc cargo",
        }, 200

    if normalized == "csharp":
        if has_dotnet_sdk():
            return {"available": True, "runner": "dotnet"}, 200

        csharp_compiler = find_first_command("csc", "mono-csc", "cli-csc")
        if csharp_compiler and command_exists("mono"):
            return {"available": True, "runner": "csc", "compiler": csharp_compiler}, 200

        if command_exists("mcs") and command_exists("mono"):
            return {"available": True, "runner": "mcs", "compiler": "mcs"}, 200

        return {
            "available": False,
            "error": "C# execution requires dotnet SDK or Mono.",
            "install_command": "sudo apt update && sudo apt install dotnet-sdk-8.0",
        }, 200

    if normalized == "brainfuck":
        if command_exists("bf"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "Brainfuck execution requires a `bf` interpreter in PATH.",
        }, 200

    if normalized == "magma":
        if command_exists("magma"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "Magma execution requires a `magma` interpreter in PATH.",
        }, 200

    return {"error": "Unsupported language"}, 400


def get_run_tooling_status(language: str) -> tuple[dict[str, object], int]:
    """
    Return run-tooling availability for a supported editor language.

    Successful detections are cached in-process until app restart. Missing-tool
    results are recomputed so newly installed runtimes become visible without a
    worker restart. With multiple workers, each worker maintains its own cache.
    """
    normalized = (language or "").strip().lower()
    if not normalized:
        return {"error": "No language specified"}, 400

    with RUN_TOOLING_CACHE_LOCK:
        cached = RUN_TOOLING_CACHE.get(normalized)
    if cached is not None:
        return dict(cached), 200

    status, http_status = compute_run_tooling_status(normalized)
    if http_status == 200 and status.get("available") is True:
        with RUN_TOOLING_CACHE_LOCK:
            RUN_TOOLING_CACHE[normalized] = dict(status)
    return status, http_status


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

                    is_link = entry.is_symlink()
                    items.append({
                        "name": entry.name,
                        "path": entry_path,
                        "is_dir": is_dir,
                        "is_symlink": is_link,
                        "symlink_target": str(os.readlink(entry)) if is_link else None,
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

        language = detect_language_for_path(path, content=content, is_binary=is_binary)

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

    @app.get("/api/run-tooling")
    def get_run_tooling():
        status, http_status = get_run_tooling_status(request.args.get("language", ""))
        return jsonify(status), http_status

    @app.get("/api/file/compile-info")
    def file_compile_info():
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

        compile_context, error, http_status = get_compile_context_for_path(path)
        if error:
            return jsonify({"error": error}), http_status

        tooling_status, http_status = get_compile_tooling_status(str(compile_context["language"]))
        payload = dict(compile_context)
        payload.update(tooling_status)
        return jsonify(payload), http_status

    @app.post("/api/file/compile")
    def compile_file_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        file_path = data.get("path", "")
        directory = str(data.get("directory", "")).strip()
        name = str(data.get("name", "")).strip()
        optimize = bool(data.get("optimize", False))
        warnings = bool(data.get("warnings", False))
        create_dirs = bool(data.get("create_dirs", False))
        overwrite = bool(data.get("overwrite", False))

        if not file_path:
            return jsonify({"error": "No path specified"}), 400
        if not name:
            return jsonify({"error": "No output name provided"}), 400
        if "\x00" in name:
            return jsonify({"error": "Invalid output name"}), 400
        if "/" in name or "\\" in name:
            return jsonify({"error": "Output name cannot include path separators"}), 400
        if name in {".", ".."}:
            return jsonify({"error": "Invalid output name"}), 400

        path, error = resolve_request_path(file_path, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "File not found"}), 404

        if not path.is_file():
            return jsonify({"error": "Not a file"}), 400

        compile_context, error, http_status = get_compile_context_for_path(path)
        if error:
            return jsonify({"error": error}), http_status

        language = str(compile_context["language"])
        tooling_status, http_status = get_compile_tooling_status(language)
        if http_status != 200:
            return jsonify(tooling_status), http_status
        if not tooling_status.get("available"):
            return jsonify(tooling_status), 400

        output_dir_raw = directory or str(path.parent)
        output_dir, error = resolve_request_path(output_dir_raw, "directory")
        if error:
            return jsonify({"error": error}), 400

        if not output_dir.exists():
            if create_dirs:
                ok, msg = ensure_directory(output_dir)
                if not ok:
                    return jsonify({"error": msg}), 500
            else:
                return jsonify({"error": "Destination directory does not exist"}), 400
        elif not output_dir.is_dir():
            return jsonify({"error": "Destination is not a directory"}), 400

        target = Path(os.path.abspath(output_dir / name))

        try:
            if target.resolve() == path.resolve():
                return jsonify({"error": "Output path cannot overwrite the source file"}), 400
        except OSError:
            return jsonify({"error": "Invalid output path"}), 400

        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                return jsonify({"error": "Output path points to an existing directory"}), 400
            if not overwrite:
                return jsonify({"error": "An output file with that name already exists"}), 409
            if _is_protected_path(target):
                return jsonify({"error": "Cannot overwrite a protected system path"}), 403

            ok, msg = delete_path(target)
            if not ok:
                return jsonify({"error": f"Failed to remove existing output: {msg}"}), 500

        success, compiler_output = compile_source_file(
            path,
            target,
            compile_context,
            tooling_status,
            optimize=optimize,
            warnings=warnings,
        )
        if not success:
            return jsonify({"error": compiler_output or "Compilation failed"}), 400

        if not target.exists():
            return jsonify({"error": "Compilation finished but no output file was created"}), 500

        if language in {"c", "cpp", "go", "rust"} and not os.access(target, os.X_OK):
            make_executable(target)

        return jsonify({
            "success": True,
            "message": build_compile_success_message(path, target, compile_context),
            "output_path": str(target),
            "compiler_output": compiler_output or None,
            "artifact_note": compile_context.get("artifact_note"),
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

    @app.post("/api/dir/create")
    def create_directory_route():
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
            return jsonify({"error": "No directory name provided"}), 400
        if "\x00" in name:
            return jsonify({"error": "Invalid name"}), 400
        if "/" in name or "\\" in name:
            return jsonify({"error": "Name cannot include path separators"}), 400
        if name in {".", ".."}:
            return jsonify({"error": "Invalid name"}), 400

        dir_path, error = resolve_request_path(directory, "directory")
        if error:
            return jsonify({"error": error}), 400
        if not dir_path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not dir_path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        target = (dir_path / name).resolve()
        if target.exists():
            return jsonify({"error": "A file or directory with that name already exists"}), 409

        success, message = create_dir(target)
        if not success:
            return jsonify({"error": message}), 500

        return jsonify({
            "success": True,
            "message": message,
            "path": str(target),
        })

    @app.post("/api/file/upload")
    def upload_files():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        directory = request.form.get("directory", "")
        if not directory:
            return jsonify({"error": "No directory specified"}), 400

        dir_path, error = resolve_request_path(directory, "directory")
        if error:
            return jsonify({"error": error}), 400
        if not dir_path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not dir_path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No files provided"}), 400

        saved = []
        skipped = []
        for f in files:
            if not f.filename:
                continue
            name = Path(f.filename).name
            if not name or name in {".", ".."} or "\x00" in name:
                continue

            target = (dir_path / name).resolve()
            if target.exists():
                skipped.append(name)
                continue
            content = f.read()

            success, message = write_file_bytes(target, content)
            if success:
                saved.append(name)
            else:
                return jsonify({"error": f"Failed to save {name}: {message}"}), 500

        msg = f"{len(saved)} file(s) uploaded"
        if skipped:
            msg += f", {len(skipped)} skipped (already exist): {', '.join(skipped)}"

        return jsonify({
            "success": True,
            "message": msg,
            "files": saved,
            "skipped": skipped,
        })

    @app.get("/api/file/download")
    def download_file():
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

        # Use read_file_bytes which has sudo fallback for unreadable files
        if os.access(path, os.R_OK):
            return send_file(path, as_attachment=True, download_name=path.name)

        success, data = read_file_bytes(path)
        if not success:
            return jsonify({"error": data}), 500

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return send_file(io.BytesIO(data), as_attachment=True, download_name=path.name, mimetype=mime)

    @app.post("/api/file/delete")
    def delete_file():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        file_path = data.get("path", "")
        if not file_path:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(file_path, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "File not found"}), 404

        if not path.is_file():
            return jsonify({"error": "Not a file"}), 400
        if _is_protected_path(path):
            return jsonify({"error": "Cannot delete files in a system directory"}), 403

        success, message = delete_path(path)
        if not success:
            return jsonify({"error": message}), 500

        return jsonify({"success": True, "message": message})

    # Directories where removing anything inside would break boot or basic functionality
    _PROTECTED_TREES = {
        "/boot",            # kernel, initramfs, grub
        "/dev", "/proc", "/sys", "/run",  # virtual/runtime filesystems
    }
    # Top-level system directories that should never be deleted
    _PROTECTED_TOPLEVEL = {
        "/bin", "/sbin", "/lib", "/lib64",
        "/usr", "/etc", "/var", "/boot",
        "/dev", "/proc", "/sys", "/run",
        "/home", "/root", "/srv", "/opt",
        "/mnt", "/media", "/snap",
    }
    # Specific critical directories (not their contents — just the dirs themselves)
    _PROTECTED_DIRS = {
        "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64",
        "/etc/systemd", "/etc/ssh",
    }
    # Specific critical files
    _PROTECTED_FILES = {
        "/etc/fstab", "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
        "/etc/sudoers", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf",
        "/etc/ssh/sshd_config",
    }

    def _is_protected_path(path: Path) -> bool:
        """Check if a path is critical for boot or basic system functionality."""
        resolved = str(Path(os.path.abspath(path)))
        # Block root
        if resolved == "/":
            return True
        # Block known system top-level directories
        if resolved in _PROTECTED_TOPLEVEL:
            return True
        # Block anything inside protected trees
        for tree in _PROTECTED_TREES:
            if resolved == tree or resolved.startswith(tree + "/"):
                return True
        # Block protected directories themselves
        if resolved in _PROTECTED_DIRS:
            return True
        # Block protected files
        if resolved in _PROTECTED_FILES:
            return True
        return False

    def _activation_result(message: str | None = None, *, error: bool = False) -> dict[str, str | bool | None]:
        return {
            "message": message,
            "error": error,
        }

    def _activate_service(dest_dir: str, filename: str, activate: bool, *, old_filename: str | None = None) -> dict[str, str | bool | None]:
        """Run activation commands for systemd or nginx after a copy/symlink/rename."""
        if not activate:
            return _activation_result()

        try:
            if dest_dir.rstrip("/") == "/etc/systemd/system":
                subprocess.run(
                    ["sudo", "-n", "systemctl", "daemon-reload"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                )
                # Enable new unit first, only then disable old
                result = subprocess.run(
                    ["sudo", "-n", "systemctl", "enable", "--now", filename],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                )
                if result.returncode != 0:
                    return _activation_result(
                        f"Service activation failed: {result.stderr.decode('utf-8', errors='replace')}",
                        error=True,
                    )
                if old_filename and old_filename != filename:
                    subprocess.run(
                        ["sudo", "-n", "systemctl", "disable", "--now", old_filename],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                    )
                return _activation_result(f"Service {filename} enabled and started.")

            if dest_dir.rstrip("/") == "/etc/nginx/sites-available":
                # Remove old dangling sites-enabled link before testing
                # (the file was already renamed, so the old link is dangling)
                if old_filename and old_filename != filename:
                    old_link = Path("/etc/nginx/sites-enabled") / old_filename
                    if old_link.is_symlink():
                        subprocess.run(
                            ["sudo", "-n", "rm", "-f", str(old_link)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                        )
                # Create new sites-enabled link
                enabled_link = Path("/etc/nginx/sites-enabled") / filename
                created_link = False
                if not enabled_link.exists():
                    subprocess.run(
                        ["sudo", "-n", "ln", "-sf", f"/etc/nginx/sites-available/{filename}", str(enabled_link)],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                    )
                    created_link = True
                test = subprocess.run(
                    ["sudo", "-n", "nginx", "-t"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                )
                if test.returncode != 0:
                    # Rollback: remove the new link
                    if created_link:
                        subprocess.run(
                            ["sudo", "-n", "rm", "-f", str(enabled_link)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                        )
                    return _activation_result(
                        f"nginx config test failed: {test.stderr.decode('utf-8', errors='replace')}",
                        error=True,
                    )
                reload = subprocess.run(
                    ["sudo", "-n", "systemctl", "reload", "nginx"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                )
                if reload.returncode == 0:
                    return _activation_result(f"Site {filename} enabled and nginx reloaded.")
                return _activation_result(
                    f"nginx reload failed: {reload.stderr.decode('utf-8', errors='replace')}",
                    error=True,
                )
        except subprocess.TimeoutExpired:
            return _activation_result("Activation timed out.", error=True)
        except Exception as e:
            return _activation_result(f"Activation error: {e}", error=True)

        return _activation_result()

    def _operation_success_payload(target: Path, activation: dict[str, str | bool | None]) -> dict[str, object]:
        return {
            "success": True,
            "path": str(target),
            "activate_message": activation["message"],
            "activate_error": activation["error"],
        }

    def _cleanup_temp_path(path: Path | str | None, *, recursive: bool = False) -> None:
        if not path:
            return

        temp_path = Path(path)

        try:
            if recursive:
                shutil.rmtree(temp_path)
            else:
                temp_path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            pass

        try:
            cleanup = subprocess.run(
                ["sudo", "-n", "rm", "-rf", str(temp_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
            if cleanup.returncode != 0:
                app.logger.warning(
                    "Failed to clean up temporary path %s: %s",
                    temp_path,
                    cleanup.stderr.decode("utf-8", errors="replace").strip(),
                )
        except Exception as e:
            app.logger.warning("Failed to clean up temporary path %s: %s", temp_path, e)

    @app.post("/api/file/copy")
    def copy_file_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        src_path_str = data.get("path", "")
        new_name = str(data.get("new_name", "")).strip()
        dest_dir_str = str(data.get("directory", "")).strip()

        if not src_path_str:
            return jsonify({"error": "No path specified"}), 400
        if not new_name:
            return jsonify({"error": "No new name provided"}), 400
        if "\x00" in new_name:
            return jsonify({"error": "Invalid name"}), 400
        if "/" in new_name or "\\" in new_name:
            return jsonify({"error": "Name cannot include path separators"}), 400
        if new_name in {".", ".."}:
            return jsonify({"error": "Invalid name"}), 400

        path, error = resolve_request_path(src_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        as_symlink = bool(data.get("symlink", False))
        create_dirs = bool(data.get("create_dirs", False))
        activate = bool(data.get("activate", False))
        overwrite = bool(data.get("overwrite", False))

        if not path.exists() and not path.is_symlink():
            return jsonify({"error": "File not found"}), 404
        if not path.is_file() and not path.is_symlink():
            return jsonify({"error": "Not a file"}), 400

        if dest_dir_str:
            dest_dir, error = resolve_request_path(dest_dir_str, "directory")
            if error:
                return jsonify({"error": error}), 400
            if not dest_dir.is_dir():
                if create_dirs:
                    ok, msg = ensure_directory(dest_dir)
                    if not ok:
                        return jsonify({"error": msg}), 500
                else:
                    return jsonify({"error": "Destination directory does not exist"}), 400
        else:
            dest_dir = path.parent

        target = Path(os.path.abspath(dest_dir / new_name))
        if target.resolve() == path.resolve():
            return jsonify({"error": "Source and destination are the same file"}), 400
        if target.exists() or target.is_symlink():
            if overwrite:
                if _is_protected_path(target):
                    return jsonify({"error": "Cannot overwrite a protected system path"}), 403
                if target.is_dir() and not target.is_symlink():
                    ok, msg = delete_directory(target)
                else:
                    ok, msg = delete_path(target)
                if not ok:
                    return jsonify({"error": f"Failed to remove existing target: {msg}"}), 500
            else:
                return jsonify({"error": "A file with that name already exists"}), 409

        if as_symlink:
            success, message = create_symlink(path, target)
        else:
            success, message = copy_file(path, target)
        if not success:
            return jsonify({"error": message}), 500

        if str(dest_dir).rstrip("/") == "/usr/local/bin":
            make_executable(target)

        # Activate for systemd / nginx
        activation = _activate_service(str(dest_dir), new_name, activate)

        return jsonify(_operation_success_payload(target, activation))

    @app.post("/api/dir/copy")
    def copy_directory_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        src_path_str = data.get("path", "")
        new_name = str(data.get("new_name", "")).strip()
        dest_dir_str = str(data.get("directory", "")).strip()

        if not src_path_str:
            return jsonify({"error": "No path specified"}), 400
        if not new_name:
            return jsonify({"error": "No new name provided"}), 400
        if "\x00" in new_name:
            return jsonify({"error": "Invalid name"}), 400
        if "/" in new_name or "\\" in new_name:
            return jsonify({"error": "Name cannot include path separators"}), 400
        if new_name in {".", ".."}:
            return jsonify({"error": "Invalid name"}), 400

        path, error = resolve_request_path(src_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        as_symlink = bool(data.get("symlink", False))
        create_dirs = bool(data.get("create_dirs", False))
        activate = bool(data.get("activate", False))
        overwrite = bool(data.get("overwrite", False))

        if not path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        if dest_dir_str:
            dest_dir, error = resolve_request_path(dest_dir_str, "directory")
            if error:
                return jsonify({"error": error}), 400
            if not dest_dir.is_dir():
                if create_dirs:
                    ok, msg = ensure_directory(dest_dir)
                    if not ok:
                        return jsonify({"error": msg}), 500
                else:
                    return jsonify({"error": "Destination directory does not exist"}), 400
        else:
            dest_dir = path.parent

        target = Path(os.path.abspath(dest_dir / new_name))
        resolved_path = path.resolve()
        if target.resolve() == resolved_path:
            return jsonify({"error": "Source and destination are the same directory"}), 400
        if not path.is_symlink() and str(target.resolve()).startswith(str(resolved_path) + "/"):
            return jsonify({"error": "Cannot copy a directory into itself"}), 400
        if target.exists() or target.is_symlink():
            if overwrite:
                if _is_protected_path(target):
                    return jsonify({"error": "Cannot overwrite a protected system path"}), 403
                if target.is_dir() and not target.is_symlink():
                    ok, msg = delete_directory(target)
                else:
                    ok, msg = delete_path(target)
                if not ok:
                    return jsonify({"error": f"Failed to remove existing target: {msg}"}), 500
            else:
                return jsonify({"error": "A file or directory with that name already exists"}), 409

        if as_symlink:
            success, message = create_symlink(path, target)
        else:
            success, message = copy_directory(path, target)
        if not success:
            return jsonify({"error": message}), 500

        # Activate for systemd / nginx
        activation = _activate_service(str(dest_dir), new_name, activate)

        return jsonify(_operation_success_payload(target, activation))

    @app.post("/api/rename")
    def rename_item():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        old_path_str = data.get("path", "")
        new_name = str(data.get("new_name", "")).strip()
        dest_dir_str = str(data.get("directory", "")).strip()
        create_dirs = bool(data.get("create_dirs", False))
        overwrite = bool(data.get("overwrite", False))
        activate = bool(data.get("activate", False))

        if not old_path_str:
            return jsonify({"error": "No path specified"}), 400
        if not new_name:
            return jsonify({"error": "No new name provided"}), 400
        if "\x00" in new_name:
            return jsonify({"error": "Invalid name"}), 400
        if "/" in new_name or "\\" in new_name:
            return jsonify({"error": "Name cannot include path separators"}), 400
        if new_name in {".", ".."}:
            return jsonify({"error": "Invalid name"}), 400

        path, error = resolve_request_path(old_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "Path not found"}), 404

        if dest_dir_str:
            dest_dir, error = resolve_request_path(dest_dir_str, "directory")
            if error:
                return jsonify({"error": error}), 400
            if not dest_dir.is_dir():
                if create_dirs:
                    ok, msg = ensure_directory(dest_dir)
                    if not ok:
                        return jsonify({"error": msg}), 500
                else:
                    return jsonify({"error": "Destination directory does not exist"}), 400
        else:
            dest_dir = path.parent

        target = Path(os.path.abspath(dest_dir / new_name))
        resolved_path = path.resolve()
        if target.resolve() == resolved_path:
            return jsonify({"error": "Source and destination are the same path"}), 400
        if path.is_dir() and not path.is_symlink() and str(target.resolve()).startswith(str(resolved_path) + "/"):
            return jsonify({"error": "Cannot move a directory into itself"}), 400
        if target.exists() or target.is_symlink():
            if overwrite:
                if _is_protected_path(target):
                    return jsonify({"error": "Cannot overwrite a protected system path"}), 403
                if target.is_dir() and not target.is_symlink():
                    ok, msg = delete_directory(target)
                else:
                    ok, msg = delete_path(target)
                if not ok:
                    return jsonify({"error": f"Failed to remove existing target: {msg}"}), 500
            else:
                return jsonify({"error": "A file or directory with that name already exists"}), 409

        success, message = rename_path(path, target)
        if not success:
            return jsonify({"error": message}), 500

        if str(dest_dir).rstrip("/") == "/usr/local/bin":
            make_executable(target)

        # Activate for systemd / nginx
        activation = _activate_service(str(dest_dir), new_name, activate, old_filename=path.name)

        return jsonify(_operation_success_payload(target, activation))

    @app.get("/api/dir/preview")
    def preview_directory():
        dir_path_str = request.args.get("path", "")
        if not dir_path_str:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(dir_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        files = []
        dirs = []
        try:
            for entry in sorted(path.rglob("*")):
                rel = str(entry.relative_to(path))
                if entry.is_dir():
                    dirs.append(rel + "/")
                else:
                    files.append(rel)
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403

        return jsonify({
            "path": str(path),
            "files": files,
            "dirs": dirs,
            "total_files": len(files),
            "total_dirs": len(dirs),
        })

    @app.post("/api/dir/delete")
    def delete_directory_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        dir_path_str = data.get("path", "")
        if not dir_path_str:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(dir_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not path.is_dir():
            return jsonify({"error": "Not a directory"}), 400
        if _is_protected_path(path):
            return jsonify({"error": "Cannot delete a system directory"}), 403

        success, message = delete_directory(path)
        if not success:
            return jsonify({"error": message}), 500

        return jsonify({"success": True, "message": message})

    @app.get("/api/dir/download")
    def download_directory_zip():
        dir_path_str = request.args.get("path", "")
        if not dir_path_str:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(dir_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not path.exists():
            return jsonify({"error": "Directory not found"}), 404
        if not path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        success, result = zip_directory(path)
        if not success:
            return jsonify({"error": result}), 500

        response = send_file(
            result,
            as_attachment=True,
            download_name=f"{path.name}.zip",
            mimetype="application/zip",
        )

        @response.call_on_close
        def _cleanup():
            _cleanup_temp_path(result)

        return response

    @app.get("/api/batch/download")
    def batch_download():
        raw_paths = request.args.getlist("path")
        if not raw_paths:
            return jsonify({"error": "No paths specified"}), 400

        paths = []
        for raw in raw_paths:
            p, err = resolve_request_path(raw)
            if err:
                return jsonify({"error": err}), 400
            if not p.exists():
                return jsonify({"error": f"Not found: {p.name}"}), 404
            paths.append(p)

        def _allocate_archive_name(source: Path, seen_names: dict[str, int]) -> str:
            base = source.name or "item"
            if base not in seen_names:
                seen_names[base] = 1
                return base

            seen_names[base] += 1
            n = seen_names[base]
            return f"{source.stem} ({n}){source.suffix}" if not source.is_dir() else f"{base} ({n})"

        staging_root = None
        zip_path = None
        try:
            staging_root = Path(tempfile.mkdtemp(prefix="diff-editor-batch-"))
            seen_names: dict[str, int] = {}

            for p in paths:
                target = staging_root / _allocate_archive_name(p, seen_names)
                if p.is_dir():
                    success, message = copy_directory(p, target)
                else:
                    success, message = copy_file(p, target)

                if not success:
                    return jsonify({"error": f"Failed to add {p.name} to archive: {message}"}), 500

            success, result = zip_directory(staging_root)
            if not success:
                return jsonify({"error": result}), 500
            zip_path = result
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        finally:
            _cleanup_temp_path(staging_root, recursive=True)

        response = send_file(
            zip_path,
            as_attachment=True,
            download_name="selection.zip",
            mimetype="application/zip",
        )

        @response.call_on_close
        def _cleanup():
            _cleanup_temp_path(zip_path)

        return response

    @app.get("/api/file/info")
    def file_info():
        raw_path = request.args.get("path", "")
        path, error = resolve_request_path(raw_path)
        if error:
            return jsonify({"error": error}), 400
        if not path.exists() and not path.is_symlink():
            return jsonify({"error": "Not found"}), 404

        success, info = stat_path(path)
        if not success:
            return jsonify({"error": info}), 500

        result = {"name": path.name, "path": str(path), **info, "mime_type": None}
        if not info["is_dir"]:
            result["mime_type"] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        return jsonify(result)

    @app.get("/api/file/info/extended")
    def file_info_extended():
        raw_path = request.args.get("path", "")
        path, error = resolve_request_path(raw_path)
        if error:
            return jsonify({"error": error}), 400
        if not path.exists() and not path.is_symlink():
            return jsonify({"error": "Not found"}), 404

        result: dict = {
            "is_binary": None,
            "line_count": None,
            "image_info": None,
            "pdf_pages": None,
            "pdf_info": None,
            "media_info": None,
            "video_info": None,
            "size_recursive": None,
            "size_recursive_human": None,
            "file_count": None,
            "dir_count": None,
        }

        if path.is_dir():
            success, info = get_directory_info(path)
            if success:
                result.update(info)
            else:
                result["error"] = info
        else:
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

            # Binary detection from file head only (not full read)
            success, head = read_file_head(path)
            if success and isinstance(head, bytes):
                result["is_binary"] = is_likely_binary(head)

            # Line count via streaming chunks (not full read)
            if result["is_binary"] is False:
                success, n = count_lines(path)
                if success:
                    result["line_count"] = n

            # Image info (Pillow reads headers lazily from path)
            if mime_type.startswith("image/"):
                try:
                    from PIL import Image
                    if os.access(path, os.R_OK):
                        with Image.open(path) as img:
                            result["image_info"] = build_image_info(img)
                    else:
                        ok, data = read_file_bytes(path)
                        if ok:
                            with Image.open(io.BytesIO(data)) as img:
                                result["image_info"] = build_image_info(img)
                except Exception:
                    pass

            # PDF metadata (pypdf reads from path)
            elif mime_type == "application/pdf":
                try:
                    import pypdf
                    if os.access(path, os.R_OK):
                        with open(path, "rb") as f:
                            reader = pypdf.PdfReader(f)
                            result["pdf_info"] = build_pdf_info(
                                reader,
                                head if isinstance(head, bytes) else None,
                            )
                            if not reader.is_encrypted:
                                result["pdf_pages"] = len(reader.pages)
                    else:
                        ok, data = read_file_bytes(path)
                        if ok:
                            reader = pypdf.PdfReader(io.BytesIO(data))
                            result["pdf_info"] = build_pdf_info(
                                reader,
                                head if isinstance(head, bytes) else data[:32],
                            )
                            if not reader.is_encrypted:
                                result["pdf_pages"] = len(reader.pages)
                except Exception:
                    pass

            # Video/audio metadata via ffprobe
            if mime_type.startswith("video/") or mime_type.startswith("audio/"):
                try:
                    proc = subprocess.run(
                        ["ffprobe", "-v", "quiet", "-print_format", "json",
                         "-show_streams", "-show_format", str(path)],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                    )
                    if proc.returncode == 0:
                        ff = json.loads(proc.stdout)
                        media_info = build_media_info(ff)
                        result["media_info"] = media_info
                        result["video_info"] = media_info
                except Exception:
                    pass

        return jsonify(result)

    @app.get("/api/file/zip-info")
    def zip_info():
        zip_path_str = request.args.get("path", "")
        if not zip_path_str:
            return jsonify({"error": "No path specified"}), 400

        zip_path, error = resolve_request_path(zip_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not zip_path.exists():
            return jsonify({"error": "File not found"}), 404

        import zipfile as zf
        try:
            size = zip_path.stat().st_size
            with zf.ZipFile(zip_path, "r") as z:
                file_count = sum(1 for i in z.infolist() if not i.is_dir())
            return jsonify({"size": size, "file_count": file_count})
        except (OSError, zf.BadZipFile):
            return jsonify({"error": "Cannot read zip file"}), 400

    @app.post("/api/file/extract")
    def extract_zip():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        zip_path_str = data.get("path", "")
        mode = data.get("mode", "directory")  # "directory" or "here"

        if not zip_path_str:
            return jsonify({"error": "No path specified"}), 400

        zip_path, error = resolve_request_path(zip_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        if not zip_path.exists():
            return jsonify({"error": "File not found"}), 404

        import zipfile as zf
        import shutil as _shutil

        def _dedupe(p: Path) -> Path:
            """Find a unique name by appending (2), (3), etc."""
            if not p.exists() and not p.is_symlink():
                return p
            stem = p.stem
            suffix = p.suffix
            parent = p.parent
            n = 2
            while True:
                candidate = parent / f"{stem} ({n}){suffix}"
                if not candidate.exists() and not candidate.is_symlink():
                    return candidate
                n += 1

        if mode == "directory":
            dest = _dedupe(zip_path.parent / zip_path.stem)
        else:
            dest = zip_path.parent

        try:
            with zf.ZipFile(zip_path, "r") as z:
                dest_resolved = str(dest.resolve()).rstrip("/") + "/"

                if mode == "directory":
                    # dest is fresh — no symlinks to worry about
                    for name in z.namelist():
                        resolved = str((dest / name).resolve())
                        if resolved != dest_resolved.rstrip("/") and not resolved.startswith(dest_resolved):
                            return jsonify({"error": f"Zip contains unsafe path: {name}"}), 400
                    dest.mkdir()
                    z.extractall(dest)
                else:
                    # Build rename map for conflicting top-level entries
                    rename_map = {}  # original top-level name -> deduped name
                    for info in z.infolist():
                        parts = info.filename.rstrip("/").split("/")
                        top = parts[0]
                        if top not in rename_map:
                            deduped = _dedupe(dest / top)
                            rename_map[top] = deduped.name

                    # Security check against deduped paths (avoids symlink confusion)
                    for name in z.namelist():
                        parts = name.rstrip("/").split("/")
                        parts[0] = rename_map.get(parts[0], parts[0])
                        mapped = "/".join(parts)
                        resolved = str(Path(os.path.abspath(dest / mapped)))
                        if resolved != dest_resolved.rstrip("/") and not resolved.startswith(dest_resolved):
                            return jsonify({"error": f"Zip contains unsafe path: {name}"}), 400

                    # Extract with mapped paths
                    for info in z.infolist():
                        parts = info.filename.rstrip("/").split("/")
                        parts[0] = rename_map.get(parts[0], parts[0])
                        target = dest / "/".join(parts)

                        if info.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with z.open(info) as src, open(target, "wb") as dst:
                                _shutil.copyfileobj(src, dst)

            count = sum(1 for i in zf.ZipFile(zip_path, "r").infolist() if not i.is_dir())
            return jsonify({"success": True, "count": count, "dest": str(dest)})
        except PermissionError:
            pass
        except (OSError, zf.BadZipFile) as e:
            return jsonify({"error": str(e)}), 400

        # Fall back to sudo unzip (no dedup — best effort)
        try:
            if mode == "directory":
                subprocess.run(
                    ["sudo", "-n", "mkdir", "-p", str(dest)],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                )
            result = subprocess.run(
                ["sudo", "-n", "unzip", "-n", str(zip_path), "-d", str(dest)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
            )
            if result.returncode in (0, 1):
                warning = None
                if result.returncode == 1:
                    # Parse skipped files from unzip output
                    stdout = result.stdout.decode("utf-8", errors="replace")
                    skipped = [line.split("exists")[0].strip().split()[-1]
                               for line in stdout.splitlines() if "already exists" in line.lower()]
                    if skipped:
                        warning = f"{len(skipped)} file(s) skipped (already exist)"
                return jsonify({"success": True, "dest": str(dest), "warning": warning})
            error_msg = result.stderr.decode("utf-8", errors="replace")
            return jsonify({"error": f"unzip failed: {error_msg}"}), 500
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Extract operation timed out"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

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

        # Validate reasoning effort (default to medium for balanced speed/quality)
        VALID_EFFORTS = ("low", "medium", "high", "xhigh")
        reasoning_effort = str(data.get("reasoning_effort", "medium")).strip().lower()
        if reasoning_effort not in VALID_EFFORTS:
            reasoning_effort = "medium"

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
                        model="gpt-5.3-codex",
                        reasoning={"effort": reasoning_effort},
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
        cmd = [
            "codex", "exec", "review",
            "-m", "gpt-5.4",
            "-c", f'model_reasoning_effort="{reasoning_effort}"',
            # "--enable", "fast_mode",
            "--json",
        ]

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
