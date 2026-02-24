"""
Terminal routes and SocketIO handlers.
"""

import glob
import json
import os
import shlex
import subprocess
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request
from flask_socketio import emit, join_room, leave_room

from utils.pty_manager import PTYManager

terminal_bp = Blueprint("terminal", __name__)
pty_manager = PTYManager()

# Commands that should redirect to diff editor
EDITOR_COMMANDS = {"nano", "vim", "vi", "nvim", "emacs", "pico", "edit"}
CLOUD_COMMAND_REDIRECTS = {
    "codex": ("https://chatgpt.com/codex", "Codex Cloud"),
    "claude": ("https://claude.ai/code", "Claude Code"),
}


def init_terminal_socketio(socketio):
    """Initialize SocketIO event handlers for terminal."""

    @socketio.on("connect", namespace="/terminal")
    def handle_connect():
        session_id = request.sid
        cwd = os.path.expanduser("~")

        # Spawn terminal as root directly via sudo -s
        if pty_manager.create_session(session_id, cwd=cwd, shell="/bin/bash"):
            join_room(session_id)
            emit("connected", {"status": "ok", "cwd": cwd})
            # Start reading output
            socketio.start_background_task(read_pty_output, socketio, session_id)
        else:
            emit("error", {"message": "Failed to create terminal session"})

    @socketio.on("disconnect", namespace="/terminal")
    def handle_disconnect():
        session_id = request.sid
        pty_manager.remove_session(session_id)
        leave_room(session_id)

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


@terminal_bp.route("/terminal")
def terminal_view():
    """Render the terminal page."""
    return render_template("terminal.html")


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
