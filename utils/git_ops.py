"""
Git operations for diff editor - finding repos, getting HEAD content, etc.
"""

import subprocess
from pathlib import Path


def _relative_directory_pathspec(directory: Path, git_root: Path) -> str | None:
    """Convert a directory into a git pathspec relative to repo root."""
    try:
        relative = directory.resolve().relative_to(git_root)
    except ValueError:
        return None
    return "." if str(relative) == "." else str(relative)


def find_git_root(path: Path) -> Path | None:
    """
    Walk up from path to find the nearest .git directory.
    Returns the repo root or None if not in a git repo.
    """
    path = path.resolve()
    current = path if path.is_dir() else path.parent

    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent

    # Check root as well
    if (current / ".git").exists():
        return current

    return None


def is_tracked_by_git(path: Path, repo_root: Path) -> bool:
    """Check if a file is tracked by git."""
    try:
        relative = path.resolve().relative_to(repo_root)
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(relative)],
            cwd=repo_root,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (ValueError, subprocess.TimeoutExpired):
        return False


def get_head_content_bytes(path: Path, repo_root: Path) -> tuple[bool, bytes | str]:
    """
    Get the HEAD version of a file from git as raw bytes.
    Returns (success, bytes_or_error).
    """
    try:
        relative = path.resolve().relative_to(repo_root)
        result = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=repo_root,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            # File might be new (not in HEAD yet)
            error = result.stderr.decode("utf-8", errors="replace")
            if "does not exist" in error or "exists on disk" in error:
                return False, "File is new (not in HEAD)"
            return False, error
    except ValueError:
        return False, "File is not within repository"
    except subprocess.TimeoutExpired:
        return False, "Git operation timed out"
    except Exception as e:
        return False, str(e)


def get_head_content(path: Path, repo_root: Path) -> tuple[bool, str]:
    """
    Get the HEAD version of a file from git.
    Returns (success, content_or_error).
    """
    success, data = get_head_content_bytes(path, repo_root)
    if not success:
        return False, str(data)
    return True, data.decode("utf-8", errors="replace")


def get_tracked_files(directory: Path) -> set[str]:
    """
    Get all git-tracked files in a directory.
    Returns a set of absolute file paths.
    """
    git_root = find_git_root(directory)
    if not git_root:
        return set()

    pathspec = _relative_directory_pathspec(directory, git_root)
    if pathspec is None:
        return set()

    try:
        result = subprocess.run(
            ["git", "ls-files", "--full-name", "--", pathspec],
            cwd=git_root,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()

        tracked = set()
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if line:
                tracked.add(str(git_root / line))
        return tracked
    except (subprocess.TimeoutExpired, Exception):
        return set()


def get_directory_git_status(directory: Path) -> dict[str, str]:
    """
    Get git status for all files in a directory.
    Returns a dict mapping absolute file paths to status: 'modified', 'new', 'deleted'.
    """
    git_root = find_git_root(directory)
    if not git_root:
        return {}

    pathspec = _relative_directory_pathspec(directory, git_root)
    if pathspec is None:
        return {}

    try:
        # Get status with porcelain format for easy parsing
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uall", "--", pathspec],
            cwd=git_root,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}

        status_map = {}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if len(line) < 4:
                continue
            # Porcelain format: XY filename
            # X = staged status, Y = unstaged status
            xy = line[:2]
            filename = line[3:]

            # Handle renamed files (R format: "R  old -> new")
            if " -> " in filename:
                filename = filename.split(" -> ")[1]

            abs_path = str(git_root / filename)

            # Determine status
            if xy[0] == '?' or xy[1] == '?':
                status_map[abs_path] = 'new'
            elif xy[0] == 'D' or xy[1] == 'D':
                status_map[abs_path] = 'deleted'
            elif xy[0] == 'A':
                status_map[abs_path] = 'new'
            else:
                status_map[abs_path] = 'modified'

        return status_map
    except subprocess.TimeoutExpired:
        return {}
    except Exception:
        return {}


def get_diff(path: Path, repo_root: Path, staged: bool = False) -> tuple[bool, str]:
    """
    Get git diff for a file.
    Returns (success, diff_output_or_error).
    """
    try:
        relative = path.resolve().relative_to(repo_root)
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        cmd.extend(["--", str(relative)])

        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.decode("utf-8", errors="replace")
        else:
            return False, result.stderr.decode("utf-8", errors="replace")
    except ValueError:
        return False, "File is not within repository"
    except subprocess.TimeoutExpired:
        return False, "Git diff timed out"
    except Exception as e:
        return False, str(e)
