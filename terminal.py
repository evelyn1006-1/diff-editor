"""
Terminal routes and SocketIO handlers.
"""

import glob
import json
import logging
import os
import re
import shlex
import subprocess
import time
import getpass
import pwd
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request
from flask_socketio import emit, join_room, leave_room

from utils.pty_manager import PTYManager

# Set up WebSocket access logging (same file as HTTP, rotation handled by rotatelog)
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

ws_logger = logging.getLogger("diff_editor.ws")
ws_logger.setLevel(logging.INFO)
ws_logger.propagate = False

_ws_handler = logging.FileHandler(LOG_DIR / "access.log")
_ws_handler.setFormatter(logging.Formatter('%(message)s'))
ws_logger.addHandler(_ws_handler)

completion_logger = logging.getLogger("diff_editor.completion")
completion_logger.setLevel(logging.INFO)
completion_logger.propagate = False

_completion_handler = logging.FileHandler(LOG_DIR / "completion.log")
_completion_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
completion_logger.addHandler(_completion_handler)

terminal_bp = Blueprint("terminal", __name__)
pty_manager = PTYManager()
_sudo_nopasswd_cache: dict[str, bool] = {}  # session_id → has passwordless sudo

# Commands that should redirect to diff editor
EDITOR_COMMANDS = {"nano", "vim", "vi", "nvim", "emacs", "pico", "edit"}
CLOUD_COMMAND_REDIRECTS = {
    "codex": ("https://chatgpt.com/codex", "Codex Cloud"),
    "claude": ("https://claude.ai/code", "Claude Code"),
}
# Commands that should open the task manager popup
TASK_MANAGER_COMMANDS = {"top", "htop"}
# Pager commands that should be rewritten to cat (TUI pagers don't work in
# this textbox-based terminal since there is no cursor positioning or
# alternate screen buffer support).
PAGER_COMMANDS = {"less", "more"}
_GIT_PAGER_SUBCOMMANDS = {"log", "diff", "show", "blame"}
_SYSTEMCTL_PAGER_SUBCOMMANDS = {"status", "list-units", "list-timers", "list-sockets", "list-jobs"}
_MAX_COMPLETIONS = 50
_BASH_COMPLETION_SCRIPTS = (
    "/usr/share/bash-completion/bash_completion",
    "/etc/bash_completion",
)
_PATH_COMPLETION_COMMANDS = {
    "cd", "nano", "vim", "vi", "nvim", "emacs", "pico", "edit",
    "cat", "less", "more", "head", "tail",
}


def _short_completion_value(value: str, max_len: int = 80) -> str:
    """Trim potentially long completion context values for logging."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def _preview_completion_items(items: list[str], *, max_items: int = 8, max_len: int = 80) -> list[str]:
    """Trim completion/debug arrays for log readability."""
    return [_short_completion_value(item, max_len=max_len) for item in items[:max_items]]


def _log_completion_event(
    *,
    method: str,
    comp_type: str,
    command: str,
    prefix: str,
    arg_index: int,
    dirs_only: bool,
    base_command: str,
    session_id: str,
    cwd: str,
    line: str | None,
    cursor_raw: str | None,
    cword_raw: str | None,
    raw_words_json: str,
    bash_result: list[str] | None,
    bash_meta: dict[str, object] | None,
    completions: list[str],
) -> None:
    """Write a structured completion debug log entry."""
    raw_words_preview: list[str] = []
    raw_words_count = 0
    if isinstance(bash_meta, dict):
        raw_words_value = bash_meta.get("raw_words")
        if isinstance(raw_words_value, list):
            raw_words_count = len(raw_words_value)
            raw_words_preview = _preview_completion_items(raw_words_value)

    payload = {
        "remote_addr": request.remote_addr or "-",
        "session_id": session_id[:12],
        "method": method,
        "type": comp_type,
        "command": _short_completion_value(command),
        "prefix": _short_completion_value(prefix),
        "arg_index": arg_index,
        "dirs_only": dirs_only,
        "base_command": _short_completion_value(base_command),
        "cwd": _short_completion_value(cwd, 120),
        "line": _short_completion_value(line or "", 160),
        "cursor_raw": cursor_raw,
        "cword_raw": cword_raw,
        "raw_words_json_len": len(raw_words_json or ""),
        "raw_words_count": raw_words_count,
        "raw_words_preview": raw_words_preview,
        "bash_result_count": None if bash_result is None else len(bash_result),
        "bash_status": (bash_meta or {}).get("status"),
        "bash_reason": (bash_meta or {}).get("reason"),
        "bash_returncode": (bash_meta or {}).get("returncode"),
        "bash_script": _short_completion_value(str((bash_meta or {}).get("script") or ""), 120),
        "cursor": (bash_meta or {}).get("cursor"),
        "cword": (bash_meta or {}).get("cword"),
        "fallback_reason": (bash_meta or {}).get("fallback_reason"),
        "result_count": len(completions),
        "results_preview": _preview_completion_items(completions, max_items=5),
    }
    try:
        completion_logger.info(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    except Exception:
        pass


def init_terminal_socketio(socketio):
    """Initialize SocketIO event handlers for terminal."""

    @socketio.on("connect", namespace="/terminal")
    def handle_connect():
        session_id = request.sid
        cwd = os.path.expanduser("~")
        remote_addr = request.remote_addr or "-"

        if pty_manager.create_session(session_id, cwd=cwd, shell="/bin/bash"):
            join_room(session_id)
            pty_session = pty_manager.get_session(session_id)
            emit("connected", {"status": "ok", "cwd": cwd, "token": pty_session.token})
            # Start reading output
            socketio.start_background_task(read_pty_output, socketio, session_id)
            ws_logger.info(
                '%s - - [%s] "WS CONNECT /terminal" - - "-" "-"',
                remote_addr, time.strftime("%d/%b/%Y:%H:%M:%S %z")
            )
        else:
            emit("error", {"message": "Failed to create terminal session"})
            ws_logger.info(
                '%s - - [%s] "WS CONNECT /terminal" FAILED - "-" "-"',
                remote_addr, time.strftime("%d/%b/%Y:%H:%M:%S %z")
            )

    @socketio.on("disconnect", namespace="/terminal")
    def handle_disconnect():
        session_id = request.sid
        remote_addr = request.remote_addr or "-"
        pty_manager.remove_session(session_id)
        _sudo_nopasswd_cache.pop(session_id, None)
        leave_room(session_id)
        ws_logger.info(
            '%s - - [%s] "WS DISCONNECT /terminal" - - "-" "-"',
            remote_addr, time.strftime("%d/%b/%Y:%H:%M:%S %z")
        )

    @socketio.on("input", namespace="/terminal")
    def handle_input(data):
        session_id = request.sid
        session = pty_manager.get_session(session_id)

        if not session or not session.alive:
            emit("error", {"message": "Session not available"})
            return

        text = data.get("data", "")
        if not text:
            return

        # Check if this is an editor command that should be intercepted
        command = text.strip()
        cloud_redirect = check_cloud_redirect(command)
        if cloud_redirect:
            redirect_url, label = cloud_redirect
            emit("output", {"data": f"{command}\nRedirecting to {label}...\n"})
            emit("editor_redirect", {"url": redirect_url})
            session.write("\n")
            return

        redirect_url = check_editor_redirect(command, get_session_cwd(session))
        if redirect_url:
            # Show clean terminal output without sending fake shell input.
            emit("output", {"data": f"{command}\nRedirecting to diff editor...\n"})
            emit("editor_redirect", {"url": redirect_url})
            # Ask shell for a fresh prompt so terminal feels natural after interception.
            session.write("\n")
            return

        # Check for task manager commands (top, htop)
        if check_task_manager_command(command):
            emit("output", {"data": f"{command}\nOpening task manager...\n"})
            emit("task_manager_popup", {})
            session.write("\n")
            return

        # Intercept pager commands and open a scrollable modal instead
        pager_result = get_pager_content(command, get_session_cwd(session), session_id)
        if pager_result is not None:
            title, content = pager_result
            emit("output", {"data": f"{command}\nOpening in pager...\n"})
            emit("pager_popup", {"title": title, "content": content})
            session.write("\n")
            return

        session.write(text)

    @socketio.on("resize", namespace="/terminal")
    def handle_resize(data):
        session_id = request.sid
        session = pty_manager.get_session(session_id)

        if session and session.alive:
            rows = data.get("rows", 24)
            cols = data.get("cols", 80)
            session.resize(rows, cols)

    @socketio.on("signal", namespace="/terminal")
    def handle_signal(data):
        """Handle special signals like Ctrl+C, Ctrl+D."""
        session_id = request.sid
        session = pty_manager.get_session(session_id)

        if not session or not session.alive:
            return

        sig = data.get("signal", "")
        if sig == "SIGINT":
            session.write("\x03")  # Ctrl+C
        elif sig == "EOF":
            session.write("\x04")  # Ctrl+D
        elif sig == "SIGTSTP":
            session.write("\x1a")  # Ctrl+Z
        elif sig == "SIGQUIT":
            session.write("\x1c")  # Ctrl+\


def read_pty_output(socketio, session_id: str):
    """Background task to read PTY output and emit to client."""
    session = pty_manager.get_session(session_id)

    while session and session.alive:
        output = session.read(timeout=0.05)
        if output:
            socketio.emit(
                "output",
                {"data": output},
                namespace="/terminal",
                room=session_id,
            )
        socketio.sleep(0.01)

    # Session ended
    socketio.emit(
        "session_ended",
        {"message": "Terminal session ended"},
        namespace="/terminal",
        room=session_id,
    )


def get_session_cwd(session) -> str:
    """Best-effort current working directory for the shell process."""
    if session and session.pid:
        try:
            return os.readlink(f"/proc/{session.pid}/cwd")
        except OSError:
            pass
    return os.path.expanduser("~")


def parse_intercept_command(command: str) -> tuple[list[str], int] | None:
    """Parse command and return (parts, command-index), handling optional sudo."""
    if not command:
        return None

    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return None

    idx = 1 if parts[0] == "sudo" else 0
    if len(parts) <= idx:
        return None

    return parts, idx


_CLOUD_NONINTERACTIVE_ARGS: dict[str, set[str]] = {
    "codex": {"exec"},
    "claude": {"-p", "--print"},
}


def check_cloud_redirect(command: str) -> tuple[str, str] | None:
    """Return (url, label) for commands that should open cloud UIs."""
    parsed = parse_intercept_command(command)
    if not parsed:
        return None

    parts, idx = parsed
    cmd = parts[idx]
    if cmd not in CLOUD_COMMAND_REDIRECTS:
        return None

    # Allow non-interactive subcommands/flags to run in the terminal.
    non_interactive = _CLOUD_NONINTERACTIVE_ARGS.get(cmd, set())
    remaining = parts[idx + 1:]
    for arg in remaining:
        if arg in non_interactive:
            return None

    return CLOUD_COMMAND_REDIRECTS[cmd]


def check_editor_redirect(command: str, cwd: str | None = None) -> str | None:
    """
    Check if a command is an editor command and return redirect URL.
    Returns None if not an editor command.
    """
    parsed = parse_intercept_command(command)
    if not parsed:
        return None

    parts, idx = parsed

    cmd = parts[idx]

    # Check if it's an editor command
    if cmd not in EDITOR_COMMANDS:
        return None

    # Get the file path if provided (first non-flag argument after editor command)
    args = parts[idx + 1:]
    file_path = next((arg for arg in args if not arg.startswith("-") and not arg.startswith("+")), None)
    if file_path:
        base_cwd = Path(cwd or os.path.expanduser("~"))
        target = Path(os.path.expanduser(file_path))
        if not target.is_absolute():
            target = base_cwd / target

        # Resolve as much as possible, even if file doesn't exist yet.
        try:
            target = target.resolve()
        except OSError:
            target = target.absolute()

        # Absolute path - use /diff/diff because nginx proxies /diff/ to Flask
        return f"/diff/diff?file={quote(str(target))}"

    # Editor without file - redirect to file browser
    return "/diff/"


def check_task_manager_command(command: str) -> bool:
    """Check if command should open task manager popup instead of running."""
    parsed = parse_intercept_command(command)
    if not parsed:
        return False
    parts, idx = parsed
    return parts[idx] in TASK_MANAGER_COMMANDS


def _run_no_pager(run_parts: list[str], cwd: str, timeout: int = 10) -> str | None:
    """Run a command and return its combined output, or None on failure."""
    try:
        result = subprocess.run(
            run_parts,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout or result.stderr
        return output if output.strip() else None
    except (subprocess.TimeoutExpired, OSError):
        return None


_SHELL_OPERATOR_CHARS = frozenset("|&;<>")


def _has_shell_operators(command: str) -> bool:
    """Return True if the command contains unquoted shell operators.

    Uses punctuation_chars so that operators like > are always their own
    token even without surrounding spaces (e.g. >out.txt, >>log, 2>&1).
    Operators inside quotes are absorbed into the surrounding token and
    are not flagged.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lex.whitespace_split = False
    except ValueError:
        return True  # Malformed quoting — treat conservatively

    return any(
        token and all(ch in _SHELL_OPERATOR_CHARS for ch in token)
        for token in lex
    )


