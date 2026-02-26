"""
Terminal routes and SocketIO handlers.
"""

import glob
import json
import logging
import os
import shlex
import subprocess
import time
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

terminal_bp = Blueprint("terminal", __name__)
pty_manager = PTYManager()

# Commands that should redirect to diff editor
EDITOR_COMMANDS = {"nano", "vim", "vi", "nvim", "emacs", "pico", "edit"}
CLOUD_COMMAND_REDIRECTS = {
    "codex": ("https://chatgpt.com/codex", "Codex Cloud"),
    "claude": ("https://claude.ai/code", "Claude Code"),
}
# Commands that should open the task manager popup
TASK_MANAGER_COMMANDS = {"top", "htop"}


def init_terminal_socketio(socketio):
    """Initialize SocketIO event handlers for terminal."""

    @socketio.on("connect", namespace="/terminal")
    def handle_connect():
        session_id = request.sid
        cwd = os.path.expanduser("~")
        remote_addr = request.remote_addr or "-"

        # Spawn terminal as root directly via sudo -s
        if pty_manager.create_session(session_id, cwd=cwd, shell="/bin/bash"):
            join_room(session_id)
            emit("connected", {"status": "ok", "cwd": cwd})
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


def check_cloud_redirect(command: str) -> tuple[str, str] | None:
    """Return (url, label) for commands that should open cloud UIs."""
    parsed = parse_intercept_command(command)
    if not parsed:
        return None

    parts, idx = parsed
    cmd = parts[idx]
    return CLOUD_COMMAND_REDIRECTS.get(cmd)


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
    # For argument completion
    command = request.args.get("command", "")
    arg_index = int(request.args.get("arg_index", "0"))

    completions = []

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
                completions = sorted(set(completions))[:50]
        except (subprocess.TimeoutExpired, OSError):
            pass

    elif comp_type == "path":
        completions = _complete_path(prefix, dirs_only, completion_cwd)

    elif comp_type == "argument":
        completions = _complete_argument(command, prefix, arg_index, completion_cwd)

    return jsonify(completions)


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


def _get_process_counts() -> dict:
    """Get process state counts."""
    counts = {"total": 0, "running": 0, "sleeping": 0, "stopped": 0, "zombie": 0}
    try:
        result = subprocess.run(
            ["ps", "axo", "stat"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[1:]:  # Skip header
                stat = line.strip()
                if not stat:
                    continue
                counts["total"] += 1
                first_char = stat[0]
                if first_char == "R":
                    counts["running"] += 1
                elif first_char in ("S", "D", "I"):
                    counts["sleeping"] += 1
                elif first_char == "T":
                    counts["stopped"] += 1
                elif first_char == "Z":
                    counts["zombie"] += 1
    except (subprocess.TimeoutExpired, OSError):
        pass
    return counts


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


def _get_processes() -> list[dict]:
    """Get process list with PPID for tree view."""
    processes = []
    try:
        # Use custom format to get PPID
        result = subprocess.run(
            ["ps", "axo", "user,pid,ppid,%cpu,%mem,vsz,rss,tty,stat,start,time,command", "--sort=-%cpu"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            # Skip header line
            for line in lines[1:150]:  # Increased limit for tree view
                parts = line.split(None, 11)
                if len(parts) >= 12:
                    cmd = parts[11]
                    # Skip the ps command itself
                    if cmd.startswith("ps axo") or cmd.startswith("ps ax"):
                        continue
                    processes.append({
                        "user": parts[0],
                        "pid": int(parts[1]),
                        "ppid": int(parts[2]),
                        "cpu": float(parts[3]),
                        "mem": float(parts[4]),
                        "vsz": int(parts[5]),
                        "rss": int(parts[6]),
                        "tty": parts[7],
                        "stat": parts[8],
                        "start": parts[9],
                        "time": parts[10],
                        "command": cmd,
                    })
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return processes


@terminal_bp.route("/terminal/processes")
def get_processes():
    """Return system stats and process list for task manager."""
    cpu_stats = _get_cpu_stats()
    return jsonify({
        "cpu_percent": cpu_stats["percent"],
        "cpu": cpu_stats,
        "memory": _get_memory_info(),
        "load": _get_load_average(),
        "uptime": _get_uptime(),
        "process_counts": _get_process_counts(),
        "processes": _get_processes(),
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
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    pid = data.get("pid")
    signal_name = data.get("signal", "TERM")

    if not pid or not isinstance(pid, int):
        return jsonify({"error": "Invalid PID"}), 400

    if signal_name not in ALLOWED_SIGNALS:
        return jsonify({"error": f"Invalid signal (allowed: {', '.join(ALLOWED_SIGNALS.keys())})"}), 400

    sig = ALLOWED_SIGNALS[signal_name]

    try:
        result = subprocess.run(
            ["kill", sig, str(pid)],
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
    }

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
