#!/usr/bin/env python3
"""
Terminal editor wrapper.

Invoked as $EDITOR by git, crontab -e, visudo, etc. Instead of opening a
TUI editor (which doesn't work in this textbox-based terminal), it sends the
file contents to the Flask server, which emits an editor_modal event to the
browser. The user edits in a textarea modal and saves or cancels. This script
blocks until the browser responds, then writes the result back to the file.

Exit codes:
  0 — saved (git/cron proceeds normally)
  1 — cancelled or error (git aborts the commit, etc.)
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app_runtime import TERMINAL_SERVER_URL

def _resolve_file_arg(argv: list[str]) -> str | None:
    """Return the file argument passed by the calling program.

    Different callers invoke $EDITOR differently. For example, visudo passes
    ``-- /etc/sudoers.tmp`` and some tools may prefix the path with a cursor
    hint like ``+12``. We want the actual file path, not those control args.
    """
    args = argv[1:]
    if not args:
        return None

    # If the caller uses ``--`` to terminate options, the next argument is the
    # file path even if it begins with a dash.
    if "--" in args:
        marker = args.index("--")
        if marker + 1 < len(args):
            return args[marker + 1]

    # Otherwise, prefer the last non-cursor argument. Editor callers nearly
    # always put the target path last.
    for arg in reversed(args):
        if arg == "--" or arg.startswith("+"):
            continue
        return arg

    return args[-1]


def main() -> None:
    file_path = _resolve_file_arg(sys.argv)
    if not file_path:
        sys.exit(1)

    session_id = os.environ.get("TERMINAL_SESSION_ID", "")
    server_url = os.environ.get("TERMINAL_SERVER_URL", TERMINAL_SERVER_URL)

    if not session_id:
        sys.exit(1)

    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as exc:
        print(f"editor_wrapper: failed to read {file_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    payload = urllib.parse.urlencode({
        "session_id": session_id,
        "file": file_path,
        "content": content,
    }).encode()

    try:
        req = urllib.request.Request(
            f"{server_url}/terminal/editor_request",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        sys.exit(1)

    if not result.get("saved"):
        sys.exit(1)

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(result.get("content", ""))
    except OSError:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