_PAGER_MAX_CHARS = 512 * 1024  # 512 KB


def get_pager_content(command: str, cwd: str, session_id: str = "") -> tuple[str, str] | None:
    """Return (title, content) if this is an interceptable pager command.

    Returns None to let the command fall through to the PTY normally
    (e.g. ``less`` with no file arg, piped commands, or unreadable files).
    Content is capped at _PAGER_MAX_CHARS to avoid large Socket.IO payloads.
    """
    if _has_shell_operators(command):
        return None
    parsed = parse_intercept_command(command)
    if not parsed:
        return None
    parts, idx = parsed
    cmd = parts[idx]

    # If the command is prefixed with sudo, only intercept when the user has
    # non-interactive sudo (no password required). The result is cached per
    # session so the check only runs once per connection.
    if idx > 0:
        if session_id not in _sudo_nopasswd_cache:
            try:
                check = subprocess.run(
                    ["sudo", "-n", "true"],
                    capture_output=True,
                    timeout=2,
                )
                _sudo_nopasswd_cache[session_id] = check.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                _sudo_nopasswd_cache[session_id] = False
        if not _sudo_nopasswd_cache.get(session_id):
            return None

    out: tuple[str, str] | None = None

    if cmd == "man":
        args = parts[idx + 1:]
        if args:
            try:
                result = subprocess.run(
                    ["man", "-P", "cat"] + args,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={**os.environ, "MANWIDTH": "80"},
                )
                if result.returncode == 0 and result.stdout:
                    out = (f"man {' '.join(args)}", result.stdout)
            except (subprocess.TimeoutExpired, OSError):
                pass

    elif cmd in PAGER_COMMANDS:
        args = parts[idx + 1:]
        file_arg = next((a for a in reversed(args) if not a.startswith("-")), None)
        if file_arg:
            path = Path(os.path.expanduser(file_arg))
            if not path.is_absolute():
                path = Path(cwd) / path
            try:
                out = (file_arg, path.read_text(errors="replace"))
            except OSError:
                pass

    elif cmd == "git":
        args = parts[idx + 1:]
        if args and args[0] in _GIT_PAGER_SUBCOMMANDS:
            run_parts = parts[:idx + 1] + ["--no-pager"] + args
            content = _run_no_pager(run_parts, cwd)
            if content is not None:
                out = (f"git {args[0]}", content)

    elif cmd == "systemctl":
        args = parts[idx + 1:]
        subcmd = next((a for a in args if not a.startswith("-")), None)
        if subcmd in _SYSTEMCTL_PAGER_SUBCOMMANDS:
            run_parts = parts[:idx + 1] + ["--no-pager"] + args
            content = _run_no_pager(run_parts, cwd)
            if content is not None:
                out = (f"systemctl {subcmd}", content)

    elif cmd == "journalctl":
        args = parts[idx + 1:]
        run_parts = parts[:idx + 1] + ["--no-pager"] + args
        content = _run_no_pager(run_parts, cwd)
        if content is not None:
            out = ("journalctl", content)

    if out is None:
        return None
    title, content = out
    if len(content) > _PAGER_MAX_CHARS:
        content = content[:_PAGER_MAX_CHARS] + "\n\n[… output truncated at 512 KB …]"
    return title, content


@terminal_bp.route("/terminal")
def terminal_view():
    """Render the terminal page."""
    # Optional command to auto-execute on connect
    auto_cmd = request.args.get("cmd", "")
    return render_template("terminal.html", auto_cmd=auto_cmd)


