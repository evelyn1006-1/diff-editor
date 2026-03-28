"""
File operations with sudo support for reading/writing files outside home directory.
"""

import errno
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HOME_DIR = Path.home()
RECYCLE_BIN = Path("/var/tmp/RECYCLE_BIN")
RECYCLE_META_DIR = Path("/var/lib/recycle-bin")


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


def _move_to_trash(path: Path) -> tuple[bool, str]:
    """
    Move a file, directory, or symlink to the recycle bin.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    hex_id = secrets.token_hex(4)
    trash_name = f"{path.name}__{timestamp}_{hex_id}"
    trash_dest = RECYCLE_BIN / trash_name

    meta = json.dumps({
        "original_path": str(path),
        "deleted_at": timestamp,
        "trash_name": trash_name,
    })

    # Ensure directories exist
    for d in (RECYCLE_BIN, RECYCLE_META_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            subprocess.run(
                ["sudo", "-n", "mkdir", "-p", str(d)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            )
    try:
        if oct(RECYCLE_BIN.stat().st_mode)[-4:] != "1777":
            subprocess.run(
                ["sudo", "-n", "chmod", "1777", str(RECYCLE_BIN)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            )
        if oct(RECYCLE_META_DIR.stat().st_mode)[-4:] != "1777":
            subprocess.run(
                ["sudo", "-n", "chmod", "1777", str(RECYCLE_META_DIR)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            )
    except OSError:
        pass

    def _write_meta():
        try:
            meta_file = RECYCLE_META_DIR / f"{trash_name}.json"
            try:
                meta_file.write_text(meta)
            except PermissionError:
                subprocess.run(
                    ["sudo", "-n", "tee", str(meta_file)],
                    input=meta.encode(), capture_output=True, timeout=10,
                )
        except Exception:
            pass  # metadata is best-effort; the move already succeeded

    # Try direct rename (fast, same filesystem)
    try:
        path.rename(trash_dest)
        _write_meta()
        return True, "Moved to recycle bin"
    except PermissionError:
        pass
    except OSError as e:
        if e.errno == errno.EXDEV:
            try:
                shutil.move(str(path), str(trash_dest))
                _write_meta()
                return True, "Moved to recycle bin"
            except (PermissionError, shutil.Error, OSError):
                pass
        elif e.errno not in (errno.EACCES, errno.EPERM):
            return False, str(e)

    # Fall back to sudo mv
    try:
        result = subprocess.run(
            ["sudo", "-n", "mv", str(path), str(trash_dest)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            _write_meta()
            return True, "Moved to recycle bin (with sudo)"
        error = result.stderr.decode("utf-8", errors="replace")
        return False, f"Failed to move to recycle bin: {error}"
    except subprocess.TimeoutExpired:
        return False, "Delete operation timed out"
    except Exception as e:
        return False, str(e)


def delete_path(path: Path) -> tuple[bool, str]:
    """Move a file or symlink to the recycle bin."""
    return _move_to_trash(Path(os.path.abspath(path)))


def delete_directory(path: Path) -> tuple[bool, str]:
    """Move a directory (or symlink) to the recycle bin."""
    return _move_to_trash(Path(os.path.abspath(path)))


def create_symlink(source: Path, link: Path) -> tuple[bool, str]:
    """
    Create a symlink at `link` pointing to `source`. Uses sudo ln if not directly creatable.
    Returns (success, message).
    """
    source = Path(os.path.abspath(source))
    link = Path(os.path.abspath(link))

    try:
        link.symlink_to(source)
        return True, "Symlink created"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo ln -s
    try:
        result = subprocess.run(
            ["sudo", "-n", "ln", "-s", str(source), str(link)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "Symlink created (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo ln failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Symlink operation timed out"
    except Exception as e:
        return False, str(e)


def ensure_directory(path: Path) -> tuple[bool, str]:
    """
    Create a directory and any missing parents. Uses sudo mkdir -p if needed.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))

    if path.is_dir():
        return True, "Directory exists"

    try:
        path.mkdir(parents=True)
        return True, "Directory created"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo mkdir -p
    try:
        result = subprocess.run(
            ["sudo", "-n", "mkdir", "-p", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, "Directory created (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo mkdir -p failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Create directory operation timed out"
    except Exception as e:
        return False, str(e)


def copy_directory(path: Path, target: Path) -> tuple[bool, str]:
    """
    Copy a directory recursively. Uses sudo cp -a if not directly copyable.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))
    target = Path(os.path.abspath(target))

    # If path is itself a symlink, just recreate the link (matches cp -a)
    if path.is_symlink():
        try:
            os.symlink(os.readlink(path), target)
            return True, "Directory copied"
        except PermissionError:
            pass
        except OSError as e:
            return False, str(e)
    else:
        try:
            shutil.copytree(path, target, symlinks=True)
            return True, "Directory copied"
        except (PermissionError, shutil.Error):
            pass
        except OSError as e:
            return False, str(e)

    # Clean up any partial copy with sudo before falling back
    if target.exists():
        subprocess.run(
            ["sudo", "-n", "rm", "-rf", str(target)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )

    # Fall back to sudo cp -a
    try:
        result = subprocess.run(
            ["sudo", "-n", "cp", "-a", str(path), str(target)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True, "Directory copied (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo cp failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Copy operation timed out"
    except Exception as e:
        return False, str(e)


def copy_file(path: Path, target: Path) -> tuple[bool, str]:
    """
    Copy a file. Uses sudo cp if not directly copyable.
    Returns (success, message).
    """
    path = Path(os.path.abspath(path))
    target = Path(os.path.abspath(target))

    try:
        if path.is_symlink():
            os.symlink(os.readlink(path), target)
        else:
            shutil.copy2(path, target)
        return True, "File copied"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Fall back to sudo cp
    try:
        result = subprocess.run(
            ["sudo", "-n", "cp", "-a", str(path), str(target)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "File copied (with sudo)"
        else:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo cp failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Copy operation timed out"
    except Exception as e:
        return False, str(e)


def make_executable(path: Path) -> None:
    """Make a file executable. Uses sudo chmod if needed."""
    path = Path(os.path.abspath(path))
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except PermissionError:
        subprocess.run(
            ["sudo", "-n", "chmod", "+x", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
    except OSError:
        pass


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
        if e.errno != errno.EXDEV:
            return False, str(e)
        # Cross-filesystem — try shutil.move before sudo
        try:
            shutil.move(str(path), str(target))
            return True, "Moved"
        except (PermissionError, shutil.Error):
            pass
        except OSError:
            pass

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
