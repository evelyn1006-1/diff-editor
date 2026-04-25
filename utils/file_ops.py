"""
Filesystem helpers and file operations with sudo support.
"""

import errno
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISDIR, S_ISREG

HOME_DIR = Path.home()
RECYCLE_BIN = Path("/var/tmp/RECYCLE_BIN")
RECYCLE_META_DIR = Path("/var/lib/recycle-bin")


def _normalize_path(path: Path | str) -> Path:
    """Return an absolute Path for path-like input."""
    return Path(os.path.abspath(path))


def is_recycle_bin_root(path: Path | str) -> bool:
    """Return True when path is the recycle-bin root."""
    return _normalize_path(path) == RECYCLE_BIN


def is_in_recycle_bin(path: Path | str) -> bool:
    """Return True when path points at the recycle bin or one of its descendants."""
    normalized = _normalize_path(path)
    return normalized == RECYCLE_BIN or RECYCLE_BIN in normalized.parents


def _is_recycle_bin_entry(path: Path | str) -> bool:
    """Return True when path is a direct child of the recycle-bin root."""
    return _normalize_path(path).parent == RECYCLE_BIN


def _recycle_meta_file(trash_name: str) -> Path:
    return RECYCLE_META_DIR / f"{trash_name}.json"


def _ensure_recycle_dirs() -> None:
    for directory in (RECYCLE_BIN, RECYCLE_META_DIR):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            subprocess.run(
                ["sudo", "-n", "mkdir", "-p", str(directory)],
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


def _write_recycle_metadata(trash_name: str, meta: dict[str, object]) -> tuple[bool, str]:
    """Write recycle-bin metadata for a trashed entry."""
    _ensure_recycle_dirs()
    meta_file = _recycle_meta_file(trash_name)
    payload = json.dumps(meta)
    try:
        meta_file.write_text(payload, encoding="utf-8")
        return True, ""
    except PermissionError:
        pass
    except Exception as e:
        return False, str(e)

    try:
        result = subprocess.run(
            ["sudo", "-n", "tee", str(meta_file)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, ""
        error = result.stderr.decode("utf-8", errors="replace")
        return False, f"sudo tee failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Metadata write timed out"
    except Exception as e:
        return False, str(e)


def _remove_recycle_metadata(trash_name: str) -> tuple[bool, str]:
    """Delete recycle-bin metadata for a trashed entry."""
    meta_file = _recycle_meta_file(trash_name)
    if not meta_file.exists():
        return True, ""

    try:
        meta_file.unlink()
        return True, ""
    except FileNotFoundError:
        return True, ""
    except PermissionError:
        pass
    except Exception as e:
        return False, str(e)

    try:
        result = subprocess.run(
            ["sudo", "-n", "rm", "-f", str(meta_file)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, ""
        error = result.stderr.decode("utf-8", errors="replace")
        return False, f"sudo rm failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Metadata delete timed out"
    except Exception as e:
        return False, str(e)


def get_recycle_metadata(path: Path | str) -> dict[str, object] | None:
    """Return metadata for a top-level recycle-bin entry, if present."""
    normalized = _normalize_path(path)
    if not _is_recycle_bin_entry(normalized):
        return None

    meta_file = _recycle_meta_file(normalized.name)
    try:
        raw = meta_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        return None

    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        return None

    return meta if isinstance(meta, dict) else None


def permanently_delete_path(path: Path | str) -> tuple[bool, str]:
    """
    Permanently remove a file, symlink, or directory.

    When deleting a top-level recycle-bin entry, remove its metadata too.
    """
    normalized = _normalize_path(path)
    was_recycle_entry = _is_recycle_bin_entry(normalized)
    trash_name = normalized.name

    try:
        if normalized.is_dir() and not normalized.is_symlink():
            shutil.rmtree(normalized)
        else:
            normalized.unlink()
    except FileNotFoundError:
        if was_recycle_entry:
            _remove_recycle_metadata(trash_name)
        return True, "Already deleted"
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)
    else:
        if was_recycle_entry:
            _remove_recycle_metadata(trash_name)
        return True, "Permanently deleted"

    rm_args = ["sudo", "-n", "rm"]
    if normalized.is_dir() and not normalized.is_symlink():
        rm_args.append("-rf")
    else:
        rm_args.append("-f")
    rm_args.append(str(normalized))

    try:
        result = subprocess.run(
            rm_args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace")
            return False, f"sudo rm failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Delete operation timed out"
    except Exception as e:
        return False, str(e)

    if was_recycle_entry:
        _remove_recycle_metadata(trash_name)
    return True, "Permanently deleted"


def is_writable_by_user(path: Path) -> bool:
    """Check if the current user can write to this file directly."""
    if not path.exists():
        return os.access(path.parent, os.W_OK)
    return os.access(path, os.W_OK)


def list_directory_entries(path: Path) -> list[dict]:
    """
    List directory entries with basic metadata.
    Falls back to sudo if the directory is not directly readable/executable.
    Returns a list of dicts with keys: name, path, is_dir, is_symlink, symlink_target.
    Raises PermissionError if both direct and sudo access fail.
    """
    path = _normalize_path(path)

    try:
        entries = []
        for entry in path.iterdir():
            try:
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": entry.is_dir(),
                    "is_symlink": entry.is_symlink(),
                    "symlink_target": str(os.readlink(entry)) if entry.is_symlink() else None,
                })
            except PermissionError:
                entries.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": False,
                    "is_symlink": False,
                    "symlink_target": None,
                    "error": "Permission denied",
                })
        return entries
    except PermissionError:
        pass

    # Fall back to sudo
    script = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        "entries = []\n"
        "for e in p.iterdir():\n"
        "    try:\n"
        "        entries.append({\n"
        '            "name": e.name,\n'
        '            "path": str(e),\n'
        '            "is_dir": e.is_dir(),\n'
        '            "is_symlink": e.is_symlink(),\n'
        '            "symlink_target": str(os.readlink(e)) if e.is_symlink() else None,\n'
        "        })\n"
        "    except Exception:\n"
        "        entries.append({\n"
        '            "name": e.name,\n'
        '            "path": str(e),\n'
        '            "is_dir": False,\n'
        '            "is_symlink": False,\n'
        '            "symlink_target": None,\n'
        '            "error": "Permission denied",\n'
        "        })\n"
        "print(json.dumps(entries))\n"
    )

    try:
        result = subprocess.run(
            ["sudo", "-n", sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        raise PermissionError(result.stderr.strip() if result.stderr else "sudo listing failed")
    except subprocess.TimeoutExpired:
        raise PermissionError("Directory listing timed out")
    except json.JSONDecodeError as e:
        raise PermissionError(f"Failed to parse directory listing: {e}")


def _sudo_test(flag: str, path: Path) -> bool:
    """Run sudo test for path checks that may cross protected directories."""
    result = subprocess.run(
        ["sudo", "-n", "test", flag, str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


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
    path = _normalize_path(path)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    hex_id = secrets.token_hex(4)
    trash_name = f"{path.name}__{timestamp}_{hex_id}"
    trash_dest = RECYCLE_BIN / trash_name

    meta = {
        "original_path": str(path),
        "deleted_at": timestamp,
        "trash_name": trash_name,
    }

    _ensure_recycle_dirs()

    def _write_meta():
        ok, _ = _write_recycle_metadata(trash_name, meta)
        if not ok:
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
    path = _normalize_path(path)
    target = _normalize_path(target)
    recycle_meta = get_recycle_metadata(path)

    def _sync_recycle_metadata() -> tuple[bool, str]:
        if recycle_meta is None:
            return True, ""

        if _is_recycle_bin_entry(target):
            updated_meta = dict(recycle_meta)
            updated_meta["trash_name"] = target.name
            ok, error = _write_recycle_metadata(target.name, updated_meta)
            if not ok:
                return False, error
        ok, error = _remove_recycle_metadata(path.name)
        if not ok:
            return False, error
        return True, ""

    def _finish_rename(msg: str) -> tuple[bool, str]:
        ok, error = _sync_recycle_metadata()
        if not ok:
            return True, f"{msg} (warning: metadata sync failed: {error})"
        return True, msg

    try:
        path.rename(target)
        return _finish_rename("Renamed")
    except PermissionError:
        pass
    except OSError as e:
        if e.errno != errno.EXDEV:
            return False, str(e)
        # Cross-filesystem — try shutil.move before sudo
        try:
            shutil.move(str(path), str(target))
            return _finish_rename("Moved")
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
            return _finish_rename("Renamed (with sudo)")
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


def human_size(n: int) -> str:
    """Format a byte count as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for unit in ["KB", "MB", "GB", "TB"]:
        f /= 1024
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
    return f"{f:.1f} TB"


def stat_path(path: Path) -> tuple[bool, dict | str]:
    """
    Stat a path without following symlinks. Uses sudo stat as fallback.
    Returns (success, stat_dict_or_error).
    """
    import grp
    import pwd
    from stat import S_ISDIR, S_ISLNK, filemode

    path = Path(os.path.abspath(path))

    def _resolve_symlink() -> str | None:
        try:
            return os.readlink(path)
        except OSError:
            pass
        try:
            r = subprocess.run(
                ["sudo", "-n", "readlink", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        return None

    def _build(mode: int, size: int, mtime: float, owner: str, group: str) -> dict:
        is_link = S_ISLNK(mode)
        return {
            "size": size,
            "size_human": human_size(size),
            "permissions": filemode(mode),
            "permissions_octal": oct(mode & 0o7777)[2:].zfill(4),
            "owner": owner,
            "group": group,
            "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            ),
            "is_dir": S_ISDIR(mode),
            "is_symlink": is_link,
            "symlink_target": _resolve_symlink() if is_link else None,
        }

    # Try direct lstat
    try:
        s = path.lstat()
        try:
            owner = pwd.getpwuid(s.st_uid).pw_name
        except KeyError:
            owner = str(s.st_uid)
        try:
            group = grp.getgrgid(s.st_gid).gr_name
        except KeyError:
            group = str(s.st_gid)
        return True, _build(s.st_mode, s.st_size, s.st_mtime, owner, group)
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Sudo fallback
    try:
        result = subprocess.run(
            ["sudo", "-n", "stat", "--printf", "%f\n%s\n%Y\n%U\n%G", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.decode("utf-8", errors="replace").split("\n")
            if len(lines) >= 5:
                mode = int(lines[0], 16)
                size = int(lines[1])
                mtime = float(lines[2])
                return True, _build(mode, size, mtime, lines[3], lines[4])
        error = result.stderr.decode("utf-8", errors="replace")
        return False, f"stat failed: {error}"
    except subprocess.TimeoutExpired:
        return False, "Stat operation timed out"
    except Exception as e:
        return False, str(e)


def read_file_head(path: Path, max_bytes: int = 8192) -> tuple[bool, bytes | str]:
    """Read up to max_bytes from the start of a file. Uses sudo if needed."""
    path = Path(os.path.abspath(path))

    if os.access(path, os.R_OK):
        try:
            with open(path, "rb") as f:
                return True, f.read(max_bytes)
        except Exception as e:
            return False, str(e)

    try:
        result = subprocess.run(
            ["sudo", "-n", "head", "-c", str(max_bytes), str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout
        return False, result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "Read operation timed out"
    except Exception as e:
        return False, str(e)


def count_lines(path: Path) -> tuple[bool, int | str]:
    """Count lines in a file by streaming chunks. Uses sudo wc -l as fallback."""
    path = Path(os.path.abspath(path))

    if os.access(path, os.R_OK):
        try:
            count = 0
            last_byte = b""
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    count += chunk.count(b"\n")
                    last_byte = chunk[-1:]
            if last_byte and last_byte != b"\n":
                count += 1
            return True, count
        except Exception as e:
            return False, str(e)

    try:
        result = subprocess.run(
            ["sudo", "-n", "wc", "-l", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            count = int(result.stdout.split()[0])
            tail = subprocess.run(
                ["sudo", "-n", "tail", "-c", "1", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            )
            if tail.returncode == 0 and tail.stdout and tail.stdout != b"\n":
                count += 1
            return True, count
        return False, result.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "Line count timed out"
    except Exception as e:
        return False, str(e)


def get_directory_info(path: Path) -> tuple[bool, dict | str]:
    """
    Get recursive directory info (total size, file count, dir count).
    Falls back to sudo du + find if direct walk fails.
    Returns (success, info_dict_or_error).
    """
    path = Path(os.path.abspath(path))

    try:
        walk_errors: list[OSError] = []
        total_size = total_files = total_dirs = 0
        for root, dirs, files in os.walk(path, onerror=walk_errors.append):
            total_dirs += len(dirs)
            total_files += len(files)
            for f in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, f))
                except OSError as e:
                    if e.errno in (errno.EACCES, errno.EPERM):
                        walk_errors.append(e)
                    pass
        if any(err.errno in (errno.EACCES, errno.EPERM) for err in walk_errors):
            raise PermissionError(str(walk_errors[0]))
        return True, {
            "size_recursive": total_size,
            "size_recursive_human": human_size(total_size),
            "file_count": total_files,
            "dir_count": total_dirs,
        }
    except PermissionError:
        pass
    except OSError as e:
        return False, str(e)

    # Sudo fallback
    info = {"size_recursive": 0, "size_recursive_human": "0 B", "file_count": 0, "dir_count": 0}

    try:
        du = subprocess.run(
            ["sudo", "-n", "du", "-sb", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        )
        if du.returncode == 0:
            info["size_recursive"] = int(du.stdout.decode().split()[0])
            info["size_recursive_human"] = human_size(info["size_recursive"])
    except (subprocess.TimeoutExpired, Exception):
        pass

    try:
        find = subprocess.run(
            ["sudo", "-n", "find", str(path), "-printf", "%y\\n"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        )
        if find.returncode == 0:
            types = find.stdout.decode("utf-8", errors="replace").strip().split("\n") if find.stdout.strip() else []
            info["file_count"] = types.count("f")
            info["dir_count"] = max(0, types.count("d") - 1)
    except (subprocess.TimeoutExpired, Exception):
        pass

    return True, info


TEXT_CONTROL_WHITESPACE_BYTES = {7, 8, 9, 10, 11, 12, 13, 27}

IMAGE_EXIF_ORIENTATION_LABELS = {
    1: "Normal",
    2: "Mirrored horizontally",
    3: "Rotated 180 deg",
    4: "Mirrored vertically",
    5: "Mirrored horizontally, rotated 90 deg CW",
    6: "Rotated 90 deg CW",
    7: "Mirrored horizontally, rotated 90 deg CCW",
    8: "Rotated 90 deg CCW",
}

EDITOR_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".html": "html",
    ".css": "css", ".scss": "scss", ".json": "json", ".md": "markdown",
    ".yaml": "yaml", ".yml": "yaml", ".xml": "xml", ".sql": "sql",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".rs": "rust", ".go": "go", ".java": "java", ".c": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".h": "c", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".rb": "ruby", ".pl": "perl", ".pm": "perl", ".t": "perl",
    ".cs": "csharp", ".csx": "csharp",
    ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".nginx": "nginx", ".conf": "ini", ".ini": "ini",
    ".toml": "toml", ".env": "dotenv", ".txt": "plaintext",
    ".bf": "brainfuck", ".mag": "magma",
}

SHEBANG_LANGUAGE_MAP = {
    "python": "python", "python3": "python", "python2": "python",
    "bash": "shell", "sh": "shell", "zsh": "shell", "fish": "shell",
    "node": "javascript", "nodejs": "javascript",
    "ruby": "ruby", "perl": "perl", "perl5": "perl", "php": "php",
    "lua": "lua", "awk": "shell", "sed": "shell",
    "bf": "brainfuck", "magma": "magma",
}


def check_path_is_file(
    path: Path,
    *,
    allow_symlink: bool = False,
) -> tuple[bool, str | None, int | None]:
    """
    Check if a path exists and is a regular file.
    When allow_symlink is True, any symlink is also accepted.
    Distinguishes PermissionError (403) from other OS errors (404).
    Falls back to sudo test when direct access is blocked.
    Returns (ok, error_message, http_status).
    """
    path = _normalize_path(path)

    def _sudo_status() -> tuple[bool, str | None, int | None]:
        try:
            if _sudo_test("-f", path):
                return True, None, None
            if allow_symlink and _sudo_test("-L", path):
                return True, None, None
            if _sudo_test("-e", path):
                return False, "Not a file", 400
            return False, "File not found", 404
        except Exception:
            return False, "Permission denied", 403

    try:
        source_stat = path.lstat()
    except PermissionError:
        return _sudo_status()
    except FileNotFoundError:
        return False, "File not found", 404
    except (OSError, RuntimeError):
        return _sudo_status()

    if allow_symlink and path.is_symlink():
        return True, None, None

    if S_ISREG(source_stat.st_mode):
        return True, None, None

    try:
        target_stat = path.resolve(strict=False).lstat()
    except PermissionError:
        return _sudo_status()
    except FileNotFoundError:
        return _sudo_status()
    except (OSError, RuntimeError):
        return _sudo_status()

    if S_ISREG(target_stat.st_mode):
        return True, None, None
    return False, "Not a file", 400


def check_path_is_directory(path: Path) -> tuple[bool, str | None, int | None]:
    """
    Check if a path exists and resolves to a directory.
    Falls back to sudo test when direct access is blocked.
    Returns (ok, error_message, http_status).
    """
    path = _normalize_path(path)

    def _sudo_status() -> tuple[bool, str | None, int | None]:
        try:
            if _sudo_test("-d", path):
                return True, None, None
            if _sudo_test("-e", path) or _sudo_test("-L", path):
                return False, "Not a directory", 400
            return False, "Path does not exist", 404
        except Exception:
            return False, "Permission denied", 403

    try:
        source_stat = path.lstat()
    except PermissionError:
        return _sudo_status()
    except FileNotFoundError:
        return False, "Path does not exist", 404
    except (OSError, RuntimeError):
        return _sudo_status()

    if S_ISDIR(source_stat.st_mode):
        return True, None, None

    try:
        target_stat = path.resolve(strict=False).lstat()
    except PermissionError:
        return _sudo_status()
    except FileNotFoundError:
        return _sudo_status()
    except (OSError, RuntimeError):
        return _sudo_status()

    if S_ISDIR(target_stat.st_mode):
        return True, None, None
    return False, "Not a directory", 400


def check_path_exists(path: Path) -> tuple[bool, str | None, int | None]:
    """
    Check if a path exists (as any type, including broken symlinks).
    Distinguishes PermissionError (403) from other OS errors (404).
    Falls back to sudo test -e when direct access is blocked.
    Returns (ok, error_message, http_status).
    """
    path = _normalize_path(path)

    try:
        path.lstat()
    except PermissionError:
        try:
            if _sudo_test("-e", path) or _sudo_test("-L", path):
                return True, None, None
            return False, "Not found", 404
        except Exception:
            return False, "Permission denied", 403
    except OSError:
        return False, "Not found", 404
    return True, None, None


def get_file_size(path: Path) -> int | None:
    """Get file size in bytes. Falls back to sudo stat if direct access fails."""
    try:
        return path.stat().st_size
    except PermissionError:
        try:
            result = subprocess.run(
                ["sudo", "-n", "stat", "-c", "%s", str(path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return int(result.stdout.strip())
        except Exception:
            pass
    return None


def resolve_request_path(raw_path: str, field_name: str = "path") -> tuple[Path | None, str | None]:
    """Resolve a user-provided path while handling invalid inputs safely."""
    if raw_path is None:
        return None, f"No {field_name} specified"

    raw_path = str(raw_path)
    if not raw_path:
        return None, f"No {field_name} specified"
    if "\x00" in raw_path:
        return None, f"Invalid {field_name}"

    try:
        expanded = os.path.expandvars(os.path.expanduser(raw_path))
        return Path(os.path.abspath(expanded)), None
    except (OSError, RuntimeError, ValueError):
        return None, f"Invalid {field_name}"


def is_likely_binary(data: bytes) -> bool:
    """Best-effort binary detection for editor and metadata rendering."""
    if not data:
        return False

    if b"\x00" in data:
        return True

    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass

    control_count = sum(1 for b in data if b < 32 and b not in TEXT_CONTROL_WHITESPACE_BYTES)
    return (control_count / len(data)) > 0.30


def bytes_to_hex_view(data: bytes, bytes_per_line: int = 16) -> str:
    """Render bytes as a readable hex dump grouped by bytes."""
    if not data:
        return ""

    lines: list[str] = []
    hex_width = (bytes_per_line * 3) - 1

    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset:offset + bytes_per_line]
        hex_bytes = " ".join(f"{b:02x}" for b in chunk)
        ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_bytes.ljust(hex_width)}  |{ascii_preview}|")

    return "\n".join(lines)


def parse_hex_view(content: str) -> tuple[bool, bytes | str]:
    """
    Parse hex dump text back into bytes.
    Accepts lines like:
    00000000  aa bb cc dd ...  |....|
    and also plain whitespace-separated byte pairs.
    """
    if not content.strip():
        return True, b""

    parsed = bytearray()
    hex_chars = set("0123456789abcdefABCDEF")

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if "|" in line:
            line = line.split("|", 1)[0].rstrip()

        parts = line.split()
        if not parts:
            continue

        if len(parts[0]) == 8 and all(ch in hex_chars for ch in parts[0]):
            parts = parts[1:]

        for part in parts:
            if len(part) != 2 or any(ch not in hex_chars for ch in part):
                return False, f"Invalid hex byte '{part}' on line {line_no}"
            parsed.append(int(part, 16))

    return True, bytes(parsed)


def detect_image_mime(path: Path, data: bytes) -> str | None:
    """Detect common image types from bytes with extension fallback."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return "image/tiff"

    head = data[:2048].decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").lower()
    if head.startswith("<svg") or ("<svg" in head and head.startswith("<?xml")):
        return "image/svg+xml"
    if path.suffix.lower() == ".svg":
        return "image/svg+xml"

    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed

    return None


def detect_language_for_path(
    path: Path,
    *,
    content: str | None = None,
    is_binary: bool = False,
) -> str:
    """Best-effort language detection shared by editor and compile helpers."""
    language = EDITOR_LANGUAGE_BY_SUFFIX.get(path.suffix.lower())

    if not is_binary and not language and content:
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("#!"):
            parts = first_line[2:].strip().split()
            if parts:
                interpreter = parts[-1] if parts[0].endswith("env") and len(parts) > 1 else parts[0]
                interpreter = interpreter.split("/")[-1]
                language = SHEBANG_LANGUAGE_MAP.get(interpreter)

    return "plaintext" if is_binary else (language or "plaintext")


def parse_optional_float(value: object) -> float | None:
    """Parse a numeric value, returning None for empty or invalid inputs."""
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_int(value: object) -> int | None:
    """Parse an integer-like value, returning None for empty or invalid inputs."""
    parsed = parse_optional_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_ffprobe_rate(value: object) -> float | None:
    """Parse ffprobe rate values like 30000/1001 or 24."""
    if value in (None, "", "N/A", "0/0"):
        return None
    if isinstance(value, str) and "/" in value:
        num, den = value.split("/", 1)
        numerator = parse_optional_float(num)
        denominator = parse_optional_float(den)
        if numerator is None or denominator in (None, 0):
            return None
        return numerator / denominator
    return parse_optional_float(value)


def parse_pdf_version(data: bytes | None) -> str | None:
    """Extract a PDF version like 1.7 from the file header."""
    if not data:
        return None
    match = re.search(rb"%PDF-(\d+(?:\.\d+)?)", data[:32])
    if not match:
        return None
    return match.group(1).decode("ascii", errors="ignore")


def normalize_metadata_text(value: object) -> str | None:
    """Return a JSON-safe metadata string, dropping empty values."""
    if value in (None, ""):
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = text.strip()
    return text or None


def get_image_orientation_label(img) -> str | None:
    """Return a human-readable EXIF orientation label when available."""
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None
    return IMAGE_EXIF_ORIENTATION_LABELS.get(exif.get(274))


def build_image_info(img) -> dict:
    """Collect compact, high-value metadata from a Pillow image object."""
    try:
        bands = tuple(img.getbands() or ())
    except Exception:
        bands = ()

    return {
        "width": img.width,
        "height": img.height,
        "mode": img.mode,
        "format": img.format,
        "has_alpha": ("A" in bands) or ("transparency" in getattr(img, "info", {})),
        "orientation": get_image_orientation_label(img),
        "frame_count": max(1, int(getattr(img, "n_frames", 1) or 1)),
    }


def build_pdf_info(reader, head: bytes | None) -> dict:
    """Collect compact PDF metadata from a pypdf reader."""
    pdf_info: dict = {
        "encrypted": bool(reader.is_encrypted),
        "version": parse_pdf_version(head),
        "title": None,
        "author": None,
        "page_width_pt": None,
        "page_height_pt": None,
    }

    try:
        metadata = reader.metadata or {}
        if metadata:
            title = getattr(metadata, "title", None)
            author = getattr(metadata, "author", None)
            if title is None and hasattr(metadata, "get"):
                title = metadata.get("/Title")
            if author is None and hasattr(metadata, "get"):
                author = metadata.get("/Author")
            pdf_info["title"] = normalize_metadata_text(title)
            pdf_info["author"] = normalize_metadata_text(author)
    except Exception:
        pass

    if pdf_info["encrypted"]:
        return pdf_info

    try:
        if reader.pages:
            first_page = reader.pages[0]
            pdf_info["page_width_pt"] = parse_optional_float(first_page.mediabox.width)
            pdf_info["page_height_pt"] = parse_optional_float(first_page.mediabox.height)
    except Exception:
        pass

    return pdf_info


def build_media_info(ffprobe_data: dict) -> dict:
    """Collect compact media metadata from ffprobe output."""
    streams = ffprobe_data.get("streams", [])
    format_info = ffprobe_data.get("format", {})
    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and (stream.get("disposition") or {}).get("attached_pic") not in (1, "1", True)
    ]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]

    video_stream = video_streams[0] if video_streams else None
    audio_stream = audio_streams[0] if audio_streams else None

    bit_rate = (
        parse_optional_int(format_info.get("bit_rate"))
        or parse_optional_int(video_stream.get("bit_rate") if video_stream else None)
        or parse_optional_int(audio_stream.get("bit_rate") if audio_stream else None)
    )

    return {
        "is_audio_only": video_stream is None and audio_stream is not None,
        "width": parse_optional_int(video_stream.get("width") if video_stream else None),
        "height": parse_optional_int(video_stream.get("height") if video_stream else None),
        "duration": parse_optional_float(format_info.get("duration")),
        "video_codec": video_stream.get("codec_name") if video_stream else None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "container": format_info.get("format_long_name") or format_info.get("format_name"),
        "bit_rate": bit_rate,
        "frame_rate": parse_ffprobe_rate(
            video_stream.get("avg_frame_rate") if video_stream else None
        ) or parse_ffprobe_rate(video_stream.get("r_frame_rate") if video_stream else None),
        "sample_rate": parse_optional_int(audio_stream.get("sample_rate") if audio_stream else None),
        "channels": parse_optional_int(audio_stream.get("channels") if audio_stream else None),
        "channel_layout": audio_stream.get("channel_layout") if audio_stream else None,
        "audio_tracks": len(audio_streams),
        "subtitle_tracks": len(subtitle_streams),
    }


def cleanup_temp_path(path: Path | str | None, *, recursive: bool = False) -> None:
    """Best-effort cleanup for temporary files or directories, with sudo fallback."""
    if not path:
        return

    temp_path = Path(path)

    try:
        if recursive:
            shutil.rmtree(temp_path)
        else:
            temp_path.unlink()
        return
    except FileNotFoundError:
        return
    except OSError:
        pass

    try:
        subprocess.run(
            ["sudo", "-n", "rm", "-rf", str(temp_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:
        pass


def get_extended_file_info(path: Path) -> dict[str, object]:
    """Collect best-effort extended metadata for a file or directory."""
    path = Path(os.path.abspath(path))
    result: dict[str, object] = {
        "is_binary": None,
        "line_count": None,
        "image_info": None,
        "pdf_pages": None,
        "pdf_info": None,
        "media_info": None,
        "video_info": None,
        "size_recursive": None,
        "size_recursive_human": None,
        "file_count": None,
        "dir_count": None,
    }

    if path.is_dir():
        success, info = get_directory_info(path)
        if success:
            result.update(info)
        else:
            result["error"] = info
        return result

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    success, head = read_file_head(path)
    if success and isinstance(head, bytes):
        result["is_binary"] = is_likely_binary(head)

    if result["is_binary"] is False:
        success, n = count_lines(path)
        if success:
            result["line_count"] = n

    if mime_type.startswith("image/"):
        try:
            from PIL import Image

            if os.access(path, os.R_OK):
                with Image.open(path) as img:
                    result["image_info"] = build_image_info(img)
            else:
                ok, data = read_file_bytes(path)
                if ok:
                    with Image.open(io.BytesIO(data)) as img:
                        result["image_info"] = build_image_info(img)
        except Exception:
            pass
    elif mime_type == "application/pdf":
        try:
            import pypdf

            if os.access(path, os.R_OK):
                with open(path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    result["pdf_info"] = build_pdf_info(
                        reader,
                        head if isinstance(head, bytes) else None,
                    )
                    if not reader.is_encrypted:
                        result["pdf_pages"] = len(reader.pages)
            else:
                ok, data = read_file_bytes(path)
                if ok:
                    reader = pypdf.PdfReader(io.BytesIO(data))
                    result["pdf_info"] = build_pdf_info(
                        reader,
                        head if isinstance(head, bytes) else data[:32],
                    )
                    if not reader.is_encrypted:
                        result["pdf_pages"] = len(reader.pages)
        except Exception:
            pass

    if mime_type.startswith("video/") or mime_type.startswith("audio/"):
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_format", str(path)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            )
            if proc.returncode == 0:
                media_info = build_media_info(json.loads(proc.stdout))
                result["media_info"] = media_info
                result["video_info"] = media_info
        except Exception:
            pass

    return result


def get_zip_info(path: Path) -> tuple[bool, dict[str, int] | str]:
    """Return basic zip metadata for an existing zip file."""
    path = Path(os.path.abspath(path))
    try:
        size = path.stat().st_size
        with zipfile.ZipFile(path, "r") as zf:
            file_count = sum(1 for info in zf.infolist() if not info.is_dir())
        return True, {"size": size, "file_count": file_count}
    except (OSError, zipfile.BadZipFile):
        return False, "Cannot read zip file"


def _dedupe_path(path: Path) -> Path:
    """Find a unique path by appending (2), (3), etc."""
    if not path.exists() and not path.is_symlink():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    n = 2
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


def extract_zip_archive(
    path: Path,
    *,
    mode: str = "directory",
) -> tuple[bool, dict[str, object] | str, int]:
    """
    Extract a zip file with the app's current safety and sudo fallback behavior.
    Returns (success, payload_or_error, http_status).
    """
    path = Path(os.path.abspath(path))
    if mode not in {"directory", "here"}:
        return False, "Invalid extraction mode", 400

    dest = _dedupe_path(path.parent / path.stem) if mode == "directory" else path.parent

    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            file_count = sum(1 for info in infos if not info.is_dir())
            dest_resolved = str(dest.resolve()).rstrip("/") + "/"

            if mode == "directory":
                for name in zf.namelist():
                    resolved = str((dest / name).resolve())
                    if resolved != dest_resolved.rstrip("/") and not resolved.startswith(dest_resolved):
                        return False, f"Zip contains unsafe path: {name}", 400
                dest.mkdir()
                zf.extractall(dest)
            else:
                rename_map: dict[str, str] = {}
                for info in infos:
                    parts = info.filename.rstrip("/").split("/")
                    top = parts[0]
                    if top not in rename_map:
                        rename_map[top] = _dedupe_path(dest / top).name

                for name in zf.namelist():
                    parts = name.rstrip("/").split("/")
                    parts[0] = rename_map.get(parts[0], parts[0])
                    mapped = "/".join(parts)
                    resolved = str(Path(os.path.abspath(dest / mapped)))
                    if resolved != dest_resolved.rstrip("/") and not resolved.startswith(dest_resolved):
                        return False, f"Zip contains unsafe path: {name}", 400

                for info in infos:
                    parts = info.filename.rstrip("/").split("/")
                    parts[0] = rename_map.get(parts[0], parts[0])
                    target = dest / "/".join(parts)

                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)

        return True, {"count": file_count, "dest": str(dest)}, 200
    except PermissionError:
        pass
    except (OSError, zipfile.BadZipFile) as e:
        return False, str(e), 400

    try:
        if mode == "directory":
            subprocess.run(
                ["sudo", "-n", "mkdir", "-p", str(dest)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
            )
        result = subprocess.run(
            ["sudo", "-n", "unzip", "-n", str(path), "-d", str(dest)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        )
        if result.returncode in (0, 1):
            warning = None
            if result.returncode == 1:
                stdout = result.stdout.decode("utf-8", errors="replace")
                skipped = [
                    line.split("exists")[0].strip().split()[-1]
                    for line in stdout.splitlines()
                    if "already exists" in line.lower()
                ]
                if skipped:
                    warning = f"{len(skipped)} file(s) skipped (already exist)"
            return True, {"dest": str(dest), "warning": warning}, 200

        error_msg = result.stderr.decode("utf-8", errors="replace")
        return False, f"unzip failed: {error_msg}", 500
    except subprocess.TimeoutExpired:
        return False, "Extract operation timed out", 500
    except Exception as e:
        return False, str(e), 500


def zip_paths(paths: list[Path]) -> tuple[bool, str]:
    """Stage multiple paths into a temporary directory and zip the result."""
    def _allocate_archive_name(source: Path, seen_names: dict[str, int]) -> str:
        base = source.name or "item"
        if base not in seen_names:
            seen_names[base] = 1
            return base

        seen_names[base] += 1
        n = seen_names[base]
        return f"{source.stem} ({n}){source.suffix}" if not source.is_dir() else f"{base} ({n})"

    staging_root: Path | None = None
    try:
        staging_root = Path(tempfile.mkdtemp(prefix="diff-editor-batch-"))
        seen_names: dict[str, int] = {}

        for source in paths:
            target = staging_root / _allocate_archive_name(source, seen_names)
            if source.is_dir():
                success, message = copy_directory(source, target)
            else:
                success, message = copy_file(source, target)

            if not success:
                return False, f"Failed to add {source.name} to archive: {message}"

        success, result = zip_directory(staging_root)
        if not success:
            return False, str(result)
        return True, str(result)
    except OSError as e:
        return False, str(e)
    finally:
        cleanup_temp_path(staging_root, recursive=True)