@terminal_bp.route("/terminal/complete")
def complete():
    """Return tab completions for commands, paths, or arguments."""
    prefix = request.args.get("prefix", "")
    comp_type = request.args.get("type", "path")  # "command", "path", or "argument"
    dirs_only = request.args.get("dirs_only", "false") == "true"
    session_id = request.args.get("session_id", "").strip()
    completion_cwd = _get_completion_cwd(session_id)
    line = request.args.get("line")
    cursor_raw = request.args.get("cursor")
    raw_words = request.args.get("raw_words", "")
    cword_raw = request.args.get("cword")
    # For argument completion
    command = request.args.get("command", "")
    base_command = request.args.get("base_command", "")
    try:
        arg_index = int(request.args.get("arg_index", "0"))
    except (TypeError, ValueError):
        arg_index = 0

    completions = []

    bash_completions = None
    bash_meta = {
        "status": "skipped",
        "reason": "builtin-command-preferred" if comp_type == "command" else "not-requested",
        "cursor": None,
        "cword": None,
        "raw_words": [],
        "script": "",
        "returncode": None,
        "fallback_reason": None,
    }
    if comp_type != "command":
        bash_completions, bash_meta = _complete_bash_from_request(
            line=line,
            cursor_raw=cursor_raw,
            cwd=completion_cwd,
            raw_words_json=raw_words,
            cword_raw=cword_raw,
        )

    path_like_prefix = (
        prefix.startswith("/")
        or prefix.startswith("~")
        or prefix.startswith(".")
        or "/" in prefix
    )
    keep_path_fallback = (
        bash_completions == []
        and comp_type == "path"
        and (dirs_only or path_like_prefix or base_command in _PATH_COMPLETION_COMMANDS)
    )
    keep_builtin_fallback = (
        bash_completions == []
        and comp_type == "argument"
        and command in {"pip", "pip3", "npm"}
    )
    if keep_path_fallback:
        bash_meta["fallback_reason"] = (
            "empty-bash-result-for-path-context"
            if not base_command else f"empty-bash-result-for-path-command:{base_command}"
        )
    if keep_builtin_fallback:
        bash_meta["fallback_reason"] = f"empty-bash-result-for-{command}"
    if bash_completions is not None and not keep_builtin_fallback and not keep_path_fallback:
        _log_completion_event(
            method="bash",
            comp_type=comp_type,
            command=command,
            prefix=prefix,
            arg_index=arg_index,
            dirs_only=dirs_only,
            base_command=base_command,
            session_id=session_id,
            cwd=completion_cwd,
            line=line,
            cursor_raw=cursor_raw,
            cword_raw=cword_raw,
            raw_words_json=raw_words,
            bash_result=bash_completions,
            bash_meta=bash_meta,
            completions=bash_completions,
        )
        return jsonify(bash_completions)

    if comp_type == "command":
        # Use compgen to get command completions
        try:
            result = subprocess.run(
                ["bash", "-c", f"compgen -c -- {shlex.quote(prefix)}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                completions = [c for c in result.stdout.strip().split("\n") if c]
                completions = sorted(set(completions))[:_MAX_COMPLETIONS]
        except (subprocess.TimeoutExpired, OSError):
            pass

    elif comp_type == "path":
        completions = _complete_path(prefix, dirs_only, completion_cwd)

    elif comp_type == "argument":
        completions = _complete_argument(command, prefix, arg_index, completion_cwd)

    method = {
        "command": "builtin-command",
        "path": "builtin-path",
        "argument": "builtin-argument",
    }.get(comp_type, "builtin")
    if keep_builtin_fallback or keep_path_fallback:
        method += "-fallback-after-empty-bash"
    _log_completion_event(
        method=method,
        comp_type=comp_type,
        command=command,
        prefix=prefix,
        arg_index=arg_index,
        dirs_only=dirs_only,
        base_command=base_command,
        session_id=session_id,
        cwd=completion_cwd,
        line=line,
        cursor_raw=cursor_raw,
        cword_raw=cword_raw,
        raw_words_json=raw_words,
        bash_result=bash_completions,
        bash_meta=bash_meta,
        completions=completions,
    )
    return jsonify(completions)


def _find_bash_completion_script() -> str | None:
    """Return the first available bash-completion entrypoint."""
    for candidate in _BASH_COMPLETION_SCRIPTS:
        if os.path.exists(candidate):
            return candidate
    return None


def _normalize_bash_completion_words(words: object, cword_raw: object) -> tuple[list[str], int] | None:
    """Validate raw completion words sent by the frontend."""
    if not isinstance(words, list):
        return None
    if not all(isinstance(word, str) for word in words):
        return None

    comp_words = list(words) or [""]
    try:
        cword = int(cword_raw)
    except (TypeError, ValueError):
        cword = len(comp_words) - 1

    if cword < 0:
        cword = 0
    if cword >= len(comp_words):
        if cword == len(comp_words):
            comp_words.append("")
        else:
            cword = len(comp_words) - 1

    return comp_words, cword


def _dedupe_completions(items: list[str]) -> list[str]:
    """Deduplicate completions while preserving order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
        if len(ordered) >= _MAX_COMPLETIONS:
            break
    return ordered


def _complete_bash_from_request(
    *,
    line: str | None,
    cursor_raw: str | None,
    cwd: str,
    raw_words_json: str,
    cword_raw: str | None,
) -> tuple[list[str] | None, dict[str, object]]:
    """Best-effort real Bash completion using a helper shell, or None on failure."""
    meta: dict[str, object] = {
        "status": "skipped",
        "reason": "missing-line-or-cursor",
        "cursor": None,
        "cword": None,
        "raw_words": [],
        "script": "",
        "returncode": None,
        "fallback_reason": None,
    }
    if line is None or cursor_raw is None:
        return None, meta

    try:
        cursor = int(cursor_raw)
    except (TypeError, ValueError):
        meta["status"] = "invalid-request"
        meta["reason"] = "invalid-cursor"
        return None, meta

    if cursor < 0:
        meta["status"] = "invalid-request"
        meta["reason"] = "negative-cursor"
        return None, meta

    try:
        parsed_words = json.loads(raw_words_json) if raw_words_json else [""]
    except json.JSONDecodeError:
        meta["status"] = "invalid-request"
        meta["reason"] = "invalid-raw-words-json"
        return None, meta

    normalized = _normalize_bash_completion_words(parsed_words, cword_raw)
    if normalized is None:
        meta["status"] = "invalid-request"
        meta["reason"] = "invalid-word-array"
        return None, meta
    comp_words, cword = normalized
    meta["status"] = "requested"
    meta["reason"] = "ready"
    meta["cursor"] = cursor
    meta["cword"] = cword
    meta["raw_words"] = comp_words

    return _complete_with_bash(
        line=line,
        cursor=cursor,
        cwd=cwd,
        comp_words=comp_words,
        cword=cword,
        meta=meta,
    )


def _complete_with_bash(
    *,
    line: str,
    cursor: int,
    cwd: str,
    comp_words: list[str],
    cword: int,
    meta: dict[str, object],
) -> tuple[list[str] | None, dict[str, object]]:
    """Run bash-completion in a helper shell using the terminal session cwd."""
    bash_completion_script = _find_bash_completion_script()
    meta["script"] = bash_completion_script or ""
    if not bash_completion_script:
        meta["status"] = "unavailable"
        meta["reason"] = "missing-bash-completion-script"
        return None, meta

    cursor = max(0, min(cursor, len(line)))
    cwd = cwd or os.path.expanduser("~")
    array_literal = " ".join(shlex.quote(word) for word in comp_words)
    shell_script = f"""
source {shlex.quote(bash_completion_script)} >/dev/null 2>&1 || exit 125
compopt() {{
    builtin compopt "$@" 2>/dev/null || return 0
}}
cd {shlex.quote(cwd)} >/dev/null 2>&1 || exit 126
COMP_LINE={shlex.quote(line)}
COMP_POINT={cursor}
COMP_TYPE=9
COMP_KEY=9
COMP_WORDS=({array_literal})
COMP_CWORD={cword}
_command_offset 0 >/dev/null 2>&1 || true
printf '%s\\0' "${{COMPREPLY[@]}}"
"""

    try:
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", shell_script],
            capture_output=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        meta["status"] = "error"
        meta["reason"] = "helper-failed"
        return None, meta

    meta["returncode"] = result.returncode

    if result.returncode == 125:
        meta["status"] = "unavailable"
        meta["reason"] = "source-bash-completion-failed"
        return None, meta
    if result.returncode == 126:
        meta["status"] = "unavailable"
        meta["reason"] = "cwd-unavailable"
        return None, meta

    if not result.stdout:
        meta["status"] = "empty"
        meta["reason"] = "no-matches"
        return [], meta

    completions = [
        item.decode("utf-8", errors="replace")
        for item in result.stdout.split(b"\0")
        if item
    ]
    completions = _dedupe_completions(completions)
    if not completions:
        meta["status"] = "empty"
        meta["reason"] = "no-matches"
        return [], meta
    meta["status"] = "success"
    meta["reason"] = "matches"
    return completions, meta


def _get_completion_cwd(session_id: str) -> str:
    """Resolve completion cwd from active terminal session."""
    if session_id:
        session = pty_manager.get_session(session_id)
        if session and session.alive:
            return get_session_cwd(session)
    return os.path.expanduser("~")


def _format_completion_path(full_path: str, prefix: str, cwd: str) -> str:
    """Format path completion to match the user's input style."""
    if prefix.startswith("~"):
        home = os.path.expanduser("~")
        if full_path.startswith(home):
            return "~" + full_path[len(home):]
        return full_path

    if os.path.isabs(prefix):
        return full_path

    rel_path = os.path.relpath(full_path, cwd)
    if prefix.startswith("./") and not rel_path.startswith("../") and not rel_path.startswith("./"):
        rel_path = f"./{rel_path}"
    return rel_path


def _complete_path(prefix: str, dirs_only: bool = False, cwd: str | None = None) -> list[str]:
    """Complete filesystem paths, optionally filtering to directories only."""
    if not prefix:
        prefix = "./"

    base_cwd = os.path.abspath(cwd or os.path.expanduser("~"))
    expanded = os.path.expanduser(prefix)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base_cwd, expanded)

    completions = []

    if expanded.endswith("/") and os.path.isdir(expanded):
        try:
            entries = os.listdir(expanded)
            for e in sorted(entries)[:100]:
                full_path = os.path.join(expanded, e)
                is_dir = os.path.isdir(full_path)
                if dirs_only and not is_dir:
                    continue
                formatted = _format_completion_path(full_path, prefix, base_cwd)
                completions.append(formatted + ("/" if is_dir else ""))
        except OSError:
            pass
    else:
        try:
            matches = glob.glob(expanded + "*")
            for m in sorted(matches)[:100]:
                is_dir = os.path.isdir(m)
                if dirs_only and not is_dir:
                    continue
                formatted = _format_completion_path(m, prefix, base_cwd)
                completions.append(formatted + ("/" if is_dir else ""))
        except OSError:
            pass

    return sorted(set(completions))[:50]


def _complete_argument(command: str, prefix: str, arg_index: int, cwd: str | None = None) -> list[str]:
    """Complete arguments for specific commands."""
    completions = []
    subcommand = request.args.get("subcommand", "")

    if command == "systemctl":
        completions = _complete_systemctl(prefix, arg_index)
    elif command == "sudo":
        completions = _complete_sudo(prefix, arg_index)
    elif command == "git":
        if arg_index == 0:
            completions = _complete_git(prefix, arg_index)
        else:
            completions = _complete_git_context(subcommand, prefix, cwd)
    elif command in ("apt", "apt-get"):
        completions = _complete_apt(prefix, arg_index)
    elif command == "ssh":
        completions = _complete_ssh(prefix)
    elif command in ("pip", "pip3"):
        completions = _complete_pip(command, subcommand, prefix, arg_index, cwd)
    elif command == "npm":
        completions = _complete_npm(subcommand, prefix, arg_index, cwd)

    return completions


def _complete_systemctl(prefix: str, arg_index: int) -> list[str]:
    """Complete systemctl subcommands and unit names."""
    subcommands = [
        "start", "stop", "restart", "reload", "status", "enable", "disable",
        "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
        "daemon-reload", "mask", "unmask", "edit", "cat", "show",
    ]

    if arg_index == 0:
        # Complete subcommand
        return sorted([s for s in subcommands if s.startswith(prefix)])[:50]
    else:
        # Complete unit names
        try:
            result = subprocess.run(
                ["systemctl", "list-unit-files", "--no-legend", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                units = []
                for line in result.stdout.strip().split("\n"):
                    if line:
                        unit = line.split()[0]
                        if unit.startswith(prefix):
                            units.append(unit)
                return sorted(units)[:50]
        except (subprocess.TimeoutExpired, OSError):
            pass
    return []


def _complete_sudo(prefix: str, arg_index: int) -> list[str]:
    """Complete sudo options."""
    del arg_index
    options = [
        "-A", "-b", "-E", "-e", "-H", "-h", "-i", "-K", "-k", "-l", "-n",
        "-P", "-p", "-S", "-s", "-U", "-u", "-V", "-v",
        "--askpass", "--background", "--close-from", "--preserve-env",
        "--edit", "--set-home", "--help", "--login", "--non-interactive",
        "--preserve-groups", "--prompt", "--stdin", "--shell", "--other-user",
        "--user", "--version", "--validate", "--remove-timestamp", "--reset-timestamp",
    ]

    if not prefix:
        return sorted(options)[:50]
    return sorted([o for o in options if o.startswith(prefix)])[:50]


def _complete_git(prefix: str, arg_index: int) -> list[str]:
    """Complete git subcommands, branches, remotes, etc."""
    subcommands = [
        "add", "branch", "checkout", "clone", "commit", "diff", "fetch",
        "init", "log", "merge", "pull", "push", "rebase", "remote", "reset",
        "restore", "show", "stash", "status", "switch", "tag",
    ]

    if arg_index == 0:
        # Complete subcommand
        return sorted([s for s in subcommands if s.startswith(prefix)])[:50]

    # For further args, we'd need to know the subcommand
    # This is passed as part of the command context from frontend
    return []


def _complete_git_context(subcommand: str, prefix: str, cwd: str | None = None) -> list[str]:
    """Complete git arguments based on subcommand context."""
    completions = []

    if subcommand in ("checkout", "switch", "branch", "merge", "rebase"):
        # Complete branch names
        ref_roots = ["refs/heads", "refs/remotes"]
        if subcommand in ("checkout", "switch"):
            ref_roots.append("refs/tags")
        try:
            result = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname:short)", *ref_roots],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=2,
            )
            if result.returncode == 0:
                branches = []
                for branch in (b.strip() for b in result.stdout.strip().split("\n")):
                    if not branch or branch.endswith("/HEAD"):
                        continue
                    branches.append(branch)
                completions = [b for b in branches if b.startswith(prefix)]
        except (subprocess.TimeoutExpired, OSError):
            pass

    elif subcommand == "remote":
        # Complete remote names
        try:
            result = subprocess.run(
                ["git", "remote"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=2,
            )
            if result.returncode == 0:
                remotes = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]
                completions = [r for r in remotes if r.startswith(prefix)]
        except (subprocess.TimeoutExpired, OSError):
            pass

    elif subcommand == "add":
        # Complete modified/untracked files
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=2,
            )
            if result.returncode == 0:
                files = []
                for line in result.stdout.strip().split("\n"):
                    if line and len(line) > 3:
                        f = line[3:].strip()
                        if f.startswith(prefix):
                            files.append(f)
                completions = files
        except (subprocess.TimeoutExpired, OSError):
            pass

    return sorted(completions)[:50]


