# Diff Editor Terminal

A browser-based file manager, side-by-side diff editor, and web terminal for administering a Linux machine from one place.

It combines a Git-aware Monaco editor with a mobile-friendly PTY terminal, a built-in task manager, and systemd/nginx integration — all designed for a trusted, admin-level single-user workflow. Many filesystem and service-management operations intentionally fall back to passwordless `sudo` when direct access isn't enough.

## Features

### File Browser
- Browse the filesystem from a configurable root directory
- Git status badges (modified, new, deleted, tracked) per file
- Create, rename, move, copy (with symlink option), upload, and download files and directories
- Batch operations: multi-select with shift-click, Ctrl-click, or long-press on mobile
- Drag and drop files and directories to move them
- Recycle bin: deletions move to `/var/tmp/RECYCLE_BIN` with 30-day auto-expiry (via `rotatelog`)
- Permanent delete when operating inside the recycle bin; "Empty Recycle Bin" button
- Zip inspection, extraction (to folder or current directory), and directory zipping
- Extended file info modal: permissions, owner, line counts, image metadata (dimensions, format, EXIF orientation), PDF metadata (pages, encryption, version, title, author), media metadata (codec, bitrate, frame rate, duration, channels)
- File shortcuts for quick copy/move to `/usr/local/bin`, `/etc/systemd/system`, and `/etc/nginx/sites-available`

### Diff Editor
- Monaco-based side-by-side diff editor with a translucent pink theme
- Git-aware: compares working tree against `HEAD` for tracked files; compares buffer against disk for untracked files
- Hex editing mode for binary files
- Image preview for PNG, JPEG, GIF, WebP, BMP, SVG, and more
- PDF preview with page thumbnails, zoom controls, text layer, and fit-width
- Textbox mode for mobile or simpler editing — syntax-highlighted via Monaco tokenization, with synchronized line numbers and an inline unified diff panel on wide screens
- Auto-refresh for log files (`.log`, `.jsonl`, `.err`, `.out`, `.trace`, and more) every 1.5 seconds
- Save with Ctrl+S; wrap toggle; keyboard-driven workflow

### AI Code Review
- Dual provider support: Codex CLI (streaming JSON events) or OpenAI SDK (Responses API or Chat Completions)
- Review existing changes with contextual prompts: normal review, or custom prompts with presets for Security, Design, and Explain
- Configurable reasoning effort (low, medium, high, xhigh)
- Streaming output with cancel support; persistent review history scoped per session and file
- Cross-worker coordination via `fcntl` file locks and cache files on disk
- Global cooldown to prevent rapid-fire review requests

### Compile & Run
- **Compile**: C (gcc), C++ (g++), Go, Java (javac + jar), Rust (rustc), C# (csc/mcs) — with optimization, warnings, and cross-compilation target selection
- **Run**: Python, JavaScript, Shell, Go, C, C++, Java, Ruby, Perl, Rust, C# (dotnet/mono), Brainfuck, Magma — auto-saves before running and opens an inline terminal iframe
- **Render preview**: Markdown → HTML and HTML/Jinja template preview in a sandboxed iframe with a Safe/Network toggle for external resources

### Web Terminal
- Real PTY sessions with `xterm-256color` and true color support
- Socket.IO transport via `gevent` / `flask-socketio`
- ANSI escape sequence stripping for the textbox-based display
- Tab completion: Bash programmable completion via `bash-completion`, plus built-in completions for systemctl, git, apt, ssh, pip, npm — with PyPI and npm registry package name lookups
- Command history with search popup (Ctrl+R)
- CWD tracking via hidden ANSI OSC escape sequences emitted by the shell's `PROMPT_COMMAND`

### Command Interception
The terminal intercepts commands before they reach the shell:

- **Editor commands** (`nano`, `vim`, `vi`, `nvim`, `emacs`, `pico`, `edit`): opens a browser textarea modal with full nano-style keyboard shortcuts (Ctrl+O/X/W/\\/G/K/U, Alt+U/E), find/replace with regex and whole-word support, a built-in diff viewer (DP-based LCS with context compression), undo/redo, and nano-style exit confirmation dialogs — blocks the shell until the browser saves or cancels
- **`diff-editor`**: redirects to the full Monaco diff editor for the given file path
- **Pager commands** (`less`, `more`, `man`, `git log`/`diff`/`show`, `systemctl status`, `journalctl`, `tail -f`): captures output and displays it in a scrollable modal with full less-style keyboard navigation (q, /, ?, n/N, d/u, f/b, g/G, j/k, Space, Shift+F for live follow with 1s polling)
- **Cloud commands** (`codex`, `claude`): redirects to the cloud UI
- **Task manager commands** (`top`, `htop`): opens the built-in task manager popup
- **Git editor flows**: automatically rewrites `git commit`, `git rebase -i`, `git tag -a`, etc. to use the browser editor wrapper — preserved through `sudo` via `sudo -E`

