"""
Diff Editor - A web-based side-by-side file diff and editing tool.
"""

import ast
import difflib
import fcntl
import hashlib
import html
import ipaddress
import io
import json
import logging
import logging.handlers
import math
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from dotenv import dotenv_values, load_dotenv
from flask import (
    Flask,
    Response,
    get_flashed_messages,
    jsonify,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, Undefined, select_autoescape
from jinja2.utils import htmlsafe_json_dumps
from werkzeug.middleware.proxy_fix import ProxyFix
from openai import OpenAI

load_dotenv()

# Set up access logging (rotation handled externally by rotatelog)
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

access_logger = logging.getLogger("diff_editor.access")
access_logger.setLevel(logging.INFO)
access_logger.propagate = False

_access_handler = logging.handlers.WatchedFileHandler(LOG_DIR / "access.log")
_access_handler.setFormatter(logging.Formatter(
    '%(message)s'  # We'll format the message ourselves for gunicorn-style output
))
access_logger.addHandler(_access_handler)

from utils.file_ops import (
    RECYCLE_BIN,
    RECYCLE_META_DIR,
    bytes_to_hex_view,
    cleanup_temp_path,
    copy_directory,
    copy_file,
    create_dir,
    create_symlink,
    delete_directory,
    delete_path,
    detect_image_mime,
    detect_language_for_path,
    ensure_directory,
    extract_zip_archive,
    get_extended_file_info,
    get_recycle_metadata,
    get_zip_info,
    is_in_recycle_bin,
    is_likely_binary,
    is_recycle_bin_root,
    is_writable_by_user,
    list_directory_entries,
    check_path_is_file,
    check_path_is_directory,
    check_path_exists,
    get_file_size,
    make_executable,
    parse_hex_view,
    permanently_delete_path,
    read_file_bytes,
    read_file_head,
    rename_path,
    resolve_request_path,
    stat_path,
    write_file,
    write_file_bytes,
    zip_directory,
    zip_paths,
)
from utils.compile_ops import (
    build_compile_success_message,
    command_exists,
    compile_source_file,
    find_first_command,
    get_compile_context_for_path,
    get_compile_tooling_status,
    has_dotnet_sdk,
)
from utils.git_ops import find_git_root, get_head_content_bytes, is_tracked_by_git, get_directory_git_status, get_tracked_files


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

RUN_TOOLING_CACHE: dict[str, dict[str, object]] = {}
RUN_TOOLING_CACHE_LOCK = threading.Lock()


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

    if normalized == "pfd":
        if command_exists("pfdsim"):
            return {"available": True}, 200
        return {
            "available": False,
            "error": "PFD execution requires `pfdsim` in PATH.",
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


class PreviewConfig(dict):
    """Dict that also supports Flask-style config attribute access in previews."""

    def __getattr__(self, key: str):
        if key in self:
            return self[key]
        return PreviewUndefined(name=f"config.{key}")


class PreviewUndefined(ChainableUndefined):
    """Undefined value that renders as empty data for best-effort previews."""

    def __call__(self, *args, **kwargs):
        return args[1] if len(args) > 1 else ""

    def __getitem__(self, key):
        return self

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __str__(self):
        return ""

    def __html__(self):
        return ""

    def __int__(self):
        return 0

    def __float__(self):
        return 0.0

    def __eq__(self, other):
        return other in (None, "", False)

    def get(self, key=None, default=""):
        return default or ""

    def keys(self):
        return []

    def values(self):
        return []

    def items(self):
        return []


def _preview_json_value(value):
    if isinstance(value, Undefined):
        return None
    if isinstance(value, dict):
        return {str(_preview_json_value(key)): _preview_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_preview_json_value(item) for item in value]
    return value


def _preview_tojson(value, indent=None):
    return htmlsafe_json_dumps(_preview_json_value(value), dumps=json.dumps, indent=indent)


def _safe_markdown_attr(value: str) -> str:
    return html.escape(html.unescape(value).strip(), quote=True)


def _safe_markdown_url(value: str) -> str:
    normalized = html.unescape(value).strip()
    parsed = urlsplit(normalized)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https", "mailto", "tel"}:
        return "#"
    return html.escape(normalized, quote=True)


def _render_markdown_inline(text: str) -> str:
    placeholders: list[str] = []

    def stash(value: str) -> str:
        placeholders.append(value)
        return f"\x00{len(placeholders) - 1}\x00"

    def render_image(match: re.Match) -> str:
        title = match.group(3)
        title_attr = f' title="{_safe_markdown_attr(title)}"' if title else ""
        return stash(
            f'<img src="{_safe_markdown_url(match.group(2))}" alt="{_safe_markdown_attr(match.group(1))}"'
            f"{title_attr}>"
        )

    def render_link(match: re.Match) -> str:
        title = match.group(3)
        title_attr = f' title="{_safe_markdown_attr(title)}"' if title else ""
        return stash(f'<a href="{_safe_markdown_url(match.group(2))}"{title_attr}>{match.group(1)}</a>')

    escaped = html.escape(text, quote=True)
    escaped = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)",
        render_image,
        escaped,
    )
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;([^&]*)&quot;)?\)",
        render_link,
        escaped,
    )
    escaped = re.sub(r"`([^`]+)`", lambda match: stash(f"<code>{match.group(1)}</code>"), escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<![\w*])\*([^*]+)\*(?![\w*])", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<![\w_])_([^_]+)_(?![\w_])", r"<em>\1</em>", escaped)

    for index, value in enumerate(placeholders):
        escaped = escaped.replace(f"\x00{index}\x00", value)
    return escaped


