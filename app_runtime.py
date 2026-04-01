"""Shared runtime configuration for the editor + terminal app."""

import os

DEFAULT_EDITOR_BIND = "127.0.0.1:8005"
DEFAULT_TERMINAL_BIND = "127.0.0.1:8006"

# Gunicorn binds to this address, and the editor wrapper posts back to the
# matching HTTP URL unless a more specific override is provided.
EDITOR_BIND = os.environ.get("EDITOR_BIND", DEFAULT_EDITOR_BIND)
TERMINAL_BIND = os.environ.get("TERMINAL_BIND", DEFAULT_TERMINAL_BIND)
TERMINAL_SERVER_URL = os.environ.get("TERMINAL_SERVER_URL", f"http://{TERMINAL_BIND}")