### Task Manager
- Live process list with CPU %, memory %, state, and command — sorted and filtered
- Tree view mode with ancestor inclusion when filtering
- Per-process signal sending (TERM, HUP, INT, KILL, STOP, CONT) with sudo escalation when the process owner differs from the current user, and a force-kill warning before SIGKILL
- Systemd service controls: restart, stop, reload, enable, disable — with split-button menus per process
- **Failed services panel**: auto-detects failed systemd units, shows compact status cards with restart/dismiss, and re-shows when failure state changes
- **Stopped services panel**: remembers services stopped during the session for quick restart
- **Service failure popup**: detailed failure view with state, exit code, PID, timestamps, and log excerpts; "Find more logs" scans the service's working directory; "Edit service config" opens the unit file in the diff editor
- System stats dashboard: overall CPU, per-core CPU, memory breakdown (used/buffers/cached/swap), load averages, uptime, and process counts

### Nginx & Cert Management
- Copy/move files into `/etc/nginx/sites-available` with one-click site enablement
- `nginx -t` validation on activation with automatic rollback on failure
- Missing-certificate detection: when `nginx -t` fails due to a missing Let's Encrypt certificate, the UI offers to request one via `certbot`
- HTTP-only detection: when a site is enabled without TLS, the UI warns and offers to upgrade to HTTPS with `certbot --nginx --redirect`
- DNS sanity check before cert requests: resolves the domain and compares against the server's public IPs to avoid requesting a certificate for a domain that doesn't point here yet — with retry support
- Configurable DNS reference via `PUBLIC_DNS_REFERENCE_HOST` and `PUBLIC_DNS_EXPECTED_IPS`

## Project Layout

```text
diff-editor-terminal/
├── app.py                     # Main Flask app: file browser, diff editor, file APIs,
│                               #   compile, run, AI review, nginx/cert management
├── terminal.py                # Terminal routes, Socket.IO handlers, tab completion,
│                               #   command interception, task manager, service management
├── app_runtime.py             # Shared bind/URL configuration
├── wsgi.py                    # Editor entry point (gunicorn)
├── wsgi_terminal.py           # Terminal entry point (gunicorn + Socket.IO)
├── gunicorn.conf.py           # Gunicorn config for the editor app
├── gunicorn_terminal.conf.py  # Gunicorn config for the terminal app
├── templates/
│   ├── base.html              # Base template with nav and CSRF token
│   ├── index.html             # File browser page with terminal overlay
│   ├── diff.html              # Diff editor page with Monaco, AI review panel, run overlay
│   └── terminal.html          # Terminal page with task manager, editor/pager modals
├── static/
│   ├── style.css              # Full stylesheet (cute pink theme with Comic Neue font)
│   ├── cute2.webp             # Background image
│   ├── fonts/                 # Comic Neue woff2 font files
│   └── js/
│       ├── file-browser.js    # File browser: listing, modals, compile, upload, drag/drop
│       ├── diff-editor.js     # Monaco diff editor, AI review, PDF viewer, textbox mode
│       └── terminal.js        # Terminal client, tab completion, task manager, editor modal
├── utils/
│   ├── __init__.py
│   ├── constants.py           # Cloud command rules for codex/claude CLI argument parsing
│   ├── file_ops.py            # Filesystem operations with sudo fallback
│   ├── compile_ops.py         # Compilation helpers for C, C++, Go, Java, Rust, C#
│   ├── git_ops.py             # Git operations: root detection, HEAD content, status, diff
│   ├── pty_manager.py         # PTY session management with editor/pager env override
│   ├── editor_wrapper.py      # $EDITOR replacement: sends file to browser modal, blocks
│   ├── pager_wrapper.py       # $PAGER replacement: captures stdin/files, sends to modal
│   ├── pack_autocomp.py       # PyPI and npm package name lookups for tab completion
│   └── terminal_bashrc        # Shell rc file that injects CWD-tracking ANSI markers
├── deploy/
│   ├── deploy.sh              # Deployment script: pip install, systemd units, nginx swap
│   ├── diff-editor.service    # systemd unit for the editor app
│   └── terminal.service       # systemd unit for the terminal app
├── logs/                      # Runtime logs (rotated by rotatelog)
├── requirements.txt
├── .env                       # Environment variables
├── README.md
└── LICENSE.md                 # Princess Evelyn's License (PEL-1)
```

## Requirements