def _wrap_markdown_preview(body_html: str, title: str) -> str:
    title_html = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_html}</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; background: #f8fafc; color: #1f2937; font: 16px/1.65 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 2rem clamp(1rem, 4vw, 3rem) 4rem; background: #fff; min-height: 100vh; box-shadow: 0 0 0 1px rgba(148, 163, 184, 0.25); }}
    h1, h2, h3, h4, h5, h6 {{ line-height: 1.2; margin: 1.7em 0 0.55em; color: #111827; }}
    h1 {{ margin-top: 0; font-size: 2rem; border-bottom: 1px solid #e5e7eb; padding-bottom: 0.35rem; }}
    a {{ color: #0f766e; }}
    img {{ max-width: 100%; height: auto; }}
    pre {{ overflow: auto; padding: 1rem; border-radius: 0.5rem; background: #111827; color: #f9fafb; }}
    code {{ border-radius: 0.25rem; background: #eef2f7; padding: 0.1rem 0.25rem; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; }}
    pre code {{ background: transparent; padding: 0; }}
    blockquote {{ margin: 1rem 0; padding-left: 1rem; border-left: 4px solid #cbd5e1; color: #475569; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d1d5db; padding: 0.4rem 0.6rem; }}
    th.align-left, td.align-left {{ text-align: left; }}
    th.align-center, td.align-center {{ text-align: center; }}
    th.align-right, td.align-right {{ text-align: right; }}
  </style>
</head>
<body>
  <main>
{body_html}
  </main>
</body>
</html>"""


def _split_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None

    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in stripped:
        if escaped:
            if char == "|":
                current.append("|")
            else:
                current.extend(("\\", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current.clear()
        else:
            current.append(char)

    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if len(cells) < 2:
        return None
    return cells


def _parse_markdown_table_separator(line: str) -> list[str] | None:
    cells = _split_markdown_table_row(line)
    if cells is None:
        return None

    alignments: list[str] = []
    for cell in cells:
        marker = re.sub(r"\s+", "", cell)
        if not re.fullmatch(r":?-{3,}:?", marker):
            return None
        if marker.startswith(":") and marker.endswith(":"):
            alignments.append("center")
        elif marker.endswith(":"):
            alignments.append("right")
        elif marker.startswith(":"):
            alignments.append("left")
        else:
            alignments.append("")
    return alignments


def _normalize_markdown_table_cells(cells: list[str], width: int) -> list[str]:
    if len(cells) >= width:
        return cells[:width]
    return cells + [""] * (width - len(cells))


def _render_markdown_table(header: list[str], alignments: list[str], rows: list[list[str]]) -> str:
    def cell_class(index: int) -> str:
        alignment = alignments[index]
        return f' class="align-{alignment}"' if alignment else ""

    normalized_header = _normalize_markdown_table_cells(header, len(alignments))
    rendered_header = "".join(
        f"<th{cell_class(index)}>{_render_markdown_inline(cell)}</th>"
        for index, cell in enumerate(normalized_header)
    )
    rendered_rows = []
    for row in rows:
        normalized_row = _normalize_markdown_table_cells(row, len(alignments))
        rendered_cells = "".join(
            f"<td{cell_class(index)}>{_render_markdown_inline(cell)}</td>"
            for index, cell in enumerate(normalized_row)
        )
        rendered_rows.append(f"<tr>{rendered_cells}</tr>")

    body_html = "\n<tbody>\n" + "\n".join(rendered_rows) + "\n</tbody>" if rendered_rows else ""
    return f"<table>\n<thead><tr>{rendered_header}</tr></thead>{body_html}\n</table>"


def _render_markdown_document(markdown_text: str, title: str) -> str:
    # Keep previews deterministic and raw HTML inert regardless of optional packages.
    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rendered: list[str] = []
    paragraph: list[str] = []
    list_type: str | None = None
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            rendered.append(f"<p>{_render_markdown_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            rendered.append(f"</{list_type}>")
            list_type = None

    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip()
        fence_match = re.match(r"^```", line)
        if fence_match:
            if in_code:
                rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines.clear()
                in_code = False
            else:
                flush_paragraph()
                close_list()
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(raw_line)
            index += 1
            continue

        if not line.strip():
            flush_paragraph()
            close_list()
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            rendered.append(f"<h{level}>{_render_markdown_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if re.match(r"^[-*_]\s*[-*_]\s*[-*_][-*_\s]*$", line):
            flush_paragraph()
            close_list()
            rendered.append("<hr>")
            index += 1
            continue

        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            flush_paragraph()
            close_list()
            rendered.append(f"<blockquote>{_render_markdown_inline(quote.group(1))}</blockquote>")
            index += 1
            continue

        table_header = _split_markdown_table_row(line)
        if table_header is not None and index + 1 < len(lines):
            table_alignments = _parse_markdown_table_separator(lines[index + 1].rstrip())
            if table_alignments is not None and len(table_alignments) == len(table_header):
                flush_paragraph()
                close_list()
                table_rows: list[list[str]] = []
                index += 2
                while index < len(lines):
                    row_line = lines[index].rstrip()
                    if not row_line.strip():
                        break
                    row_cells = _split_markdown_table_row(row_line)
                    if row_cells is None:
                        break
                    table_rows.append(row_cells)
                    index += 1
                rendered.append(_render_markdown_table(table_header, table_alignments, table_rows))
                continue

        unordered = re.match(r"^\s*[-*+]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if unordered or ordered:
            flush_paragraph()
            next_list_type = "ul" if unordered else "ol"
            if list_type != next_list_type:
                close_list()
                rendered.append(f"<{next_list_type}>")
                list_type = next_list_type
            rendered.append(f"<li>{_render_markdown_inline((unordered or ordered).group(1))}</li>")
            index += 1
            continue

        close_list()
        paragraph.append(line.strip())
        index += 1

    if in_code:
        rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    flush_paragraph()
    close_list()

    body_html = "\n".join(rendered)
    return _wrap_markdown_preview(body_html, title)


def _eval_preview_config_expr(node: ast.AST, names: dict[str, object], env_values: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id, PreviewUndefined(name=node.id))
    if isinstance(node, ast.List):
        return [_eval_preview_config_expr(item, names, env_values) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_preview_config_expr(item, names, env_values) for item in node.elts)
    if isinstance(node, ast.Set):
        return {_eval_preview_config_expr(item, names, env_values) for item in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _eval_preview_config_expr(key, names, env_values): _eval_preview_config_expr(value, names, env_values)
            for key, value in zip(node.keys, node.values)
            if key is not None
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _eval_preview_config_expr(node.operand, names, env_values)
        return -value
    if isinstance(node, ast.BinOp):
        left = _eval_preview_config_expr(node.left, names, env_values)
        right = _eval_preview_config_expr(node.right, names, env_values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Div):
            return left / right
    if isinstance(node, ast.Call):
        func = node.func
        args = [_eval_preview_config_expr(arg, names, env_values) for arg in node.args]
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = func.value.id
            if owner == "os" and func.attr == "getenv":
                key = str(args[0]) if args else ""
                default = args[1] if len(args) > 1 else None
                return os.getenv(key, env_values.get(key, default))
            owner_value = names.get(owner)
            if func.attr == "keys" and hasattr(owner_value, "keys"):
                return owner_value.keys()
            if func.attr == "values" and hasattr(owner_value, "values"):
                return owner_value.values()
            if func.attr == "items" and hasattr(owner_value, "items"):
                return owner_value.items()
        if isinstance(func, ast.Name):
            if func.id == "int":
                return int(args[0])
            if func.id == "str":
                return str(args[0])
            if func.id == "float":
                return float(args[0])
            if func.id == "bool":
                return bool(args[0])
            if func.id == "list":
                return list(args[0])
            if func.id == "set":
                return set(args[0])
            if func.id == "tuple":
                return tuple(args[0])
            if func.id == "dict":
                return dict(args[0]) if args else {}
    raise ValueError(f"Unsupported config expression: {ast.dump(node, include_attributes=False)}")


def _assign_preview_name(target: ast.AST, value: object, names: dict[str, object]) -> None:
    if isinstance(target, ast.Name):
        names[target.id] = value


def _load_project_preview_config(project_root: Path, base_config: dict[str, object]) -> PreviewConfig:
    preview_config = PreviewConfig(base_config)
    app_file = project_root / "app.py"
    if not app_file.exists():
        return preview_config

    try:
        source = app_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return preview_config

    env_values = dict(dotenv_values(project_root / ".env"))
    names: dict[str, object] = {}

    for stmt in tree.body:
        try:
            if isinstance(stmt, ast.Assign):
                value = _eval_preview_config_expr(stmt.value, names, env_values)
                for target in stmt.targets:
                    _assign_preview_name(target, value, names)
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                value = _eval_preview_config_expr(stmt.value, names, env_values)
                _assign_preview_name(stmt.target, value, names)
        except Exception:
            continue

    for func in [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for stmt in ast.walk(func):
            try:
                if (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute)
                    and stmt.value.func.attr == "update"
                    and isinstance(stmt.value.func.value, ast.Attribute)
                    and stmt.value.func.value.attr == "config"
                ):
                    for keyword in stmt.value.keywords:
                        if keyword.arg:
                            preview_config[keyword.arg] = _eval_preview_config_expr(keyword.value, names, env_values)
                elif (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Subscript)
                    and isinstance(stmt.targets[0].value, ast.Attribute)
                    and stmt.targets[0].value.attr == "config"
                ):
                    key = _eval_preview_config_expr(stmt.targets[0].slice, names, env_values)
                    preview_config[str(key)] = _eval_preview_config_expr(stmt.value, names, env_values)
            except Exception:
                continue

    return preview_config


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
    MAX_PDF_FILE_SIZE = int(os.environ.get("MAX_PDF_FILE_SIZE", 250 * 1024 * 1024))
    AI_REVIEW_COOLDOWN_SECONDS = max(0.0, float(os.environ.get("AI_REVIEW_COOLDOWN_SECONDS", "10")))
    AI_REVIEW_COOLDOWN_FILE = Path(
        os.environ.get("AI_REVIEW_COOLDOWN_FILE", "/tmp/diff-editor-ai-review-cooldown.txt")
    )
    AI_REVIEW_PROVIDER = os.environ.get("AI_REVIEW_PROVIDER", "codex_cli").strip().lower()
    AI_REVIEW_MODEL = os.environ.get("AI_REVIEW_MODEL", "gpt-5.3-codex").strip()
    AI_REVIEW_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    AI_REVIEW_REASONING = os.environ.get("AI_REVIEW_REASONING", "true").strip().lower() not in ("0", "false", "no", "off")
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
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com 'unsafe-inline'; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; "
            "worker-src 'self' blob: https://cdnjs.cloudflare.com; "
            "frame-ancestors 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return render_template("index.html", default_root=DEFAULT_ROOT, error=f"{error}: {file_path}"), status

        return render_template("diff.html", file_path=str(path))

    @app.get("/api/browse")
    def browse_directory():
        path_str = request.args.get("path", DEFAULT_ROOT)
        show_hidden = request.args.get("hidden", "false").lower() == "true"

        path, error = resolve_request_path(path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        ok, error, status = check_path_is_directory(path)
        if not ok:
            return jsonify({"error": error}), status

        # Get git status and tracked files for the directory
        git_status = get_directory_git_status(path)
        tracked_files = get_tracked_files(path)

        current_is_recycle_root = is_recycle_bin_root(path)
        current_in_recycle_bin = is_in_recycle_bin(path)

        items = []
        try:
            raw_entries = list_directory_entries(path)
            for info in sorted(raw_entries, key=lambda x: (not x.get("is_dir"), x["name"].lower())):
                if not show_hidden and info["name"].startswith("."):
                    continue

                if info.get("error"):
                    items.append({
                        "name": info["name"],
                        "path": info["path"],
                        "is_dir": False,
                        "is_git": False,
                        "git_status": None,
                        "writable": False,
                        "error": "Permission denied",
                    })
                    continue

                entry = Path(info["path"])
                try:
                    is_dir = info["is_dir"]
                    entry_path = info["path"]
                    file_git_status = git_status.get(entry_path) if not is_dir else None
                    # Only mark as git-tracked if actually in git's index (not just in a repo folder)
                    is_tracked = entry_path in tracked_files if not is_dir else False
                    recycle_meta = get_recycle_metadata(entry)
                    original_path = recycle_meta.get("original_path") if isinstance(recycle_meta, dict) else None

                    is_link = info["is_symlink"]
                    items.append({
                        "name": info["name"],
                        "path": entry_path,
                        "is_dir": is_dir,
                        "is_symlink": is_link,
                        "symlink_target": info.get("symlink_target"),
                        "is_git": is_tracked,
                        "git_status": file_git_status,
                        "writable": is_writable_by_user(entry) if not is_dir else None,
                        "trash_original_path": original_path if isinstance(original_path, str) else None,
                        "trash_original_name": Path(original_path).name if isinstance(original_path, str) else None,
                    })
                except PermissionError:
                    items.append({
                        "name": info["name"],
                        "path": info["path"],
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
            "recycle_bin_root": str(RECYCLE_BIN),
            "is_recycle_bin_root": current_is_recycle_root,
            "is_in_recycle_bin": current_in_recycle_bin,
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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

        head_success, head_bytes = read_file_head(path)
        head_bytes = head_bytes if head_success and isinstance(head_bytes, bytes) else b""
        head_image_mime = detect_image_mime(path, head_bytes)
        is_pdf = not head_image_mime and (
            mimetypes.guess_type(path.name)[0] == "application/pdf"
            or head_bytes.startswith(b"%PDF-")
        )

        size = get_file_size(path)
        size_limit = MAX_PDF_FILE_SIZE if is_pdf else MAX_FILE_SIZE
        if size is not None and size > size_limit:
            return jsonify({"error": f"File too large (max {size_limit // 1024 // 1024}MB)"}), 400

        if is_pdf:
            git_root = find_git_root(path)
            is_git_tracked = is_tracked_by_git(path, git_root) if git_root else False
            return jsonify({
                "path": str(path),
                "content": "",
                "original": "",
                "language": "plaintext",
                "is_image": False,
                "is_pdf": True,
                "is_binary": False,
                "pdf_url": url_for("file_pdf", path=str(path)),
                "is_git": is_git_tracked,
                "writable": False,
            })

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
            "is_pdf": False,
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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

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
        cross_compile_enabled = bool(data.get("cross_compile_enabled", False))
        cross_compile_target = str(data.get("cross_compile_target", "")).strip() or None

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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

        compile_context, error, http_status = get_compile_context_for_path(path)
        if error:
            return jsonify({"error": error}), http_status

        language = str(compile_context["language"])
        tooling_status, http_status = get_compile_tooling_status(language)
        if http_status != 200:
            return jsonify(tooling_status), http_status
        if not tooling_status.get("available"):
            return jsonify(tooling_status), 400

        if cross_compile_enabled:
            if not tooling_status.get("supports_cross_compile"):
                return jsonify({"error": "Cross-compilation is not supported for this language"}), 400
            if not cross_compile_target:
                return jsonify({"error": "Select a cross-compile target"}), 400

        if cross_compile_target:
            available_targets = tooling_status.get("cross_compile_targets") or []
            valid_targets = {str(t.get("value")) for t in available_targets}
            if cross_compile_target not in valid_targets:
                return jsonify({"error": f"Cross-compile target '{cross_compile_target}' is not available"}), 400

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
            cross_compile_target=cross_compile_target,
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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

        size = get_file_size(path)
        if size is not None and size > MAX_FILE_SIZE:
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

    @app.get("/api/file/pdf")
    def file_pdf():
        file_path = request.args.get("path", "")
        if not file_path:
            return jsonify({"error": "No path specified"}), 400

        path, error = resolve_request_path(file_path, "path")
        if error:
            return jsonify({"error": error}), 400

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

        head_success, head_bytes = read_file_head(path)
        if not head_success:
            return jsonify({"error": head_bytes}), 500

        is_pdf = (
            mimetypes.guess_type(path.name)[0] == "application/pdf"
            or head_bytes.startswith(b"%PDF-")
        )
        if not is_pdf:
            return jsonify({"error": "Not a PDF file"}), 400

        size = get_file_size(path)
        if size is not None and size > MAX_PDF_FILE_SIZE:
            return jsonify({"error": f"File too large (max {MAX_PDF_FILE_SIZE // 1024 // 1024}MB)"}), 400

        if os.access(path, os.R_OK):
            response = send_file(
                path,
                mimetype="application/pdf",
                as_attachment=False,
                download_name=path.name,
                conditional=True,
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        success, content_bytes = read_file_bytes(path)
        if not success:
            return jsonify({"error": content_bytes}), 500

        return Response(
            content_bytes,
            mimetype="application/pdf",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="{path.name}"',
            },
        )

    @app.get("/diff/render-file/<path:file_path>")
    @app.get("/render-file/<path:file_path>")
    def render_file(file_path):
        path, error = resolve_request_path(f"/{file_path}", "path")
        if error:
            return jsonify({"error": error}), 400

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

        size = get_file_size(path)
        if size is not None and size > MAX_FILE_SIZE:
            return jsonify({"error": f"File too large (max {MAX_FILE_SIZE // 1024 // 1024}MB)"}), 400

        success, content_bytes = read_file_bytes(path)
        if not success:
            return jsonify({"error": content_bytes}), 500

        network_preview = request.args.get("network", "").strip().lower() in {"1", "true", "yes", "on"}
        if network_preview:
            preview_csp = (
                "sandbox allow-scripts allow-forms allow-modals allow-popups allow-downloads; "
                "default-src 'self' https: data: blob:; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https: blob:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "img-src 'self' https: data: blob:; "
                "font-src 'self' https: data:; "
                "connect-src 'self' https:; "
                "media-src 'self' https: data: blob:; "
                "frame-src 'self' https:; "
                "form-action 'self' https:; "
                "base-uri 'none'"
            )
        else:
            preview_csp = (
                "sandbox allow-scripts; "
                "default-src 'none'; "
                "script-src 'self' 'unsafe-inline' blob:; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; "
                "connect-src 'none'; "
                "media-src 'self' data: blob:; "
                "frame-src 'none'; "
                "form-action 'none'; "
                "base-uri 'none'"
            )

        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in {".md", ".markdown"}:
            content = content_bytes.decode("utf-8", errors="replace")
            content_bytes = _render_markdown_document(content, path.name).encode("utf-8")
            mime_type = "text/html"

        template_root = next((parent for parent in path.parents if parent.name == "templates"), None)
        if (
            template_root is not None
            and mime_type == "text/html"
            and path.suffix.lower() in {".html", ".htm"}
            and any(marker in content_bytes for marker in (b"{%", b"{{", b"{#"))
        ):
            template_name = path.relative_to(template_root).as_posix()
            static_root = template_root.parent / "static"
            route_prefix = request.script_root.rstrip("/") or ("/diff" if request.path.startswith("/diff/") else "")

            def preview_file_url(target: Path) -> str:
                normalized = str(target).lstrip("/")
                encoded_path = "/".join(quote(part) for part in normalized.split("/"))
                return f"{route_prefix}/render-file/{encoded_path}"

            def preview_url_for(endpoint: str, **values) -> str:
                if endpoint.endswith("static"):
                    filename = str(values.pop("filename", "")).lstrip("/\\")
                    static_target = static_root / filename
                    url = preview_file_url(static_target)
                else:
                    url = f"#{endpoint}"

                query_values = {k: v for k, v in values.items() if not k.startswith("_") and v is not None}
                if query_values:
                    url = f"{url}?{urlencode(query_values, doseq=True)}"
                return url

            def preview_static_url(filename: str) -> str:
                return preview_url_for("static", filename=filename)

            template_env = Environment(
                loader=FileSystemLoader(str(template_root)),
                autoescape=select_autoescape(("html", "htm", "xml")),
                undefined=PreviewUndefined,
            )
            template_env.filters["basename"] = lambda value: Path(str(value)).name
            template_env.filters["tojson"] = _preview_tojson
            project_root = template_root.parent
            preview_config = _load_project_preview_config(project_root, dict(app.config))
            template_env.globals.update(
                config=preview_config,
                get_flashed_messages=get_flashed_messages,
                request=request,
                session=session,
                csrf_token=lambda: "",
                static_url=preview_static_url,
                url_for=preview_url_for,
            )
            try:
                content_bytes = template_env.get_template(template_name).render(
                    file_path=str(path),
                    path=str(path),
                ).encode("utf-8")
            except Exception as err:
                return Response(
                    f"Template render failed: {err}",
                    status=500,
                    mimetype="text/plain",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'",
                    },
                )

        return Response(
            content_bytes,
            mimetype=mime_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": preview_csp,
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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

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

        ok, error, status = check_path_is_file(path, allow_symlink=True)
        if not ok:
            return jsonify({"error": error}), status

        if _is_protected_path(path):
            return jsonify({"error": "Cannot delete files in a system directory"}), 403

        if is_in_recycle_bin(path):
            success, message = permanently_delete_path(path)
        else:
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
        str(RECYCLE_BIN),
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

    def _normalize_absolute_pathname(path: Path | str) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(str(path)))
        return Path(os.path.abspath(expanded))

    def _iter_nginx_site_server_names(filename: str) -> list[str]:
        """Return valid concrete server_name entries from an nginx site file."""
        site_path = Path("/etc/nginx/sites-available") / filename
        ok, content = read_file_bytes(site_path)
        if not ok or isinstance(content, str):
            return []

        text = content.decode("utf-8", errors="replace")
        names: list[str] = []
        for match in re.finditer(r"^\s*server_name\s+([^;]+);", text, flags=re.MULTILINE):
            for token in match.group(1).split():
                token = token.strip().lower()
                if token in {"_", "localhost"} or token.startswith("$") or "*" in token:
                    continue
                if re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+", token):
                    names.append(token)
        return names

    def _get_nginx_site_primary_domain(filename: str) -> str | None:
        names = _iter_nginx_site_server_names(filename)
        return names[0] if names else None

    def _parse_missing_certificate_error(stderr: str) -> dict[str, str] | None:
        """Extract missing-certificate details from an nginx config-test failure."""
        cert_match = re.search(
            r'cannot load certificate "([^"]+)".*?No such file or directory',
            stderr,
            flags=re.DOTALL,
        )
        if not cert_match:
            return None

        cert_path = cert_match.group(1)
        domain_match = re.search(r"^/etc/letsencrypt/live/([^/]+)/", cert_path)
        if not domain_match:
            return None

        return {
            "kind": "missing_cert",
            "domain": domain_match.group(1),
            "cert_path": cert_path,
        }

    def _inspect_nginx_site_tls_status(filename: str) -> dict[str, str] | None:
        """Detect whether an enabled nginx site appears to be HTTP-only."""
        site_path = Path("/etc/nginx/sites-available") / filename
        ok, content = read_file_bytes(site_path)
        if not ok or isinstance(content, str):
            return None

        text = content.decode("utf-8", errors="replace")
        has_tls = bool(re.search(r"^\s*ssl_certificate(?:_key)?\s+", text, flags=re.MULTILINE))
        has_tls = has_tls or bool(re.search(r"^\s*listen\s+[^;]*\b443\b[^;]*\bssl\b", text, flags=re.MULTILINE))
        if has_tls:
            return None

        domain = _get_nginx_site_primary_domain(filename)
        if domain:
            return {
                "kind": "http_only",
                "domain": domain,
                "cert_path": f"/etc/letsencrypt/live/{domain}/fullchain.pem",
            }

        return None

    def _configured_dns_reference(request_host: str) -> tuple[str, list[str]]:
        """Choose expected public IPs from env vars, then fall back to the current host."""
        configured_ips = [
            ip.strip()
            for ip in os.getenv("PUBLIC_DNS_EXPECTED_IPS", "").split(",")
            if ip.strip()
        ]
        configured_host = os.getenv("PUBLIC_DNS_REFERENCE_HOST", "").strip().lower()
        request_host = request_host.split(":", 1)[0].strip().lower()

        if configured_ips:
            reference_host = configured_host or "configured public IPs"
            return reference_host, sorted(set(configured_ips))

        if configured_host:
            expected_ips = _resolve_host_ips(configured_host)
            if expected_ips:
                return configured_host, expected_ips

        if request_host:
            request_host_ips = _resolve_host_ips(request_host)
            if _ips_are_public(request_host_ips):
                return request_host, request_host_ips

        return "", []

    def _resolve_host_ips(hostname: str) -> list[str]:
        """Resolve a hostname to a sorted list of unique IP addresses."""
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return []
        except OSError:
            return []

        ips = sorted({info[4][0] for info in infos if info[4] and info[4][0]})
        return ips

    def _ips_are_public(ips: list[str]) -> bool:
        """Return True when every parsed IP is globally reachable."""
        if not ips:
            return False
        try:
            return all(ipaddress.ip_address(ip).is_global for ip in ips)
        except ValueError:
            return False

    def _dns_sanity_check(domain: str, request_host: str) -> dict[str, object]:
        """
        Check whether `domain` appears to point to the same server as the configured reference.

        This is a pragmatic sanity check for the UI flow before requesting a cert.
        """
        reference_host, expected_ips = _configured_dns_reference(request_host)
        target_ips = _resolve_host_ips(domain)

        if not expected_ips:
            return {
                "ok": None,
                "status": "reference_unresolved",
                "reference_host": reference_host,
                "expected_ips": [],
                "target_ips": target_ips,
                "message": f"Could not resolve the current UI host ({reference_host}) for comparison.",
            }

        if not target_ips:
            return {
                "ok": False,
                "status": "unresolved",
                "reference_host": reference_host,
                "expected_ips": expected_ips,
                "target_ips": [],
                "message": f"{domain} does not resolve yet.",
            }

        if set(expected_ips) & set(target_ips):
            return {
                "ok": True,
                "status": "match",
                "reference_host": reference_host,
                "expected_ips": expected_ips,
                "target_ips": target_ips,
                "message": f"{domain} resolves to this server.",
            }

        return {
            "ok": False,
            "status": "mismatch",
            "reference_host": reference_host,
            "expected_ips": expected_ips,
            "target_ips": target_ips,
            "message": f"{domain} resolves, but not to the same IPs as {reference_host}.",
        }

    def _symlink_points_to_target(source: Path, target: Path) -> bool:
        """Detect the destructive case where copying a symlink would overwrite its referent."""
        if not source.is_symlink():
            return False
        try:
            return _normalize_absolute_pathname(source.resolve()) == _normalize_absolute_pathname(target)
        except OSError:
            return False

    def _activation_result(
        message: str | None = None,
        *,
        error: bool = False,
        kind: str | None = None,
        domain: str | None = None,
        cert_path: str | None = None,
    ) -> dict[str, str | bool | None]:
        return {
            "message": message,
            "error": error,
            "kind": kind,
            "domain": domain,
            "cert_path": cert_path,
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
                    stderr = test.stderr.decode("utf-8", errors="replace")
                    missing_cert = _parse_missing_certificate_error(stderr)
                    if missing_cert:
                        site_domain = _get_nginx_site_primary_domain(filename)
                        if site_domain:
                            missing_cert["domain"] = site_domain
                    return _activation_result(
                        f"nginx config test failed: {stderr}",
                        error=True,
                        kind=missing_cert["kind"] if missing_cert else None,
                        domain=missing_cert["domain"] if missing_cert else None,
                        cert_path=missing_cert["cert_path"] if missing_cert else None,
                    )
                reload = subprocess.run(
                    ["sudo", "-n", "systemctl", "reload", "nginx"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                )
                if reload.returncode == 0:
                    http_only = _inspect_nginx_site_tls_status(filename)
                    if http_only:
                        return _activation_result(
                            f"Site {filename} enabled and nginx reloaded. Warning: {http_only['domain']} is currently HTTP-only and traffic is not protected by TLS.",
                            kind=http_only["kind"],
                            domain=http_only["domain"],
                            cert_path=http_only["cert_path"],
                        )
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
        payload = {
            "success": True,
            "path": str(target),
            "activate_message": activation["message"],
            "activate_error": activation["error"],
            "activate_kind": activation["kind"],
            "activate_domain": activation["domain"],
            "activate_cert_path": activation["cert_path"],
        }
        if activation["kind"] in {"http_only", "missing_cert"} and activation["domain"]:
            dns = _dns_sanity_check(str(activation["domain"]), request.host)
            payload.update({
                "dns_ok": dns["ok"],
                "dns_status": dns["status"],
                "dns_message": dns["message"],
                "dns_reference_host": dns["reference_host"],
                "dns_expected_ips": dns["expected_ips"],
                "dns_target_ips": dns["target_ips"],
            })
        return payload

    @app.post("/api/nginx/request-cert")
    def request_nginx_certificate_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        domain = str(data.get("domain", "")).strip().lower()
        filename = str(data.get("filename", "")).strip()
        request_kind = str(data.get("kind", "missing_cert")).strip().lower()

        if not domain:
            return jsonify({"error": "No domain provided"}), 400
        if not re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+", domain):
            return jsonify({"error": "Invalid domain"}), 400
        if not filename:
            filename = domain
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            return jsonify({"error": "Invalid filename"}), 400
        if request_kind not in {"missing_cert", "http_only"}:
            return jsonify({"error": "Invalid certificate request kind"}), 400
        if not command_exists("certbot"):
            return jsonify({"error": "certbot is not installed"}), 500

        site_path = Path("/etc/nginx/sites-available") / filename
        if not site_path.exists() and not site_path.is_symlink():
            return jsonify({"error": f"Nginx site file not found: {filename}"}), 404

        certbot_cmd = ["sudo", "-n", "certbot"]
        if request_kind == "http_only":
            certbot_cmd.extend(["--nginx", "--redirect"])
        else:
            certbot_cmd.extend(["certonly", "--nginx"])
        certbot_cmd.extend(["--non-interactive", "--agree-tos", "-d", domain])
        letsencrypt_email = os.getenv("LETSENCRYPT_EMAIL", "").strip()
        if letsencrypt_email:
            certbot_cmd.extend(["-m", letsencrypt_email])
        else:
            certbot_cmd.append("--register-unsafely-without-email")

        try:
            certbot_result = subprocess.run(
                certbot_cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Certificate request timed out"}), 500
        except Exception as e:
            return jsonify({"error": f"Certificate request failed: {e}"}), 500

        certbot_stdout = certbot_result.stdout.decode("utf-8", errors="replace").strip()
        certbot_stderr = certbot_result.stderr.decode("utf-8", errors="replace").strip()
        certbot_output = "\n".join(part for part in (certbot_stdout, certbot_stderr) if part)

        if certbot_result.returncode != 0:
            return jsonify({
                "error": certbot_output or f"certbot failed with exit code {certbot_result.returncode}",
            }), 500

        activation = _activate_service("/etc/nginx/sites-available", filename, True)
        return jsonify({
            "success": True,
            "message": (
                f"HTTPS was requested and installed for {domain}."
                if request_kind == "http_only"
                else f"Certificate requested for {domain}."
            ),
            "activate_message": activation["message"],
            "activate_error": activation["error"],
            "activate_kind": activation["kind"],
            "activate_domain": activation["domain"],
            "activate_cert_path": activation["cert_path"],
            "certbot_output": certbot_output or None,
        })

    @app.post("/api/nginx/check-dns")
    def check_nginx_dns_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        domain = str(data.get("domain", "")).strip().lower()
        if not domain:
            return jsonify({"error": "No domain provided"}), 400
        if not re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+", domain):
            return jsonify({"error": "Invalid domain"}), 400

        dns = _dns_sanity_check(domain, request.host)
        return jsonify({
            "success": True,
            "dns_ok": dns["ok"],
            "dns_status": dns["status"],
            "dns_message": dns["message"],
            "dns_reference_host": dns["reference_host"],
            "dns_expected_ips": dns["expected_ips"],
            "dns_target_ips": dns["target_ips"],
        })

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

        ok, error, status = check_path_is_file(path, allow_symlink=True)
        if not ok:
            return jsonify({"error": error}), status

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
        if _normalize_absolute_pathname(target) == _normalize_absolute_pathname(path):
            return jsonify({"error": "Source and destination are the same file"}), 400
        if _symlink_points_to_target(path, target):
            return jsonify({"error": "Cannot overwrite a file via a symlink source"}), 400
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
        if _normalize_absolute_pathname(target) == _normalize_absolute_pathname(path):
            return jsonify({"error": "Source and destination are the same directory"}), 400
        if _symlink_points_to_target(path, target):
            return jsonify({"error": "Cannot overwrite a directory via a symlink source"}), 400
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

        ok, error, status = check_path_exists(path)
        if not ok:
            return jsonify({"error": error}), status

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
        if is_recycle_bin_root(path):
            return jsonify({"error": "Cannot delete the recycle bin itself"}), 403
        if _is_protected_path(path):
            return jsonify({"error": "Cannot delete a system directory"}), 403

        if is_in_recycle_bin(path):
            success, message = permanently_delete_path(path)
        else:
            success, message = delete_directory(path)
        if not success:
            return jsonify({"error": message}), 500

        return jsonify({"success": True, "message": message})

    @app.post("/api/recycle-bin/empty")
    def empty_recycle_bin_route():
        csrf = request.headers.get("X-CSRF-Token", "")
        if not validate_csrf_token(csrf):
            return jsonify({"error": "Invalid CSRF token"}), 403

        deleted_items = 0
        removed_metadata = 0
        errors: list[str] = []

        try:
            entries = sorted(RECYCLE_BIN.iterdir(), key=lambda entry: entry.name.lower()) if RECYCLE_BIN.exists() else []
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403

        for entry in entries:
            success, message = permanently_delete_path(entry)
            if success:
                deleted_items += 1
            else:
                errors.append(f"{entry.name}: {message}")

        surviving = {e.name for e in entries if (RECYCLE_BIN / e.name).exists()}

        try:
            meta_files = sorted(RECYCLE_META_DIR.glob("*.json")) if RECYCLE_META_DIR.exists() else []
        except PermissionError:
            meta_files = []
            errors.append("Could not access recycle-bin metadata directory")

        for meta_file in meta_files:
            if meta_file.stem in surviving:
                continue
            success, message = permanently_delete_path(meta_file)
            if success:
                removed_metadata += 1
            else:
                errors.append(f"{meta_file.name}: {message}")

        if errors:
            return jsonify({
                "error": "Failed to fully empty the recycle bin.",
                "details": errors,
                "deleted_items": deleted_items,
                "removed_metadata": removed_metadata,
            }), 500

        parts = [f"Removed {deleted_items} item{'s' if deleted_items != 1 else ''}"]
        if removed_metadata:
            parts.append(
                f"cleaned up {removed_metadata} stale metadata file{'s' if removed_metadata != 1 else ''}"
            )
        return jsonify({
            "success": True,
            "message": ", and ".join(parts) + ".",
            "deleted_items": deleted_items,
            "removed_metadata": removed_metadata,
        })

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
            cleanup_temp_path(result)

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

        success, zip_path = zip_paths(paths)
        if not success:
            return jsonify({"error": zip_path}), 500

        response = send_file(
            zip_path,
            as_attachment=True,
            download_name="selection.zip",
            mimetype="application/zip",
        )

        @response.call_on_close
        def _cleanup():
            cleanup_temp_path(zip_path)

        return response

    @app.get("/api/file/info")
    def file_info():
        raw_path = request.args.get("path", "")
        path, error = resolve_request_path(raw_path)
        if error:
            return jsonify({"error": error}), 400

        ok, error, status = check_path_exists(path)
        if not ok:
            return jsonify({"error": error}), status

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

        ok, error, status = check_path_exists(path)
        if not ok:
            return jsonify({"error": error}), status

        return jsonify(get_extended_file_info(path))

    @app.get("/api/file/zip-info")
    def zip_info():
        zip_path_str = request.args.get("path", "")
        if not zip_path_str:
            return jsonify({"error": "No path specified"}), 400

        zip_path, error = resolve_request_path(zip_path_str, "path")
        if error:
            return jsonify({"error": error}), 400

        ok, error, status = check_path_is_file(zip_path)
        if not ok:
            return jsonify({"error": error}), status

        success, result = get_zip_info(zip_path)
        if not success:
            return jsonify({"error": result}), 400
        return jsonify(result)

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

        ok, error, status = check_path_is_file(zip_path)
        if not ok:
            return jsonify({"error": error}), status

        success, result, http_status = extract_zip_archive(zip_path, mode=mode)
        if not success:
            return jsonify({"error": result}), http_status
        payload = {"success": True}
        payload.update(result)
        return jsonify(payload)

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

        ok, error, status = check_path_is_file(path)
        if not ok:
            return jsonify({"error": error}), status

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
        raw_custom_prompt = data.get("custom_prompt", "")
        if raw_custom_prompt is None:
            raw_custom_prompt = ""
        if not isinstance(raw_custom_prompt, str):
            return jsonify({"error": "Custom prompt must be text"}), 400
        custom_prompt = raw_custom_prompt.strip()
        if len(custom_prompt) > 4000:
            return jsonify({"error": "Custom prompt is too long"}), 400

        # Validate reasoning effort (default to medium for balanced speed/quality)
        VALID_EFFORTS = ("low", "medium", "high", "xhigh")
        reasoning_effort = str(data.get("reasoning_effort", "medium")).strip().lower()
        if reasoning_effort not in VALID_EFFORTS:
            reasoning_effort = "medium"

        if data.get("review_id") and not requested_review_id:
            return jsonify({"error": "Invalid review_id"}), 400

        if original == modified and not custom_prompt:
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
        has_unified_diff = bool(unified_diff.strip())

        if not has_unified_diff and not custom_prompt:
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

        custom_prompt_block = ""
        if custom_prompt:
            custom_prompt_block = (
                "\n\nCustom review request:\n"
                f"{custom_prompt}\n\n"
                "Only report review findings; do not modify files."
            )

        # Build prompt based on case
        if not has_unified_diff:
            review_case = "custom_file"
            if AI_REVIEW_PROVIDER == "openai_sdk":
                review_prompt = f"""Review `{file_path}` ({language}) using this custom request:
{custom_prompt}

Only report review findings; do not modify files.

Current file contents:
```{language}
{modified}
```"""
            else:
                review_prompt = f"""Read `{file_path}` ({language}) and review it using this custom request:
{custom_prompt}

Only report review findings; do not modify files."""
        elif AI_REVIEW_PROVIDER != "openai_sdk" and review_case == "uncommitted_only":
            # Case 3: No diff needed, tell codex to review uncommitted changes
            review_prompt = f"Review uncommitted changes for `{file_path}` ({language}). Use `git diff` to see what changed.{custom_prompt_block}"
        elif AI_REVIEW_PROVIDER != "openai_sdk" and review_case == "uncommitted_plus_edits":
            # Case 4: Diff needed + instruction to check HEAD
            review_prompt = f"""Review this diff for `{file_path}` ({language}).

Note: The file on disk has uncommitted changes not reflected in this diff.
This diff shows changes from HEAD. Use `git show HEAD:{file_path}` to see the baseline.
{custom_prompt_block}

```diff
{unified_diff}
```"""
        else:
            # Cases 2 and non_git: Simple diff prompt
            review_prompt = f"""Review this diff for `{file_path}` ({language}):
{custom_prompt_block}

```diff
{unified_diff}
```"""

        if AI_REVIEW_PROVIDER == "openai_sdk":
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return jsonify({"error": "OpenAI API key not configured"}), 500

            def generate_openai():
                reasoning_open = False
                try:
                    client_kwargs = {"api_key": api_key}
                    if AI_REVIEW_BASE_URL:
                        client_kwargs["base_url"] = AI_REVIEW_BASE_URL
                    client = OpenAI(**client_kwargs)

                    if AI_REVIEW_BASE_URL:
                        # Chat Completions path for alternate endpoints
                        chat_kwargs: dict = {
                            "model": AI_REVIEW_MODEL,
                            "messages": [{"role": "user", "content": review_prompt}],
                            "stream": True,
                        }
                        if AI_REVIEW_REASONING:
                            chat_kwargs["reasoning_effort"] = reasoning_effort
                        stream = client.chat.completions.create(**chat_kwargs)
                        for chunk in stream:
                            delta = chunk.choices[0].delta if chunk.choices else None
                            if delta and delta.content:
                                yield delta.content
                    else:
                        # Responses API path (OpenAI native)
                        resp_kwargs: dict = {
                            "model": AI_REVIEW_MODEL,
                            "input": review_prompt,
                            "stream": True,
                        }
                        if AI_REVIEW_REASONING:
                            resp_kwargs["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
                        stream = client.responses.create(**resp_kwargs)
                        for event in stream:
                            if event.type == "response.reasoning_summary_part.added":
                                if not reasoning_open:
                                    yield "\n\n<details open><summary>AI Reasoning</summary>\n\n"
                                    reasoning_open = True
                            elif event.type == "response.reasoning_summary_text.delta":
                                yield event.delta
                            elif event.type == "response.reasoning_summary_part.done":
                                if reasoning_open:
                                    yield "\n\n</details>\n\n"
                                    reasoning_open = False
                            elif event.type == "response.output_text.delta":
                                yield event.delta
                except Exception as e:
                    yield f"\n\n**Error:** {str(e)}"
                finally:
                    if reasoning_open:
                        yield "\n\n</details>\n\n"

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
            "-m", AI_REVIEW_MODEL,
            "-c", f'model_reasoning_effort="{reasoning_effort}"',
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
