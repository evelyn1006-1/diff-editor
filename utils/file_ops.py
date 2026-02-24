"""
File operations with sudo support for reading/writing files outside home directory.
"""

import os
import subprocess
from pathlib import Path

HOME_DIR = Path.home()


def is_writable_by_user(path: Path) -> bool:
    """Check if the current user can write to this file directly."""
    if not path.exists():
        return os.access(path.parent, os.W_OK)
    return os.access(path, os.W_OK)


def read_file_bytes(path: Path) -> tuple[bool, bytes | str]:
    """
    Read file content as bytes. Uses sudo cat if not directly readable.
    Returns (success, bytes_or_error).
    """
    path = path.resolve()

    # Try direct read first
    if os.access(path, os.R_OK):
        try:
            return True, path.read_bytes()
        except Exception as e:
            return False, str(e)

    # Fall back to sudo cat
    try:
        result = subprocess.run(
            ["sudo", "cat", str(path)],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "Read operation timed out"
    except Exception as e:
        return False, str(e)


def read_file(path: Path) -> tuple[bool, str]:
    """
    Read file content. Uses sudo cat if not directly readable.
    Returns (success, content_or_error).
    """
    success, data = read_file_bytes(path)
    if not success:
        return False, str(data)
    return True, data.decode("utf-8", errors="replace")


def write_file(path: Path, content: str) -> tuple[bool, str]:
    """
    Write content to file. Uses sudo tee if not directly writable.
    Returns (success, message).
    """
    path = path.resolve()

    # Try direct write first
    if is_writable_by_user(path):
        try:
            path.write_text(content, encoding="utf-8")
            return True, "File saved"
        except Exception as e:
            return False, str(e)

    # Fall back to sudo tee
    try:
        result = subprocess.run(
            ["sudo", "tee", str(path)],
            input=content.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "File saved (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo tee failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Write operation timed out"
    except Exception as e:
        return False, str(e)


def write_file_bytes(path: Path, content: bytes) -> tuple[bool, str]:
    """
    Write raw bytes to file. Uses sudo tee if not directly writable.
    Returns (success, message).
    """
    path = path.resolve()

    # Try direct write first
    if is_writable_by_user(path):
        try:
            path.write_bytes(content)
            return True, "File saved"
        except Exception as e:
            return False, str(e)

    # Fall back to sudo tee
    try:
        result = subprocess.run(
            ["sudo", "tee", str(path)],
            input=content,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "File saved (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo tee failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Write operation timed out"
    except Exception as e:
        return False, str(e)
