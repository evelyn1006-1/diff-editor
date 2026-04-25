# Diff Editor Terminal

A browser-based file manager, diff editor, and terminal for administering a machine from one place.

This project pairs a Git-aware side-by-side editor with a mobile-friendly web terminal. You can browse files, inspect diffs against `HEAD`, edit text or binary files, preview images, compile or run supported source files, and jump straight into a terminal session when you need to do something outside the editor.

It is designed for a trusted, admin-level environment rather than a multi-tenant SaaS setup. Many filesystem and service-management operations intentionally fall back to passwordless `sudo` when direct access is not enough.

## Features

- File browser rooted at a configurable starting directory
- Monaco-based side-by-side diff editor for text files
- Git-aware comparisons against `HEAD` for tracked files
- Hex editing path for binary files
- Image preview for common image formats
- Textbox mode for mobile or simpler editing
- Save, rename, move, copy, upload, download, and batch download flows
- Recycle-bin delete flow with permanent delete inside the recycle bin
- Zip inspection, extraction, and directory zipping
- Browser terminal backed by PTYs and Socket.IO
- Modal editor support for commands like `nano`, `vim`, `vi`, `nvim`, and `emacs`; use `diff-editor` for the full diff editor
- Pager interception for commands like `less` and `more`
- Built-in task manager with process details and systemd controls
- Compile helpers for `C`, `C++`, `Go`, `Java`, `Rust`, and `C#`
- Run helpers for `Python`, `JavaScript`, `Shell`, `Go`, `C`, `C++`, `Java`, `Ruby`, `Perl`, `Rust`, `C#`, `Brainfuck`, and `Magma`
- Optional AI review for file changes through Codex CLI or the OpenAI SDK

## Project Layout

```text
diff-editor-terminal/
├── app.py                     # Main Flask app and file-management API
├── terminal.py                # Terminal routes, Socket.IO handlers, task manager
├── app_runtime.py             # Shared bind/url configuration
├── wsgi.py                    # Editor entry point
├── wsgi_terminal.py           # Terminal entry point
├── gunicorn.conf.py           # Gunicorn config for the editor app
├── gunicorn_terminal.conf.py  # Gunicorn config for the terminal app
├── templates/                 # HTML templates
├── static/                    # CSS, JS, fonts, and image assets
├── utils/                     # File, git, compile, PTY, and completion helpers
└── deploy/                    # systemd units and deployment script
```

## Requirements

- Python 3.10+
- A Linux environment with PTYs and `/proc`
- `pip` for Python dependencies
- `gunicorn` for production serving
- `gevent` / `flask-socketio` for terminal WebSockets
- Optional toolchains depending on what you want to compile or run
- Optional passwordless `sudo` if you want the elevated-access workflows to work

The frontend currently pulls Monaco Editor and the Socket.IO client from jsDelivr, so local or production use expects network access to those CDNs unless you vendor those assets yourself.

## Installation

```bash
cd /home/evelyn/diff-editor-terminal

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Environment variables are loaded from `.env` via `python-dotenv`.

Common settings:

```env
FLASK_SECRET_KEY=change-me
DEFAULT_ROOT=/home/evelyn
MAX_FILE_SIZE=10485760

EDITOR_BIND=127.0.0.1:8005
TERMINAL_BIND=127.0.0.1:8006
TERMINAL_SERVER_URL=http://127.0.0.1:8006

AI_REVIEW_PROVIDER=codex_cli
OPENAI_API_KEY=
AI_REVIEW_COOLDOWN_SECONDS=10
```

Important variables:

| Variable | Purpose |
|----------|---------|
| `FLASK_SECRET_KEY` | Session and CSRF signing key. Set this explicitly in production. |
| `DEFAULT_ROOT` | Starting directory shown in the browser UI. |
| `MAX_FILE_SIZE` | Maximum file size accepted by the editor API. |
| `EDITOR_BIND` | Bind address for the editor app. Defaults to `127.0.0.1:8005`. |
| `TERMINAL_BIND` | Bind address for the terminal app. Defaults to `127.0.0.1:8006`. |
| `TERMINAL_SERVER_URL` | Base URL used when the editor launches an embedded terminal. |
| `AI_REVIEW_PROVIDER` | `codex_cli` by default, or `openai_sdk` if you want SDK-backed reviews. |
| `OPENAI_API_KEY` | Required when `AI_REVIEW_PROVIDER=openai_sdk`. |
| `AI_REVIEW_COOLDOWN_SECONDS` | Global cooldown between review starts. |
| `AI_REVIEW_TIMEOUT` | Timeout for Codex CLI reviews. |
| `NO_PACKAGE_LOOKUP` | Disables PyPI/npm lookup during terminal completion when set truthy. |

## Running Locally

Run the editor and terminal as separate processes:

```bash
cd /home/evelyn/diff-editor-terminal
source .venv/bin/activate

python app.py
```

In a second shell:

```bash
cd /home/evelyn/diff-editor-terminal
source .venv/bin/activate

python wsgi_terminal.py
```

Then open:

- `http://127.0.0.1:8005/` for the file browser
- `http://127.0.0.1:8005/diff?file=/absolute/path/to/file` for a specific file
- `http://127.0.0.1:8006/terminal` for the terminal directly

If you want a production-like local setup, use Gunicorn instead:

```bash
gunicorn --config gunicorn.conf.py wsgi:app
gunicorn --config gunicorn_terminal.conf.py wsgi_terminal:app
```

## Production Model

The repo is structured as two Flask entry points behind a reverse proxy:

- The editor app serves the file browser, diff editor, file APIs, and AI review endpoints
- The terminal app serves `/terminal`, its Socket.IO transport, task manager APIs, and terminal-side editor integration

The checked-in Gunicorn configs default to:

- editor on `127.0.0.1:8005`
- terminal on `127.0.0.1:8006`

`deploy/deploy.sh` is opinionated for the original host. It:

- installs Python dependencies when `requirements.txt` changes
- links the included `systemd` unit files into `/etc/systemd/system`
- validates and swaps in the nginx config referenced by `NGINX_SOURCE`
- reloads nginx, reloads systemd, and restarts both services

The bundled service units are:

- `deploy/diff-editor.service`
- `deploy/terminal.service`

## How Diffs Work

- For Git-tracked files, the editor compares the working tree file against the version in `HEAD`
- For untracked files, the editor compares the current buffer against the current on-disk content
- Binary files are exposed through a hex view instead of raw text
- Image files are previewed instead of edited

## Security Notes

This project assumes a trusted operator and local-machine style access.

- It can read and write arbitrary filesystem paths the service account can reach
- It can optionally use `sudo -n` for operations that need elevation
- It exposes service-management and process-management capabilities through the UI
- It is not hardened as a general multi-user remote editor

If you want to adapt it for broader access, start by putting it behind authentication, narrowing the accessible root, and reviewing every privileged code path in `app.py`, `terminal.py`, and `utils/file_ops.py`.

## License

See [LICENSE.md](LICENSE.md).