- Python 3.10+
- A Linux environment with PTYs, `/proc`, and `/sys`
- `pip` for Python dependencies
- `gunicorn` with `geventwebsocket` worker for production serving
- `gevent` / `flask-socketio` for terminal WebSockets
- Passwordless `sudo` (`sudo -n`) for elevated-access workflows to work
- Optional: `gcc`, `g++`, `go`, `javac`/`jar`, `rustc`, `csc`/`mcs`, `node`, `ruby`, `perl`, `bf`, `magma`, `pfdsim` depending on which languages you want to compile or run
- Optional: `certbot` and `nginx` for the TLS certificate management features
- Optional: Codex CLI or an OpenAI API key for AI code review

The frontend loads Monaco Editor, the Socket.IO client, and PDF.js from CDNs (jsDelivr, cdnjs), so an internet connection is expected unless you vendor those assets.

## Installation

```bash
cd /home/evelyn/diff-editor-terminal

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Environment variables are loaded from `.env` via `python-dotenv`. All variables are read at startup.

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | (random) | Session and CSRF signing key. Set explicitly in production. |
| `DEFAULT_ROOT` | `/home/evelyn` | Starting directory shown in the file browser. |
| `MAX_FILE_SIZE` | `10485760` (10 MB) | Maximum file size accepted by the editor and image APIs. |
| `MAX_PDF_FILE_SIZE` | `262144000` (250 MB) | Maximum PDF size accepted by the editor and PDF API. |
| `EDITOR_BIND` | `127.0.0.1:8005` | Bind address for the editor gunicorn process. |
| `TERMINAL_BIND` | `127.0.0.1:8006` | Bind address for the terminal gunicorn process. |
| `TERMINAL_SERVER_URL` | `http://127.0.0.1:8006` | Base URL the editor wrapper and pager wrapper post back to. |
| **AI Review** | | |
| `AI_REVIEW_PROVIDER` | `codex_cli` | `codex_cli` or `openai_sdk`. |
| `AI_REVIEW_MODEL` | `gpt-5.3-codex` | Model name passed to codex or the OpenAI SDK. |
| `OPENAI_API_KEY` | (none) | Required when `AI_REVIEW_PROVIDER=openai_sdk`. |
| `OPENAI_BASE_URL` | (none) | Optional base URL override for the OpenAI SDK (uses Chat Completions path). |
| `AI_REVIEW_REASONING` | `true` | Enable reasoning/thinking for OpenAI SDK reviews. |
| `AI_REVIEW_COOLDOWN_SECONDS` | `10` | Global cooldown between AI review starts (cross-worker via file lock). |
| `AI_REVIEW_TIMEOUT` | `900` | Timeout in seconds for Codex CLI reviews. |
| `AI_REVIEW_CACHE_DIR` | `/tmp/diff-editor-ai-review-cache` | Directory for review cache files. |
| `AI_REVIEW_CACHE_TTL_SECONDS` | `86400` (24h) | How long cached reviews live before expiry. |
| `AI_REVIEW_DEBUG_FILE` | `logs/ai-review-debug.log` | Debug log for Codex CLI review events. |
| **Nginx / Cert** | | |
| `LETSENCRYPT_EMAIL` | (none) | Email passed to certbot for registration. |
| `PUBLIC_DNS_REFERENCE_HOST` | (auto) | Hostname whose IPs are used as the expected DNS target. |
| `PUBLIC_DNS_EXPECTED_IPS` | (auto) | Comma-separated IPs the domain should resolve to for cert requests. |
| **Terminal** | | |
| `NO_PACKAGE_LOOKUP` | `false` | Set truthy to disable PyPI/npm package lookups during tab completion. |

## Architecture

The project runs as **two separate gunicorn processes** behind a reverse proxy (typically nginx):

- **Editor app** (`wsgi.py` → `app.py`): Serves the file browser at `/`, the diff editor at `/diff`, all file/compile/run/nginx APIs, AI review endpoints, and render-file previews. Default: `127.0.0.1:8005` with 2 gevent workers.
- **Terminal app** (`wsgi_terminal.py` → `terminal.py`): Serves the terminal at `/terminal`, Socket.IO at `/terminal/socket.io`, task manager APIs, and the editor/pager modal integration endpoints. Default: `127.0.0.1:8006` with 1 gevent worker (single worker required for PTY session state).

The terminal app must run with exactly one worker because PTY sessions and in-memory state (editor events, sudo-nopasswd cache, CPU EMA tracking) are stored in-process and not shared across workers.

### How the terminal editor wrapper works

When you run `git commit`, `crontab -e`, `visudo`, or any other command that launches `$EDITOR`:

