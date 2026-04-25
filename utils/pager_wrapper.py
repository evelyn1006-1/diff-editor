#!/usr/bin/env python3
"""
Terminal pager wrapper.

Invoked as $PAGER by tools such as git, man, systemctl, and other CLIs that
pipe long output through a pager. It forwards the captured output to the
terminal server so the browser can show the existing pager modal.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app_runtime import TERMINAL_SERVER_URL


_PAGER_MAX_CHARS = 512 * 1024


def _read_stream() -> str:
    content = sys.stdin.buffer.read(_PAGER_MAX_CHARS + 1)
    text = content[:_PAGER_MAX_CHARS].decode("utf-8", errors="replace")
    if len(content) > _PAGER_MAX_CHARS:
        text += "\n\n[... output truncated at 512 KB ...]"
    return text


def _read_files(args: list[str]) -> str:
    chunks: list[str] = []
    for raw_path in args:
        if raw_path.startswith("-") or raw_path.startswith("+"):
            continue
        path = Path(os.path.expanduser(raw_path))
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        if len(args) > 1:
            chunks.append(f"==> {raw_path} <==\n")
        chunks.append(content)
        if len(args) > 1 and not content.endswith("\n"):
            chunks.append("\n")
        if sum(len(chunk) for chunk in chunks) > _PAGER_MAX_CHARS:
            break
    text = "".join(chunks)
    if len(text) > _PAGER_MAX_CHARS:
        return text[:_PAGER_MAX_CHARS] + "\n\n[... output truncated at 512 KB ...]"
    return text


def _title(args: list[str]) -> str:
    file_args = [arg for arg in args if not arg.startswith("-") and not arg.startswith("+")]
    if len(file_args) == 1:
        return file_args[0]
    if len(file_args) > 1:
        return " ".join(file_args)
    return "Pager"


def main() -> None:
    session_id = os.environ.get("TERMINAL_SESSION_ID", "")
    server_url = os.environ.get("TERMINAL_SERVER_URL", TERMINAL_SERVER_URL)
    if not session_id:
        sys.exit(1)

    args = sys.argv[1:]
    content = _read_stream() if not sys.stdin.isatty() else _read_files(args)
    if not content.strip():
        sys.exit(0)

    payload = urllib.parse.urlencode({
        "session_id": session_id,
        "title": _title(args),
        "content": content,
    }).encode()

    try:
        req = urllib.request.Request(
            f"{server_url}/terminal/pager_request",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        sys.stdout.write(content)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