def _complete_apt(prefix: str, arg_index: int) -> list[str]:
    """Complete apt subcommands and package names."""
    subcommands = [
        "install", "remove", "purge", "update", "upgrade", "full-upgrade",
        "search", "show", "list", "autoremove", "clean", "autoclean",
    ]

    if arg_index == 0:
        return sorted([s for s in subcommands if s.startswith(prefix)])[:50]
    else:
        # Complete package names
        if not prefix or len(prefix) < 2:
            return []  # Don't search with very short prefixes
        try:
            result = subprocess.run(
                ["apt-cache", "pkgnames", prefix],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                packages = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
                return sorted(packages)[:50]
        except (subprocess.TimeoutExpired, OSError):
            pass
    return []


def _complete_ssh(prefix: str) -> list[str]:
    """Complete SSH hostnames from config and known_hosts."""
    hosts = set()

    # Parse ~/.ssh/config
    ssh_config = Path.home() / ".ssh" / "config"
    if ssh_config.exists():
        try:
            with open(ssh_config) as f:
                for line in f:
                    line = line.strip().lower()
                    if line.startswith("host ") and "*" not in line:
                        for host in line[5:].split():
                            if host.startswith(prefix):
                                hosts.add(host)
        except OSError:
            pass

    # Parse ~/.ssh/known_hosts
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    if known_hosts.exists():
        try:
            with open(known_hosts) as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    # Format: hostname[,ip] key-type key
                    host_part = line.split()[0] if line.split() else ""
                    # Handle hashed entries (start with |)
                    if host_part.startswith("|"):
                        continue
                    for h in host_part.split(","):
                        h = h.strip("[]").split(":")[0]  # Remove brackets and port
                        if h and h.startswith(prefix):
                            hosts.add(h)
        except OSError:
            pass

    return sorted(hosts)[:50]


def _complete_pip(pip_cmd: str, subcommand: str, prefix: str, arg_index: int, cwd: str | None = None) -> list[str]:
    """Complete pip subcommands/options with local hints and global package fallback."""
    subcommands = [
        "install", "uninstall", "list", "show", "freeze", "check", "config", "search",
        "cache", "index", "download", "wheel", "hash", "completion", "debug",
        "inspect", "help",
    ]
    global_opts = [
        "-h", "--help", "-V", "--version", "-q", "--quiet", "-v", "--verbose",
        "--no-cache-dir", "--disable-pip-version-check", "--proxy", "--timeout",
        "--retries", "--trusted-host", "--cert", "--client-cert", "--exists-action",
    ]
    install_opts = [
        "-r", "--requirement", "-e", "--editable", "-U", "--upgrade",
        "--upgrade-strategy", "--pre", "--no-deps", "--user", "--target",
        "--root", "--prefix", "--force-reinstall", "--ignore-installed",
        "--break-system-packages",
    ]

    if prefix.startswith("-"):
        opts = global_opts + (install_opts if subcommand in ("install", "download", "wheel") else [])
        return sorted(set(o for o in opts if o.startswith(prefix)))[:50]

    if arg_index == 0:
        return sorted([s for s in subcommands if s.startswith(prefix)])[:50]

    completions: set[str] = set()

    # Prefer environment-visible installed packages first, then fall back to python -m pip.
    pip_candidates = [[pip_cmd, "list", "--format=freeze"]]
    if pip_cmd != "pip":
        pip_candidates.append(["pip", "list", "--format=freeze"])
    if pip_cmd != "pip3":
        pip_candidates.append(["pip3", "list", "--format=freeze"])
    pip_candidates.extend(
        [
            ["python3", "-m", "pip", "list", "--format=freeze"],
            ["python", "-m", "pip", "list", "--format=freeze"],
        ]
    )

    seen_cmds: set[tuple[str, ...]] = set()
    cwd_candidates = [cwd, os.path.expanduser("~")]
    for cmd in pip_candidates:
        cmd_key = tuple(cmd)
        if cmd_key in seen_cmds:
            continue
        seen_cmds.add(cmd_key)
        for run_cwd in cwd_candidates:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=run_cwd,
                    timeout=2,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "==" in line:
                    name = line.split("==", 1)[0].strip()
                elif " @ " in line:
                    name = line.split(" @ ", 1)[0].strip()
                else:
                    name = line.split()[0].strip()
                if name and name.startswith(prefix):
                    completions.add(name)

    # Helpful when completing `pip install -r req...`
    base_cwd = Path(cwd or os.path.expanduser("~"))
    for req_file in ("requirements.txt", "requirements-dev.txt", "requirements/base.txt", "requirements-prod.txt"):
        if req_file.startswith(prefix) and (base_cwd / req_file).exists():
            completions.add(req_file)

    return sorted(completions)[:50]


def _load_package_json(cwd: str | None) -> dict:
    """Load package.json from cwd if present."""
    base_cwd = Path(cwd or os.path.expanduser("~"))
    package_json = base_cwd / "package.json"
    if not package_json.exists():
        return {}
    try:
        with open(package_json) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _complete_npm(subcommand: str, prefix: str, arg_index: int, cwd: str | None = None) -> list[str]:
    """Complete npm with local package.json hints and global package fallback."""
    subcommands = [
        "install", "i", "uninstall", "remove", "rm", "update", "up",
        "run", "run-script", "test", "start", "stop", "build", "publish",
        "pack", "link", "list", "ls", "outdated", "audit", "init", "create",
        "login", "logout", "whoami", "ci", "exec", "cache", "config",
    ]
    npm_opts = [
        "-g", "--global", "-D", "--save-dev", "-O", "--save-optional",
        "--save-peer", "--no-save", "--omit", "--include", "--workspace",
        "--workspaces", "--if-present", "--silent", "--yes", "--force",
    ]

    if arg_index == 0:
        return sorted([s for s in subcommands if s.startswith(prefix)])[:50]

    if prefix.startswith("-"):
        return sorted([o for o in npm_opts if o.startswith(prefix)])[:50]

    completions: set[str] = set()
    package_data = _load_package_json(cwd)

    if subcommand in ("run", "run-script"):
        scripts = package_data.get("scripts", {})
        if isinstance(scripts, dict):
            for name in scripts:
                if isinstance(name, str) and name.startswith(prefix):
                    completions.add(name)

    if subcommand in ("install", "i", "uninstall", "remove", "rm", "update", "up"):
        dep_sections = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
        for section in dep_sections:
            deps = package_data.get(section, {})
            if isinstance(deps, dict):
                for dep in deps:
                    if isinstance(dep, str) and dep.startswith(prefix):
                        completions.add(dep)

    # Global fallback for package-targeting npm commands (useful outside project dirs).
    if subcommand in ("install", "i", "uninstall", "remove", "rm", "update", "up", "exec"):
        completions.update(_complete_npm_global_packages(prefix, cwd))

    return sorted(completions)[:50]


def _complete_npm_global_packages(prefix: str, cwd: str | None = None) -> set[str]:
    """Return globally installed npm package names matching prefix."""
    completions: set[str] = set()
    cwd_candidates = [cwd, os.path.expanduser("~")]

    for run_cwd in cwd_candidates:
        try:
            result = subprocess.run(
                ["npm", "ls", "-g", "--depth=0", "--json"],
                capture_output=True,
                text=True,
                cwd=run_cwd,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            result = None

        # npm can exit non-zero even when useful JSON is present.
        if result and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                data = None
            deps = data.get("dependencies", {}) if isinstance(data, dict) else {}
            if isinstance(deps, dict):
                for dep_name in deps:
                    if isinstance(dep_name, str) and dep_name.startswith(prefix):
                        completions.add(dep_name)

        # Fallback: inspect global node_modules directly.
        try:
            root_result = subprocess.run(
                ["npm", "root", "-g"],
                capture_output=True,
                text=True,
                cwd=run_cwd,
                timeout=2,
            )
        except (subprocess.TimeoutExpired, OSError):
            root_result = None

        if root_result and root_result.returncode == 0:
            root_path = Path(root_result.stdout.strip())
            if root_path.is_dir():
                try:
                    for entry in root_path.iterdir():
                        name = entry.name
                        if not name or name.startswith("."):
                            continue
                        if name.startswith("@") and entry.is_dir():
                            for scoped in entry.iterdir():
                                scoped_name = f"{name}/{scoped.name}"
                                if scoped.is_dir() and scoped_name.startswith(prefix):
                                    completions.add(scoped_name)
                        elif entry.is_dir() and name.startswith(prefix):
                            completions.add(name)
                except OSError:
                    pass

    return completions


# ─────────────────────────────────────────────────────────────────────────────
# Task Manager Endpoints
# ─────────────────────────────────────────────────────────────────────────────

# Store previous CPU times for calculating usage percentage (overall + per-core)
_prev_cpu_times: dict[str, dict[str, int]] = {}

# Per-process CPU time tracking for delta-based %cpu calculation
_prev_proc_cpu: dict[int, tuple[int, int, float]] = {}  # pid -> (starttime, total_ticks, timestamp)
_CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
_uid_to_name: dict[int, str] = {}
_total_mem_kb: int | None = None
_boot_time: int | None = None

# EMA smoothing for CPU display and sort stability
# Display (α=0.88): responsive, nearly raw — what the user sees
# Sort    (α=0.6):  more inertia — used as tiebreaker when display values match
_DISPLAY_CPU_ALPHA = 0.88
_SORT_CPU_ALPHA = 0.6
_ema_display_cpu: dict[int, float] = {}  # pid -> smoothed display cpu (full precision)
_ema_sort_cpu: dict[int, float] = {}     # pid -> smoothed sort cpu (full precision)

# Cache for whether a systemd unit has ExecReload defined (one batched lookup, then cached)
_unit_has_reload: dict[str, bool] = {}


def _get_cpu_stats() -> dict:
    """Get CPU stats including overall %, breakdown, and per-core usage."""
    global _prev_cpu_times
    result = {
        "percent": 0.0,
        "breakdown": {"user": 0, "system": 0, "nice": 0, "idle": 0, "iowait": 0},
        "cores": [],
    }

    try:
        with open("/proc/stat") as f:
            lines = f.readlines()

        for line in lines:
            parts = line.split()
            if not parts:
                continue

            name = parts[0]
            if not name.startswith("cpu"):
                continue

            # cpu user nice system idle iowait irq softirq steal
            times = [int(p) for p in parts[1:8]]
            total = sum(times)
            idle = times[3] + times[4]  # idle + iowait

            prev = _prev_cpu_times.get(name, {})
            prev_total = prev.get("total", 0)
            prev_idle = prev.get("idle", 0)

            _prev_cpu_times[name] = {"total": total, "idle": idle, "times": times}

            if prev_total == 0:
                usage = 0.0
            else:
                total_diff = total - prev_total
                idle_diff = idle - prev_idle
                usage = round((1.0 - idle_diff / total_diff) * 100, 1) if total_diff > 0 else 0.0

            if name == "cpu":
                # Overall CPU
                result["percent"] = usage
                # Calculate breakdown percentages
                if total > 0:
                    result["breakdown"] = {
                        "user": round(times[0] / total * 100, 1),
                        "nice": round(times[1] / total * 100, 1),
                        "system": round(times[2] / total * 100, 1),
                        "idle": round(times[3] / total * 100, 1),
                        "iowait": round(times[4] / total * 100, 1),
                    }
            else:
                # Per-core (cpu0, cpu1, etc.)
                result["cores"].append({"name": name, "percent": usage})

    except (OSError, ValueError, IndexError):
        pass

    return result


def _get_load_average() -> dict:
    """Get load average from /proc/loadavg."""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return {
            "1min": float(parts[0]),
            "5min": float(parts[1]),
            "15min": float(parts[2]),
        }
    except (OSError, ValueError, IndexError):
        return {"1min": 0, "5min": 0, "15min": 0}


def _get_uptime() -> dict:
    """Get system uptime from /proc/uptime."""
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = float(f.read().split()[0])
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        return {
            "seconds": uptime_seconds,
            "formatted": f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m",
        }
    except (OSError, ValueError, IndexError):
        return {"seconds": 0, "formatted": "unknown"}


def _get_total_mem_kb() -> int:
    """Read total physical memory from /proc/meminfo (cached after first call)."""
    global _total_mem_kb
    if _total_mem_kb is None:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        _total_mem_kb = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            pass
        if _total_mem_kb is None:
            _total_mem_kb = 1
    return _total_mem_kb


def _get_boot_time() -> int:
    """Read system boot time from /proc/stat (cached after first call)."""
    global _boot_time
    if _boot_time is None:
        try:
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        _boot_time = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            pass
        if _boot_time is None:
            _boot_time = 0
    return _boot_time


def _get_username(uid: int) -> str:
    """Resolve UID to username with caching."""
    name = _uid_to_name.get(uid)
    if name is None:
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = str(uid)
        _uid_to_name[uid] = name
    return name


def _read_proc_stat(pid: int) -> tuple[str, int, int, int, int, int] | None:
    """Parse /proc/{pid}/stat -> (state, ppid, utime, stime, starttime, rss_pages)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    # comm field can contain spaces/parens — find the last ')' to skip it safely
    i = data.rfind(")")
    if i == -1:
        return None
    fields = data[i + 2:].split()
    if len(fields) < 22:
        return None
    try:
        return (fields[0], int(fields[1]), int(fields[11]), int(fields[12]),
                int(fields[19]), int(fields[21]))
    except (ValueError, IndexError):
        return None


def _get_memory_info() -> dict:
    """Get memory stats from /proc/meminfo including breakdown."""
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    val = int(parts[1])  # in kB
                    mem[key] = val

        total_kb = mem.get("MemTotal", 0)
        available_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
        used_kb = total_kb - available_kb
        buffers_kb = mem.get("Buffers", 0)
        cached_kb = mem.get("Cached", 0) + mem.get("SReclaimable", 0)
        swap_total_kb = mem.get("SwapTotal", 0)
        swap_free_kb = mem.get("SwapFree", 0)
        swap_used_kb = swap_total_kb - swap_free_kb

        return {
            "total_gb": round(total_kb / 1024 / 1024, 2),
            "used_gb": round(used_kb / 1024 / 1024, 2),
            "percent": round(used_kb / total_kb * 100, 1) if total_kb > 0 else 0.0,
            "buffers_mb": round(buffers_kb / 1024, 1),
            "cached_mb": round(cached_kb / 1024, 1),
            "swap_total_gb": round(swap_total_kb / 1024 / 1024, 2),
            "swap_used_gb": round(swap_used_kb / 1024 / 1024, 2),
            "swap_percent": round(swap_used_kb / swap_total_kb * 100, 1) if swap_total_kb > 0 else 0.0,
        }
    except (OSError, ValueError):
        return {
            "total_gb": 0, "used_gb": 0, "percent": 0,
            "buffers_mb": 0, "cached_mb": 0,
            "swap_total_gb": 0, "swap_used_gb": 0, "swap_percent": 0,
        }


def _get_processes() -> tuple[list[dict], dict]:
    """Get process list and state counts directly from /proc.

    Returns (processes, process_counts).  Replaces both the old ps-based
    _get_processes and _get_process_counts with a single /proc walk, which
    avoids the whitespace-parsing bugs that ps output is prone to and
    eliminates two subprocess spawns per refresh cycle.
    """
    global _prev_proc_cpu, _ema_display_cpu, _ema_sort_cpu

    processes: list[dict] = []
    counts = {"total": 0, "running": 0, "sleeping": 0, "stopped": 0, "zombie": 0}
    now = time.time()
    total_mem_kb = _get_total_mem_kb()
    current_cpu: dict[int, tuple[int, float]] = {}
    new_ema_display: dict[int, float] = {}
    new_ema_sort: dict[int, float] = {}

    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return processes, counts

    for pid in pids:
        stat_data = _read_proc_stat(pid)
        if stat_data is None:
            continue
        state, ppid, utime, stime, starttime_ticks, rss_pages = stat_data
        total_ticks = utime + stime

        # Count process states
        counts["total"] += 1
        if state == "R":
            counts["running"] += 1
        elif state in ("S", "D", "I"):
            counts["sleeping"] += 1
        elif state == "T":
            counts["stopped"] += 1
        elif state == "Z":
            counts["zombie"] += 1

        # Delta-based %cpu (shows current usage, not lifetime average)
        # Include starttime in the cache key so PID reuse doesn't contaminate data.
        prev = _prev_proc_cpu.get(pid)
        pid_reused = prev is not None and prev[0] != starttime_ticks
        if prev and not pid_reused:
            _prev_start, prev_ticks, prev_time = prev
            elapsed = now - prev_time
            if elapsed > 0:
                cpu_raw = max(0.0, (total_ticks - prev_ticks) / (_CLK_TCK * elapsed) * 100)
            else:
                cpu_raw = 0.0
        else:
            cpu_raw = 0.0
        current_cpu[pid] = (starttime_ticks, total_ticks, now)

        # EMA smoothing — display keeps full float precision, sort rounds to 4dp
        # so decaying processes reach exactly 0 instead of lingering indefinitely.
        # On PID reuse, discard stale EMA state.
        prev_display = None if pid_reused else _ema_display_cpu.get(pid)
        prev_sort = None if pid_reused else _ema_sort_cpu.get(pid)
        display_cpu = (_DISPLAY_CPU_ALPHA * cpu_raw + (1 - _DISPLAY_CPU_ALPHA) * prev_display
                       if prev_display is not None else cpu_raw)
        sort_cpu = (_SORT_CPU_ALPHA * cpu_raw + (1 - _SORT_CPU_ALPHA) * prev_sort
                    if prev_sort is not None else cpu_raw)
        sort_cpu = round(sort_cpu, 4)
        new_ema_display[pid] = display_cpu
        new_ema_sort[pid] = sort_cpu
        cpu_percent = round(display_cpu, 1)

        # %mem from RSS pages
        rss_kb = rss_pages * _PAGE_SIZE // 1024
        mem_percent = round(rss_kb / total_mem_kb * 100, 1) if total_mem_kb > 0 else 0.0

        # Username from /proc/{pid} ownership
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            uid = 0
        user = _get_username(uid)

        # Command from /proc/{pid}/cmdline (null-separated args)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline_raw = f.read()
        except OSError:
            cmdline_raw = b""
        if cmdline_raw:
            command = cmdline_raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        else:
            # Kernel thread or zombie — fall back to [comm]
            try:
                with open(f"/proc/{pid}/comm") as f:
                    command = f"[{f.read().strip()}]"
            except OSError:
                command = "[unknown]"

        processes.append({
            "user": user,
            "pid": pid,
            "ppid": ppid,
            "cpu": cpu_percent,
            "sort_cpu": sort_cpu,
            "mem": mem_percent,
            "state": state,
            "command": command,
        })

    _prev_proc_cpu = current_cpu
    _ema_display_cpu = new_ema_display
    _ema_sort_cpu = new_ema_sort

    # Primary sort by displayed CPU (rounded), tiebreak by smoothed sort_cpu
    # (stickier EMA), then PID for final stability
    processes.sort(key=lambda p: (-p["cpu"], -p["sort_cpu"], p["pid"]))
    processes = processes[:500]

    # Look up systemd units only for displayed processes (not the full /proc set).
    # Show unit badge only on the root process of each service.
    pid_to_unit: dict[int, str | None] = {}
    for p in processes:
        pid_to_unit[p["pid"]] = _get_systemd_unit(p["pid"])
    for p in processes:
        unit = pid_to_unit.get(p["pid"])
        parent_unit = pid_to_unit.get(p["ppid"])
        p["systemd_unit"] = unit if unit and unit != parent_unit else None

    # Batch-fetch ExecReload for any uncached units (single systemctl call).
    visible_units = [p["systemd_unit"] for p in processes if p["systemd_unit"]]
    if visible_units:
        _populate_reload_cache(visible_units)
    for p in processes:
        if p["systemd_unit"]:
            p["has_reload"] = _unit_has_reload.get(p["systemd_unit"], False)

    return processes, counts


@terminal_bp.route("/terminal/processes")
def get_processes():
    """Return system stats and process list for task manager."""
    cpu_stats = _get_cpu_stats()
    processes, process_counts = _get_processes()
    return jsonify({
        "cpu_percent": cpu_stats["percent"],
        "cpu": cpu_stats,
        "memory": _get_memory_info(),
        "load": _get_load_average(),
        "uptime": _get_uptime(),
        "process_counts": process_counts,
        "processes": processes,
        "current_user": getpass.getuser(),
    })


ALLOWED_SIGNALS = {
    "TERM": "-SIGTERM",
    "KILL": "-SIGKILL",
    "HUP": "-SIGHUP",
    "INT": "-SIGINT",
    "STOP": "-SIGSTOP",
    "CONT": "-SIGCONT",
    "USR1": "-SIGUSR1",
    "USR2": "-SIGUSR2",
}


@terminal_bp.route("/terminal/process/kill", methods=["POST"])
def kill_process():
    """Send a signal to a process by PID."""
    # Validate terminal token - proves request came from active terminal session
    terminal_session_id = request.headers.get("X-Terminal-Session", "")
    terminal_token = request.headers.get("X-Terminal-Token", "")
    if not pty_manager.validate_token(terminal_session_id, terminal_token):
        return jsonify({"error": "Invalid or missing terminal session"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    pid = data.get("pid")
    signal_name = data.get("signal", "TERM")
    use_sudo = data.get("use_sudo", False)

    if not pid or not isinstance(pid, int):
        return jsonify({"error": "Invalid PID"}), 400

    if signal_name not in ALLOWED_SIGNALS:
        return jsonify({"error": f"Invalid signal (allowed: {', '.join(ALLOWED_SIGNALS.keys())})"}), 400

    sig = ALLOWED_SIGNALS[signal_name]
    cmd = ["sudo", "kill", sig, str(pid)] if use_sudo else ["kill", sig, str(pid)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return jsonify({"success": True, "message": f"Sent SIG{signal_name} to PID {pid}"})
        else:
            error = result.stderr.strip() or f"Failed to signal process {pid}"
            return jsonify({"error": error}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Kill command timed out"}), 500
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# Allowed systemd service actions
ALLOWED_SERVICE_ACTIONS = {"restart", "start", "stop", "reload", "enable", "disable"}


def _is_valid_service_unit(unit: str) -> bool:
    """Validate a basic systemd service unit name."""
    return bool(unit) and unit.endswith(".service") and "/" not in unit and ".." not in unit


def _parse_systemctl_show(stdout: str) -> dict[str, str]:
    """Parse `systemctl show` KEY=VALUE output."""
    data: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip()
    return data


def _get_service_properties(unit: str, *, use_sudo: bool = False) -> dict[str, str]:
    """Fetch a small set of systemd properties for a unit."""
    cmd = [
        "systemctl",
        "show",
        unit,
        "--property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,StatusText,Description,"
        "MainPID,ExecMainStartTimestamp,ExecMainExitTimestamp,NRestarts,FragmentPath",
    ]
    if use_sudo:
        cmd.insert(0, "sudo")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {}
        return _parse_systemctl_show(result.stdout)
    except (subprocess.TimeoutExpired, OSError):
        return {}


def _format_service_short_status(props: dict[str, str]) -> str:
    """Build a compact status string for a service."""
    active = props.get("ActiveState") or "unknown"
    sub = props.get("SubState") or ""
    result = props.get("Result") or ""
    exec_status = props.get("ExecMainStatus") or ""

    parts = [active]
    if sub and sub != active:
        parts.append(sub)
    if result and result not in {"success", "none"}:
        parts.append(result)
    if exec_status and exec_status != "0":
        parts.append(f"exit {exec_status}")
    return " / ".join(parts)


# systemd ExecMainCode values (from src/basic/exit-status.h)
EXEC_MAIN_CODE_NAMES = {
    "0": None,        # CLD_EXITED with status 0 (success)
    "1": "exited",    # CLD_EXITED
    "2": "killed",    # CLD_KILLED
    "3": "dumped",    # CLD_DUMPED (core dump)
    "4": "timeout",   # timeout
    "5": "watchdog",  # watchdog
    "6": "start-limit-hit",
    "7": "condition-failed",
    "8": "exec-error",
}


def _get_service_reason_preview(props: dict[str, str]) -> str | None:
    """Extract a short human-readable reason for a unit state."""
    status_text = (props.get("StatusText") or "").strip()
    if status_text:
        return status_text

    exec_code = props.get("ExecMainCode") or ""
    exec_status = props.get("ExecMainStatus") or ""
    result = props.get("Result") or ""

    if exec_status and exec_status != "0":
        code_name = EXEC_MAIN_CODE_NAMES.get(exec_code, exec_code)
        if code_name:
            return f"{code_name} with status {exec_status}"
        return f"exit {exec_status}"
    if result and result not in {"success", "none"}:
        return f"systemd result: {result}"
    return None


def _get_service_log_excerpt(unit: str, max_lines: int = 16) -> str | None:
    """Return the tail of the service journal as a best-effort failure excerpt."""
    try:
        result = subprocess.run(
            ["sudo", "journalctl", "-u", unit, "-n", "40", "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode != 0:
            return None
        lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        return "\n".join(lines[-max_lines:])
    except (subprocess.TimeoutExpired, OSError):
        return None


def _get_service_working_directory(unit: str) -> str | None:
    """Get the WorkingDirectory for a systemd service unit."""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=WorkingDirectory"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("WorkingDirectory="):
                path = line.split("=", 1)[1].strip()
                return path if path else None
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _find_service_log_files(unit: str, max_files: int = 10) -> list[dict]:
    """Find log files in the service's working directory, sorted by modification time."""
    work_dir = _get_service_working_directory(unit)
    if not work_dir:
        return []

    try:
        # Find log files, excluding:
        # - Hidden directories (starting with .)
        # - Directories containing 'cache' (case insensitive)
        # - static, node_modules, venv directories
        # Include text-based log formats, exclude binary (etl, journal, evtx)
        # Output format: modification_time (epoch) followed by path
        cmd = [
            "sudo", "find", work_dir,
            "-maxdepth", "4",
            "(",
                "-name", ".*", "-o",
                "-iname", "*cache*", "-o",
                "-iname", "*archive*", "-o",
                "-name", "static", "-o",
                "-name", "node_modules", "-o",
                "-name", "venv",
            ")", "-prune", "-o",
            "-type", "f",
            "(",
                "-name", "*.log", "-o",
                "-name", "*.jsonl", "-o",
                "-name", "*.err", "-o",
                "-name", "*.out", "-o",
                "-name", "*.trace", "-o",
                "-name", "*.logfile", "-o",
                "-name", "*.access", "-o",
                "-iname", "*log*.txt",
            ")",
            "-printf", "%T@ %p\\n",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        log_files = []
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            mtime_str, path = parts

            # Skip rotated logs (e.g., app.log.1, app.log.gz, app.log.2024-01-01)
            if re.search(r"\.(log|jsonl|err|out|trace|logfile|access|txt)\.(gz|[0-9])", path):
                continue

            # Skip compiler output (a.out)
            if path.endswith("/a.out"):
                continue

            try:
                mtime = float(mtime_str)
                log_files.append({"path": path, "mtime": mtime})
            except ValueError:
                continue

        # Sort by modification time (most recent first)
        log_files.sort(key=lambda x: x["mtime"], reverse=True)
        return log_files[:max_files]

    except (subprocess.TimeoutExpired, OSError):
        return []


def _read_log_file_tail(path: str, max_lines: int = 20) -> str | None:
    """Read the tail of a log file."""
    try:
        result = subprocess.run(
            ["sudo", "tail", "-n", str(max_lines), path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() if result.stdout.strip() else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _get_service_failure_details(unit: str, *, include_logs: bool = False) -> dict | None:
    """Get state and best-effort failure details for a service unit."""
    props = _get_service_properties(unit)
    if not props:
        props = _get_service_properties(unit, use_sudo=True)
    if not props:
        return None

    data = {
        "unit": unit,
        "description": props.get("Description") or unit,
        "active_state": props.get("ActiveState") or "unknown",
        "sub_state": props.get("SubState") or "",
        "result": props.get("Result") or "",
        "short_status": _format_service_short_status(props),
        "reason_preview": _get_service_reason_preview(props),
    }
    if include_logs:
        data["log_excerpt"] = _get_service_log_excerpt(unit)
        # Include additional details for the debug modal
        data["exit_status"] = props.get("ExecMainStatus") or ""
        data["main_pid"] = props.get("MainPID") or ""
        data["started"] = props.get("ExecMainStartTimestamp") or ""
        data["finished"] = props.get("ExecMainExitTimestamp") or ""
        data["restart_count"] = props.get("NRestarts") or "0"
        data["fragment_path"] = props.get("FragmentPath") or ""
    return data


def _count_recent_failures(unit: str, minutes: int = 5) -> int:
    """Count how many times a service failed in the last N minutes via journal."""
    try:
        result = subprocess.run(
            [
                "sudo", "journalctl",
                "-u", unit,
                "--since", f"{minutes} minutes ago",
                "--no-pager",
                "-o", "cat",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return 0
        return result.stdout.count("Failed with result")
    except (subprocess.TimeoutExpired, OSError):
        return 0


def _get_restart_looping_services() -> list[dict]:
    """Find services stuck in restart loops (auto-restart with repeated failures)."""
    try:
        # Query services currently in activating state
        result = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--type=service",
                "--state=activating",
                "--all",
                "--plain",
                "--full",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        looping_services: list[dict] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 5)
            if parts and parts[0] == "●":
                parts = parts[1:]
            if len(parts) < 4:
                continue
            unit = parts[0]
            if not _is_valid_service_unit(unit):
                continue

            # Check if it's in auto-restart substate
            props = _get_service_properties(unit)
            if not props:
                props = _get_service_properties(unit, use_sudo=True)
            if props.get("SubState") != "auto-restart":
                continue

            # Count recent failures to distinguish from benign restarts
            failure_count = _count_recent_failures(unit, minutes=5)
            if failure_count < 3:
                continue  # Not a problematic loop

            # Build details with restart-loop specific status
            n_restarts = props.get("NRestarts") or "0"
            result_str = props.get("Result") or "unknown"
            short_status = f"restart-loop / {result_str} / {failure_count} failures in 5 min"

            details = {
                "unit": unit,
                "description": props.get("Description") or unit,
                "active_state": "restart-loop",
                "sub_state": props.get("SubState") or "",
                "result": result_str,
                "short_status": short_status,
                "reason_preview": f"{n_restarts} total restarts, failing repeatedly",
                "is_restart_loop": True,
            }
            looping_services.append(details)

        return looping_services
    except (subprocess.TimeoutExpired, OSError):
        return []


def _get_failed_services() -> list[dict]:
    """List currently failed systemd services with a compact status summary."""
    try:
        result = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--type=service",
                "--state=failed",
                "--all",
                "--plain",
                "--full",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        failed_services: list[dict] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 5)
            if parts and parts[0] == "●":
                parts = parts[1:]
            if len(parts) < 4:
                continue
            unit = parts[0]
            if not _is_valid_service_unit(unit):
                continue

            details = _get_service_failure_details(unit, include_logs=False) or {
                "unit": unit,
                "description": parts[4] if len(parts) > 4 else unit,
                "active_state": parts[2],
                "sub_state": parts[3],
                "result": "",
                "short_status": " / ".join(parts[2:4]),
                "reason_preview": None,
            }
            if len(parts) > 4 and parts[4]:
                details["description"] = parts[4]
            failed_services.append(details)

        # Also include services stuck in restart loops
        looping_services = _get_restart_looping_services()
        seen_units = {s["unit"] for s in failed_services}
        for service in looping_services:
            if service["unit"] not in seen_units:
                failed_services.append(service)

        failed_services.sort(key=lambda item: item["unit"])
        return failed_services
    except (subprocess.TimeoutExpired, OSError):
        return []


@terminal_bp.route("/terminal/services/failed")
def get_failed_services_endpoint():
    """Return failed systemd services for the task manager."""
    terminal_session_id = request.headers.get("X-Terminal-Session", "")
    terminal_token = request.headers.get("X-Terminal-Token", "")
    if not pty_manager.validate_token(terminal_session_id, terminal_token):
        return jsonify({"error": "Invalid or missing terminal session"}), 403

    _unit_has_reload.clear()  # re-probe on next process fetch (task manager reopened)
    return jsonify({"failed_services": _get_failed_services()})


@terminal_bp.route("/terminal/service/failure")
def get_service_failure_endpoint():
    """Return detailed failure information for a systemd service."""
    terminal_session_id = request.headers.get("X-Terminal-Session", "")
    terminal_token = request.headers.get("X-Terminal-Token", "")
    if not pty_manager.validate_token(terminal_session_id, terminal_token):
        return jsonify({"error": "Invalid or missing terminal session"}), 403

    unit = request.args.get("unit", "").strip()
    if not _is_valid_service_unit(unit):
        return jsonify({"error": "Invalid unit name format"}), 400

    details = _get_service_failure_details(unit, include_logs=True)
    if not details:
        return jsonify({"error": "Service details unavailable"}), 404
    return jsonify(details)


@terminal_bp.route("/terminal/service/logs")
def get_service_logs_endpoint():
    """Find and return log files for a systemd service."""
    terminal_session_id = request.headers.get("X-Terminal-Session", "")
    terminal_token = request.headers.get("X-Terminal-Token", "")
    if not pty_manager.validate_token(terminal_session_id, terminal_token):
        return jsonify({"error": "Invalid or missing terminal session"}), 403

    unit = request.args.get("unit", "").strip()
    if not _is_valid_service_unit(unit):
        return jsonify({"error": "Invalid unit name format"}), 400

    # Find log files in the service's working directory
    log_files = _find_service_log_files(unit)

    # If a specific file is requested, return its contents
    file_path = request.args.get("file", "").strip()
    if file_path:
        # Validate the path is one of the found log files (security check)
        valid_paths = {f["path"] for f in log_files}
        if file_path not in valid_paths:
            return jsonify({"error": "Invalid log file path"}), 400
        content = _read_log_file_tail(file_path, max_lines=30)
        return jsonify({"path": file_path, "content": content})

    # Return list of found log files
    work_dir = _get_service_working_directory(unit)
    return jsonify({
        "working_directory": work_dir,
        "log_files": log_files,
        "message": "No log files found" if not log_files else None,
    })


@terminal_bp.route("/terminal/service/control", methods=["POST"])
def control_service():
    """Control a systemd service (restart, start, stop, reload, enable, disable)."""
    # Validate terminal token
    terminal_session_id = request.headers.get("X-Terminal-Session", "")
    terminal_token = request.headers.get("X-Terminal-Token", "")
    if not pty_manager.validate_token(terminal_session_id, terminal_token):
        return jsonify({"error": "Invalid or missing terminal session"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    unit = data.get("unit")
    action = data.get("action", "restart")

    if not unit or not isinstance(unit, str):
        return jsonify({"error": "Invalid unit name"}), 400

    # Validate unit name format (basic sanitization)
    if not _is_valid_service_unit(unit):
        return jsonify({"error": "Invalid unit name format"}), 400

    if action not in ALLOWED_SERVICE_ACTIONS:
        return jsonify({"error": f"Invalid action (allowed: {', '.join(ALLOWED_SERVICE_ACTIONS)})"}), 400

    daemon_reload = bool(data.get("daemon_reload"))

    # All service control requires sudo
    try:
        if daemon_reload:
            reload_required = False
            try:
                need_check = subprocess.run(
                    ["systemctl", "show", unit, "--property=NeedDaemonReload"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                reload_required = (
                    need_check.returncode == 0 and "NeedDaemonReload=yes" in need_check.stdout
                )
            except (subprocess.TimeoutExpired, OSError):
                reload_required = False

            if reload_required:
                try:
                    reload_result = subprocess.run(
                        ["sudo", "-n", "systemctl", "daemon-reload"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired:
                    return jsonify({"error": "systemctl daemon-reload timed out"}), 500
                except OSError as e:
                    return jsonify({"error": str(e)}), 500

                if reload_result.returncode != 0:
                    error = reload_result.stderr.strip() or "systemctl daemon-reload failed"
                    return jsonify({
                        "error": error,
                        "service": _get_service_failure_details(unit, include_logs=True),
                    }), 400

        result = subprocess.run(
            ["sudo", "systemctl", action, unit],
            capture_output=True,
            text=True,
            timeout=30,  # Longer timeout for service operations
        )
        if result.returncode == 0:
            return jsonify({"success": True, "message": f"Service {unit}: {action} successful"})
        else:
            error = result.stderr.strip() or f"Failed to {action} {unit}"
            return jsonify({
                "error": error,
                "service": _get_service_failure_details(unit, include_logs=True),
            }), 400
    except subprocess.TimeoutExpired:
        return jsonify({"error": f"Service {action} timed out"}), 500
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# Store previous I/O stats for rate calculation
_prev_io_stats: dict[int, dict] = {}
_prev_io_time: dict[int, float] = {}


def _format_bytes(b: float) -> str:
    """Format bytes to human readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(b) < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def _get_systemd_unit(pid: int) -> str | None:
    """Quick lookup of systemd unit name from cgroup (no enabled check)."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            path = f.read().strip().split("::", 1)[-1]
            for seg in path.split("/"):
                if seg.endswith(".service"):
                    return seg
    except OSError:
        pass
    return None


def _populate_reload_cache(units: list[str]) -> None:
    """Batch-fetch ExecReload for uncached units in a single systemctl call."""
    uncached = [u for u in units if u not in _unit_has_reload]
    if not uncached:
        return
    try:
        result = subprocess.run(
            ["systemctl", "show", *uncached, "--property=Id,ExecReload"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Default all queried units to False so we never retry on failure.
        # Parse output regardless of returncode — systemctl show returns
        # valid data for known units even when some in the batch are invalid
        # (and returns RC 0 in almost all cases anyway).
        for u in uncached:
            _unit_has_reload.setdefault(u, False)
        # Units without ExecReload omit the line entirely, so its presence
        # in a block means reload is supported.
        for block in result.stdout.split("\n\n"):
            if "ExecReload=" not in block:
                continue
            for line in block.splitlines():
                if line.startswith("Id="):
                    _unit_has_reload[line.split("=", 1)[1].strip()] = True
                    break
    except (subprocess.TimeoutExpired, OSError):
        # Cache as False so we don't retry a failing subprocess every refresh
        for u in uncached:
            _unit_has_reload.setdefault(u, False)


def _get_systemd_info(pid: int) -> dict | None:
    """Get systemd unit info including enabled status (for details panel)."""
    unit = _get_systemd_unit(pid)
    if not unit:
        return None

    data = {"unit": unit, "enabled": False}
    try:
        systemd_result = subprocess.run(
            ["systemctl", "is-enabled", unit],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if systemd_result.stdout.strip() == "enabled":
            data["enabled"] = True
    except (subprocess.TimeoutExpired, OSError):
        pass

    return data


def _get_process_details(pid: int) -> dict:
    """Get detailed info for a process including ports, I/O, fds, etc."""
    import time
    global _prev_io_stats, _prev_io_time

    result = {
        "ports": [],
        "connections": [],
        "io": None,
        "fds": None,
        "cwd": None,
        "threads": None,
        "memory": None,
        "systemd": None,
        "start_time": None,
        "cpu_time": None,
    }

    # ─── Start time and cumulative CPU time from /proc/{pid}/stat ───
    stat_data = _read_proc_stat(pid)
    if stat_data:
        _state, _ppid, utime, stime, starttime_ticks, _rss = stat_data
        boot_time = _get_boot_time()
        if boot_time:
            result["start_time"] = boot_time + starttime_ticks / _CLK_TCK
        total_cpu_secs = (utime + stime) / _CLK_TCK
        if total_cpu_secs >= 3600:
            h = int(total_cpu_secs // 3600)
            m = int((total_cpu_secs % 3600) // 60)
            s = int(total_cpu_secs % 60)
            result["cpu_time"] = f"{h}:{m:02d}:{s:02d}"
        elif total_cpu_secs >= 60:
            m = int(total_cpu_secs // 60)
            s = int(total_cpu_secs % 60)
            result["cpu_time"] = f"{m}:{s:02d}"
        else:
            result["cpu_time"] = f"{total_cpu_secs:.1f}s"

    # ─── Network: Listening ports ───
    try:
        ss_result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if ss_result.returncode == 0:
            pid_str = f"pid={pid},"
            for line in ss_result.stdout.strip().split("\n")[1:]:
                if pid_str in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        local = parts[3]
                        if ":" in local:
                            addr, port = local.rsplit(":", 1)
                            result["ports"].append({
                                "local_addr": addr.strip("[]"),
                                "local_port": port,
                                "proto": "tcp",
                            })
    except (subprocess.TimeoutExpired, OSError):
        pass

    # ─── Network: Established connections ───
    try:
        ss_conn = subprocess.run(
            ["ss", "-tnp"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if ss_conn.returncode == 0:
            pid_str = f"pid={pid},"
            for line in ss_conn.stdout.strip().split("\n")[1:]:
                if pid_str in line and "ESTAB" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        remote = parts[4]
                        if ":" in remote:
                            addr, port = remote.rsplit(":", 1)
                            result["connections"].append({
                                "remote_addr": addr.strip("[]"),
                                "remote_port": port,
                            })
    except (subprocess.TimeoutExpired, OSError):
        pass

    # ─── I/O stats with rate calculation ───
    try:
        with open(f"/proc/{pid}/io") as f:
            io_data = {}
            for line in f:
                key, val = line.strip().split(": ")
                io_data[key] = int(val)

        now = time.time()
        prev = _prev_io_stats.get(pid)
        prev_time = _prev_io_time.get(pid, now)
        elapsed = now - prev_time

        # Calculate rates if we have previous data
        if prev and elapsed > 0.5:
            read_rate = (io_data.get("rchar", 0) - prev.get("rchar", 0)) / elapsed
            write_rate = (io_data.get("wchar", 0) - prev.get("wchar", 0)) / elapsed
            disk_read_rate = (io_data.get("read_bytes", 0) - prev.get("read_bytes", 0)) / elapsed
            disk_write_rate = (io_data.get("write_bytes", 0) - prev.get("write_bytes", 0)) / elapsed

            result["io"] = {
                "read_rate": _format_bytes(read_rate),
                "write_rate": _format_bytes(write_rate),
                "disk_read_rate": _format_bytes(disk_read_rate),
                "disk_write_rate": _format_bytes(disk_write_rate),
                "total_read": _format_bytes(io_data.get("rchar", 0)),
                "total_write": _format_bytes(io_data.get("wchar", 0)),
            }
        else:
            # First request - show totals only
            result["io"] = {
                "total_read": _format_bytes(io_data.get("rchar", 0)),
                "total_write": _format_bytes(io_data.get("wchar", 0)),
                "note": "Rate available on next refresh",
            }

        _prev_io_stats[pid] = io_data
        _prev_io_time[pid] = now

    except (OSError, ValueError, KeyError):
        pass

    # ─── File descriptors ───
    try:
        fd_path = f"/proc/{pid}/fd"
        fds = os.listdir(fd_path)
        fd_count = len(fds)

        # Sample a few file descriptors to show what they point to
        fd_samples = []
        for fd in fds[:5]:
            try:
                target = os.readlink(f"{fd_path}/{fd}")
                # Categorize the fd
                if target.startswith("/"):
                    fd_type = "file"
                elif target.startswith("socket:"):
                    fd_type = "socket"
                elif target.startswith("pipe:"):
                    fd_type = "pipe"
                elif target.startswith("anon_inode:"):
                    fd_type = target.split(":")[1].strip("[]")
                else:
                    fd_type = "other"
                fd_samples.append({"fd": fd, "target": target, "type": fd_type})
            except OSError:
                pass

        result["fds"] = {"count": fd_count, "samples": fd_samples}
    except OSError:
        pass

    # ─── Working directory ───
    try:
        result["cwd"] = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass

    # ─── Thread count and memory from /proc/{pid}/status ───
    try:
        with open(f"/proc/{pid}/status") as f:
            mem_info = {}
            for line in f:
                if line.startswith("Threads:"):
                    result["threads"] = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    mem_info["rss_kb"] = int(line.split()[1])
                elif line.startswith("VmSize:"):
                    mem_info["vsz_kb"] = int(line.split()[1])
                elif line.startswith("RssAnon:"):
                    mem_info["private_kb"] = int(line.split()[1])
                elif line.startswith("RssShmem:") or line.startswith("RssFile:"):
                    key = "shared_kb" if "Shmem" in line else "file_kb"
                    mem_info[key] = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    mem_info["swap_kb"] = int(line.split()[1])

            if mem_info:
                result["memory"] = {
                    "rss": _format_bytes(mem_info.get("rss_kb", 0) * 1024),
                    "vsz": _format_bytes(mem_info.get("vsz_kb", 0) * 1024),
                    "private": _format_bytes(mem_info.get("private_kb", 0) * 1024),
                    "shared": _format_bytes(mem_info.get("shared_kb", 0) * 1024),
                    "swap": _format_bytes(mem_info.get("swap_kb", 0) * 1024),
                }
    except (OSError, ValueError, IndexError):
        pass

    # ─── Systemd unit info ───
    result["systemd"] = _get_systemd_info(pid)

    return result


@terminal_bp.route("/terminal/process/<int:pid>/details")
def get_process_details_endpoint(pid: int):
    """Get detailed info for a process including ports, I/O, fds, etc."""
    # Validate PID exists
    try:
        with open(f"/proc/{pid}/comm"):
            pass
    except (OSError, FileNotFoundError):
        return jsonify({"error": "Process not found"}), 404

    details = _get_process_details(pid)
    return jsonify(details)