1. The PTY session sets `EDITOR`, `VISUAL`, `GIT_EDITOR`, `SUDO_EDITOR`, `SYSTEMD_EDITOR`, `KUBE_EDITOR`, `HGEDITOR`, and `SVN_EDITOR` to `utils/editor_wrapper.py`
2. The wrapper reads the target file, POSTs its contents to the terminal server, and blocks on a `gevent.event.Event`
3. The server emits an `editor_modal` event over Socket.IO to the browser
4. The user edits in a textarea modal (or cancels)
5. The browser POSTs the result back, the server sets the event, and the wrapper writes the content to disk and exits with the appropriate code

The same pattern applies to `$PAGER` (`less`, `more`): `utils/pager_wrapper.py` captures stdin or reads file arguments and forwards the content to the browser's pager modal.

### How diffs work

- For Git-tracked files: the editor compares the working tree file against the version in `HEAD` (fetched via `git show HEAD:<path>`)
- For untracked files: the editor compares the current buffer against the on-disk content
- Binary files are shown in a hex view (offset + hex bytes + ASCII preview)
- Image files get an image preview instead of the editor
- PDF files get a PDF.js viewer with thumbnails and text layer

### How tab completion works

Tab completion uses a layered approach:

1. Bash programmable completion is tried first (via `bash-completion`, running in a helper shell with the session's cwd) for all completion types except command names
2. If Bash returns no results, built-in fallbacks kick in: `compgen -c` for command names, filesystem globbing for paths, and language-specific completions for pip/npm/systemctl/git/apt/ssh
3. For `pip install` and `npm install`, the fallback includes live PyPI and npm registry lookups (cached in-process for PyPI)
4. Completion results are logged to `logs/completion.log` in structured JSON for debugging

## Running Locally

### Development (Flask dev server)

```bash
cd /home/evelyn/diff-editor-terminal
source .venv/bin/activate

# Terminal 1: Editor app
python app.py                    # http://127.0.0.1:8005/

# Terminal 2: Terminal app
python wsgi_terminal.py          # http://127.0.0.1:8006/terminal
```

### Production-like (Gunicorn)

```bash
gunicorn --config gunicorn.conf.py wsgi:app
gunicorn --config gunicorn_terminal.conf.py wsgi_terminal:app
```

Then open:
- `http://127.0.0.1:8005/` for the file browser
- `http://127.0.0.1:8005/diff?file=/absolute/path/to/file` for a specific file
- `http://127.0.0.1:8006/terminal` for the terminal directly

When running behind nginx, the typical setup proxies `/` and `/diff/` to port 8005 and `/terminal/` (including `/terminal/socket.io/`) to port 8006.

## Deployment

`deploy/deploy.sh` automates a full deploy. It:

1. Installs or updates Python dependencies when `requirements.txt` changes (tracked via hash)
2. Symlinks `deploy/diff-editor.service` and `deploy/terminal.service` into `/etc/systemd/system`
3. Enables both units in `multi-user.target.wants`
4. Validates the replacement nginx config (backing up the existing one first, restoring on failure)
5. Reloads nginx, reloads systemd, and restarts both services

The systemd units run as `User=evelyn` and `Group=www-data` with automatic restarts.

## Security Notes

This project is designed for a **trusted single-user, local-machine** workflow. It is not hardened as a multi-user or internet-facing editor.

- It can read and write arbitrary filesystem paths the service account can reach
- It uses `sudo -n` (passwordless sudo) for operations that need elevation, including: file reads/writes, directory creation, process signaling, service management, nginx config changes, certbot certificate requests, and systemctl operations
- It exposes service-management (start/stop/restart/reload/enable/disable) and process-signaling (including SIGKILL) through the browser UI
- CSRF protection is enforced on all mutating API endpoints via a per-session token
- The terminal session is protected by a 256-bit secret token: editor-response and service-control endpoints validate this token before acting
- Protected system paths (`/boot`, `/dev`, `/proc`, `/sys`, `/run`, `/etc/fstab`, `/etc/passwd`, `/etc/shadow`, `/etc/sudoers`, etc.) cannot be deleted or overwritten through the file browser
- Rendered HTML/file previews are served with restrictive Content-Security-Policy headers and sandboxed iframes
- File upload names are sanitized (no path separators, no null bytes, no `.` or `..`)

If adapting this for broader access, start by putting it behind authentication, narrowing the accessible root, and auditing every privileged code path in `app.py`, `terminal.py`, and `utils/file_ops.py`.

## License

See [LICENSE.md](LICENSE.md) — Princess Evelyn's License (PEL-1), a copyleft license compatible with the GNU AGPL v3+.
