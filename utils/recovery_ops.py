"""Read-only recovery comparisons and candidate decoding for both recovery sources."""

import codecs
import difflib
from pathlib import Path

from utils.file_ops import (
    bytes_to_hex_view,
    check_path_is_file,
    file_backup_path,
    file_revision,
    get_file_size,
    is_likely_binary,
    parse_hex_view,
    read_file_bytes,
)

PREVIEW_BYTES = 65536
PREVIEW_LINES = 2000
BINARY_PREVIEW_BYTES = 4096


def read_current(path: Path, max_size: int) -> bytes | None:
    ok, error, status = check_path_is_file(path)
    if not ok:
        if status == 404:
            return None
        raise ValueError(error)
    return read_limited(path, max_size)


def read_limited(path: Path, max_size: int) -> bytes:
    size = get_file_size(path)
    if size is not None and size > max_size:
        raise ValueError("File exceeds the recovery size limit; download the version instead")
    ok, content = read_file_bytes(path)
    if not ok:
        raise OSError(content)
    if len(content) > max_size:
        raise ValueError("File exceeds the recovery size limit; download the version instead")
    return content


def candidate_bytes(path: Path, data: dict, max_size: int, notebook_operations=None) -> bytes:
    if data.get("source") == "backup":
        backup_id = data.get("backup_id")
        if not isinstance(backup_id, str):
            raise ValueError("Select a saved backup")
        content = read_limited(file_backup_path(path, backup_id), max_size)
    elif data.get("source") == "draft":
        kind = data.get("kind")
        source = data.get("content")
        binary = data.get("is_binary", False)
        if kind not in {"diff", "notebook", "terminal"} or not isinstance(source, str) or type(binary) is not bool:
            raise ValueError("Invalid browser draft")
        if kind == "notebook" and path.suffix.lower() != ".ipynb":
            raise ValueError("Notebook drafts must restore to an .ipynb file")
        if binary:
            ok, content = parse_hex_view(source)
            if not ok:
                raise ValueError(content)
        else:
            content = source.encode("utf-8")
        if path.suffix.lower() == ".ipynb" and kind != "terminal" and notebook_operations is not None:
            content = notebook_operations.serialize_notebook(notebook_operations.parse_notebook(content))
    else:
        raise ValueError("Select a saved backup or a browser draft")
    if len(content) > max_size:
        raise ValueError("Recovery version exceeds the file size limit")
    return content


def preview(current: bytes | None, candidate: bytes) -> dict:
    original = current if current is not None else b""

    def text_prefix(content):
        return codecs.getincrementaldecoder("utf-8")().decode(content[:PREVIEW_BYTES], final=len(content) <= PREVIEW_BYTES)

    binary = is_likely_binary(original[:PREVIEW_BYTES]) or is_likely_binary(candidate[:PREVIEW_BYTES])
    try:
        texts = [text_prefix(content) for content in (original, candidate)] if not binary else None
    except UnicodeError:
        binary, texts = True, None
    truncated = False
    rendered = []
    for index, content in enumerate((original, candidate)):
        if binary:
            rendered.append(bytes_to_hex_view(content[:BINARY_PREVIEW_BYTES]))
            truncated |= len(content) > BINARY_PREVIEW_BYTES
        else:
            lines = texts[index].splitlines(keepends=True)
            rendered.append("".join(lines[:PREVIEW_LINES]))
            truncated |= len(content) > PREVIEW_BYTES or len(lines) > PREVIEW_LINES
    diff = []
    for line in difflib.unified_diff(rendered[0].splitlines(keepends=True), rendered[1].splitlines(keepends=True),
                                     fromfile="Current file", tofile="Selected recovery version"):
        diff.append(line)
        if not line.endswith("\n"):
            diff.append("\n\\ No newline at end of preview\n" if truncated or binary else "\n\\ No newline at end of file\n")
    return {"current": rendered[0], "candidate": rendered[1], "diff": "".join(diff),
            "is_binary": binary, "truncated": truncated, "identical": current is not None and original == candidate,
            "exists": current is not None, "revision": file_revision(current) if current is not None else None,
            "candidate_revision": file_revision(candidate), "current_size": len(original), "candidate_size": len(candidate)}
