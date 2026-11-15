"""Explicit saves preserve revisions and recoverable copies before overwrites."""

import os
import subprocess
from types import SimpleNamespace

import pytest

import app as editor
from utils import file_ops


@pytest.fixture
def editable(tmp_path):
    path = tmp_path / "example.txt"
    path.write_text("original")
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    with application.test_client() as client:
        with client.session_transaction() as session:
            session["csrf_token"] = "save-test"
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "save-test"
        yield path, client


def save(client, path, content, revision=None):
    return client.post("/api/file", json={"path": str(path), "content": content, "revision": revision})


def current(client, path):
    response = client.get("/api/file", query_string={"path": str(path)})
    assert response.status_code == 200
    return response.get_json()


def backup_contents(path):
    return [file_ops.file_backup_path(path, item["id"]).read_bytes() for item in file_ops.list_file_backups(path)]


@pytest.mark.parametrize("binary", [False, True])
def test_saves_preserve_previous_bytes_and_reject_stale_tabs(editable, binary):
    path, client = editable
    original, first, second = (b"\x00\x01", b"\x00\x02", b"\x00\x03") if binary else (b"original", b"first", b"second")
    path.write_bytes(original)
    loaded = current(client, path)
    display = file_ops.bytes_to_hex_view if binary else lambda value: value.decode()
    response = save(client, path, display(first), loaded["revision"])
    assert response.status_code == 200
    assert path.read_bytes() == first
    assert backup_contents(path) == [original]
    stale = save(client, path, display(second), loaded["revision"])
    assert stale.status_code == 409
    assert path.read_bytes() == first and backup_contents(path) == [original]
    response = save(client, path, display(second), response.get_json()["revision"])
    assert response.status_code == 200
    assert backup_contents(path) == [first, original]
    # Repeated Ctrl+S must not bury the useful backup under identical copies.
    assert save(client, path, display(second), response.get_json()["revision"]).status_code == 200
    assert backup_contents(path) == [first, original]


@pytest.mark.parametrize("revision", [None, "", "bad", 7, True])
def test_missing_revision_never_overwrites_file(editable, revision):
    path, client = editable
    assert save(client, path, "overwrite", revision).status_code == 409
    assert path.read_text() == "original" and backup_contents(path) == []


def test_external_edits_are_preserved(editable):
    path, client = editable
    loaded = current(client, path)
    path.write_text("another editor")
    response = save(client, path, "local draft", loaded["revision"])
    assert response.status_code == 409
    assert "changed on disk" in response.get_json()["error"]
    assert path.read_text() == "another editor" and backup_contents(path) == []


def test_backup_home_remains_the_editor_account_when_sudo_changes_home(editable, monkeypatch):
    path, _ = editable
    home = path.parent / "editor-home"
    monkeypatch.setattr(file_ops, "pwd", SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_dir=str(home))))
    monkeypatch.setenv("HOME", str(path.parent / "root-home"))
    monkeypatch.setenv("FILE_BACKUP_ROOT", "~/backups")
    assert file_ops.write_file_bytes(path, b"edited")[0]
    assert file_ops.file_backup_directory(path).parent == home / "backups"
    assert backup_contents(path) == [b"original"]


def test_hex_editor_saves_remain_bytes_when_current_file_is_text(editable, monkeypatch):
    path, client = editable
    # A binary Git original makes both panes use hex, even if the working
    # copy currently contains valid text. Keep the loaded editor format.
    monkeypatch.setattr(editor, "find_git_root", lambda path: path.parent)
    monkeypatch.setattr(editor, "get_head_content_bytes", lambda path, root: (True, b"\x00binary original"))
    loaded = current(client, path)
    assert loaded["is_binary"]
    for value in (b"first text bytes", b"next text bytes"):
        response = client.post("/api/file", json={
            "path": str(path), "content": file_ops.bytes_to_hex_view(value),
            "revision": loaded["revision"], "is_binary": loaded["is_binary"],
        })
        assert response.status_code == 200 and path.read_bytes() == value
        loaded["revision"] = response.get_json()["revision"]


def test_backup_failure_aborts_before_writing(editable, monkeypatch):
    path, client = editable
    loaded = current(client, path)

    def full_disk(*args):
        raise OSError("Backup disk is full")

    monkeypatch.setattr(file_ops, "_durable_private_file", full_disk)
    response = save(client, path, "local draft", loaded["revision"])
    assert response.status_code == 500
    assert "Backup disk is full" in response.get_json()["error"]
    assert path.read_text() == "original" and backup_contents(path) == []


def test_interrupted_write_keeps_complete_backup(editable, monkeypatch):
    path, client = editable
    loaded = current(client, path)

    def interrupted(target, content, *, exclusive=False):
        target.write_bytes(content[:2])
        raise OSError("Simulated interrupted write")

    monkeypatch.setattr(file_ops, "_write_file_content", interrupted)
    response = save(client, path, "local draft", loaded["revision"])
    assert response.status_code == 500
    assert path.read_bytes() == b"lo"
    assert backup_contents(path) == [b"original"]


def test_saves_preserve_links_permissions_and_extended_attributes(editable):
    path, client = editable
    path.chmod(0o640)
    original_info = path.stat()
    os.setxattr(path, "user.save-test", b"preserve")
    symlink = path.with_name("alias.txt")
    symlink.symlink_to(path)
    hardlink = path.with_name("hardlink.txt")
    hardlink.hardlink_to(path)
    loaded = current(client, symlink)
    response = save(client, symlink, "edited through link", loaded["revision"])
    assert response.status_code == 200
    assert symlink.is_symlink() and hardlink.read_text() == "edited through link"
    assert path.stat().st_ino == original_info.st_ino
    assert path.stat().st_uid == original_info.st_uid
    assert path.stat().st_mode & 0o777 == 0o640
    assert os.getxattr(path, "user.save-test") == b"preserve"
    assert backup_contents(path) == backup_contents(symlink) == [b"original"]


def test_protected_files_and_root_terminal_share_backup_writer(editable):
    from terminal import _write_file_for_terminal

    path, client = editable
    loaded = current(client, path)
    subprocess.run(["sudo", "-n", "chown", "0:0", str(path)], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "600", str(path)], check=True, capture_output=True)
    try:
        response = save(client, path, "protected edit", loaded["revision"])
        assert response.status_code == 200
        assert _write_file_for_terminal(path, "root terminal edit", as_root=True)[0]
        ok, content = file_ops.read_file_bytes(path)
        assert ok and content == b"root terminal edit"
        assert backup_contents(path) == [b"protected edit", b"original"]
        assert path.stat().st_uid == 0 and path.stat().st_mode & 0o777 == 0o600
        assert all(file_ops.file_backup_path(path, item["id"]).stat().st_uid == os.getuid()
                   for item in file_ops.list_file_backups(path))
    finally:
        subprocess.run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(path)],
                       check=True, capture_output=True)


def test_oversized_or_invalid_save_keeps_file_and_backup_history(editable, monkeypatch):
    path, _ = editable
    monkeypatch.setenv("MAX_FILE_SIZE", "8")
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    with application.test_client() as client:
        with client.session_transaction() as session:
            session["csrf_token"] = "save-test"
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "save-test"
        loaded = current(client, path)
        assert save(client, path, "x" * 9, loaded["revision"]).status_code == 400
        assert client.post("/api/file", json=[]).status_code == 400
        assert save(client, path, {}, loaded["revision"]).status_code == 400
        assert path.read_text() == "original" and backup_contents(path) == []
