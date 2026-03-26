"""
File operations with sudo support for reading/writing files outside home directory.
"""

import os
import shutil
import subprocess
import tempfile
import zipfile
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
    path = Path(os.path.abspath(path))

    # Try direct read first
    if os.access(path, os.R_OK):
        try:
            return True, path.read_bytes()
        except Exception as e:
            return False, str(e)

    # Fall back to sudo cat
    try:
        result = subprocess.run(
            ["sudo", "-n", "cat", str(path)],
            stdin=subprocess.DEVNULL,
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
    path = Path(os.path.abspath(path))

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
            ["sudo", "-n", "tee", str(path)],
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
    path = Path(os.path.abspath(path))

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
            ["sudo", "-n", "tee", str(path)],
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


def create_dir(path: Path) -> tuple[bool, str]:
    """
    Create a directory. Uses sudo mkdir if not directly creatable.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))

    try:
        path.mkdir()
        return True, "Directory created"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo mkdir
    try:
        result = subprocess.run(
            ["sudo", "-n", "mkdir", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "Directory created (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo mkdir failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Create operation timed out"
    except Exception as e:
        return False, str(e)


def delete_path(path: Path) -> tuple[bool, str]:
    """
    Delete a file. Uses sudo rm if not directly deletable.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))

    try:
        path.unlink()
        return True, "File deleted"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo rm
    try:
        result = subprocess.run(
            ["sudo", "-n", "rm", "-f", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "File deleted (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo rm failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Delete operation timed out"
    except Exception as e:
        return False, str(e)


def delete_directory(path: Path) -> tuple[bool, str]:
    """
    Delete a directory recursively. Uses sudo rm -rf if not directly deletable.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))

    # If it's a symlink, just remove the link itself
    if path.is_symlink():
        return delete_path(path)

    try:
        shutil.rmtree(path)
        return True, "Directory deleted"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo rm -rf
    try:
        result = subprocess.run(
            ["sudo", "-n", "rm", "-rf", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "Directory deleted (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo rm -rf failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Delete operation timed out"
    except Exception as e:
        return False, str(e)


def rename_path(path: Path, target: Path) -> tuple[bool, str]:
    """
    Rename/move a file or directory. Uses sudo mv if not directly renameable.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))
    target = Path(os.path.abspath(target))

    try:
        path.rename(target)
        return True, "Renamed"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo mv
    try:
        result = subprocess.run(
            ["sudo", "-n", "mv", str(path), str(target)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "Renamed (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo mv failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Rename operation timed out"
    except Exception as e:
        return False, str(e)


def zip_directory(path: Path) -> tuple[bool, str]:
    """
    Zip a directory into a temporary file. Uses sudo zip if not directly readable.
    Returns (success, zip_path_or_error).
    """
    path = Path(os.path.abspath(path))

    # Try direct zip with Python's zipfile
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in sorted(path.rglob("*")):
                arcname = str(entry.relative_to(path))
                if entry.is_dir():
                    zf.mkdir(arcname)
                else:
                    zf.write(entry, arcname)
        tmp.close()
        return True, tmp_path
    except PermissionError:
        # Clean up the partial zip
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo zip
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        os.close(tmp_fd)

        result = subprocess.run(
            ["sudo", "-n", "zip", "-r", tmp_path, "."],
            cwd=str(path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, tmp_path
        else:
            os.unlink(tmp_path)
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo zip failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Zip operation timed out"
    except Exception as e:
        return False, str(e)
