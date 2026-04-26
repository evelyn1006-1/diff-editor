"""
PTY session management for web terminal.
"""

import os
import pty
import secrets
import select
import signal
import struct
import time
import fcntl
import termios
from pathlib import Path
from threading import Lock
from typing import Optional

from app_runtime import TERMINAL_SERVER_URL

_WRAPPER_PATH = str(Path(__file__).resolve().parent / "editor_wrapper.py")
_PAGER_WRAPPER_PATH = str(Path(__file__).resolve().parent / "pager_wrapper.py")
_BASH_RCFILE_PATH = str(Path(__file__).resolve().parent / "terminal_bashrc")
_EDITOR_CMD = _WRAPPER_PATH
_PAGER_CMD = _PAGER_WRAPPER_PATH
_EDITOR_ENV_VARS = (
    "EDITOR",
    "VISUAL",
    "GIT_EDITOR",
    "GIT_SEQUENCE_EDITOR",
    "SUDO_EDITOR",
    "SYSTEMD_EDITOR",
    "KUBE_EDITOR",
    "HGEDITOR",
    "SVN_EDITOR",
)
_PAGER_ENV_VARS = (
    "PAGER",
    "GIT_PAGER",
    "MANPAGER",
    "SYSTEMD_PAGER",
)


class PTYSession:
    """Manages a single PTY session."""

    def __init__(
        self,
        shell: str = "/bin/bash",
        shell_args: list[str] = None,
        cwd: str = None,
        session_id: str = "",
        effective_cwd: str = None,
        is_root: bool = False,
    ):
        self.shell = shell
        self.shell_args = shell_args or []
        self.cwd = cwd or os.path.expanduser("~")
        self.effective_cwd = effective_cwd or self.cwd
        self.is_root = is_root
        self.session_id = session_id
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.alive = False
        self.token = secrets.token_hex(32)  # 256-bit secret for request validation
        self.cwd_marker_token = secrets.token_hex(16)

    def spawn(self) -> bool:
        """Spawn a new PTY process."""
        try:
            pid, master_fd = pty.fork()

            if pid == 0:
                # Child process
                os.chdir(self.cwd)
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["COLORTERM"] = "truecolor"
                # Use classic Python REPL to avoid rich-line redraw artifacts in this minimal terminal UI.
                env["PYTHON_BASIC_REPL"] = "1"
                # Route editor invocations (git commit, crontab -e, etc.) to the browser modal.
                env["TERMINAL_SESSION_ID"] = self.session_id
                env["TERMINAL_SERVER_URL"] = TERMINAL_SERVER_URL
                env["DET_TERMINAL_CWD_TOKEN"] = self.cwd_marker_token
                for key in _EDITOR_ENV_VARS:
                    env[key] = _EDITOR_CMD
                for key in _PAGER_ENV_VARS:
                    env[key] = _PAGER_CMD
                shell_args = list(self.shell_args)
                if os.path.basename(self.shell) == "bash" and not shell_args:
                    shell_args = ["--rcfile", _BASH_RCFILE_PATH]
                argv = [self.shell] + shell_args
                os.execvpe(self.shell, argv, env)
            else:
                # Parent process
                self.pid = pid
                self.master_fd = master_fd
                self.alive = True

                # Set non-blocking mode
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                return True
        except Exception as e:
            print(f"PTY spawn error: {e}")
            return False

    def read(self, timeout: float = 0.1) -> Optional[str]:
        """Read available output from PTY."""
        fd = self.master_fd
        if not self.alive or fd is None:
            return None

        try:
            ready, _, _ = select.select([fd], [], [], timeout)
            if ready:
                data = os.read(fd, 4096)
                if data:
                    return data.decode("utf-8", errors="replace")
                else:
                    self.alive = False
                    return None
        except (OSError, IOError, TypeError):
            self.alive = False
            return None

        return ""

    def write(self, data: str) -> bool:
        """Write input to PTY."""
        fd = self.master_fd
        if not self.alive or fd is None:
            return False

        try:
            os.write(fd, data.encode("utf-8"))
            return True
        except (OSError, IOError):
            self.alive = False
            return False

    def resize(self, rows: int, cols: int) -> bool:
        """Resize PTY window."""
        fd = self.master_fd
        if not self.alive or fd is None:
            return False

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
            return True
        except (OSError, IOError):
            return False

    def terminate(self):
        """Terminate the PTY session."""
        # Clear shared state first so concurrent readers/writers stop quickly.
        pid = self.pid
        fd = self.master_fd
        self.alive = False
        self.pid = None
        self.master_fd = None

        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                pass

            # Reap promptly to avoid zombies. Escalate if process does not exit.
            deadline = time.monotonic() + 0.3
            reaped = False
            while time.monotonic() < deadline:
                try:
                    waited_pid, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    reaped = True
                    break
                if waited_pid == pid:
                    reaped = True
                    break
                time.sleep(0.05)

            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class PTYManager:
    """Manages multiple PTY sessions."""

    def __init__(self):
        self.sessions: dict[str, PTYSession] = {}
        self.lock = Lock()

    def create_session(
        self,
        session_id: str,
        shell: str = "/bin/bash",
        shell_args: list[str] = None,
        cwd: str = None,
        effective_cwd: str = None,
        is_root: bool = False,
    ) -> bool:
        """Create a new PTY session."""
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id].terminate()

            session = PTYSession(
                shell=shell,
                shell_args=shell_args,
                cwd=cwd,
                session_id=session_id,
                effective_cwd=effective_cwd,
                is_root=is_root,
            )
            if session.spawn():
                self.sessions[session_id] = session
                return True
            return False

    def get_session(self, session_id: str) -> Optional[PTYSession]:
        """Get an existing PTY session."""
        with self.lock:
            return self.sessions.get(session_id)

    def validate_token(self, session_id: str, token: str) -> bool:
        """Validate that a token matches the session's secret token and session is alive."""
        with self.lock:
            session = self.sessions.get(session_id)
            return session is not None and session.token == token and session.alive

    def remove_session(self, session_id: str):
        """Remove and terminate a PTY session."""
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id].terminate()
                del self.sessions[session_id]

    def cleanup_dead_sessions(self):
        """Remove all dead sessions."""
        with self.lock:
            dead = [sid for sid, s in self.sessions.items() if not s.alive]
            for sid in dead:
                self.sessions[sid].terminate()
                del self.sessions[sid]
