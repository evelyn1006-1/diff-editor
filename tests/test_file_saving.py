"""Explicit saves preserve revisions and recoverable copies before overwrites."""

import os
import json
import subprocess
from types import SimpleNamespace

import pytest

import app as editor
from utils import file_ops
from utils import recovery_ops


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


def recovery_preview(client, path, source):
    return client.post("/api/file/recovery/preview", json={"path": str(path), **source})


def recovery_restore(client, path, source, preview):
    return client.post("/api/file/recovery/restore", json={"path": str(path), **source,
        **{key: preview[key] for key in ("exists", "revision", "candidate_revision", "target")}})


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


def test_backup_downloads_are_private_attachments_and_survive_deleted_original(editable):
    path, client = editable
    loaded = current(client, path)
    assert save(client, path, "first edit", loaded["revision"]).status_code == 200
    directory = file_ops.file_backup_directory(path)
    backup = file_ops.list_file_backups(path)[0]
    assert directory.stat().st_mode & 0o777 == 0o700
    assert file_ops.file_backup_path(path, backup["id"]).stat().st_mode & 0o777 == 0o600
    assert (directory / "original-path.txt").read_text() == str(path)
    # A move outside the expected name must not make recovery inaccessible.
    path.rename(path.with_suffix(".moved"))
    listing = client.get("/backups", query_string={"path": str(path)})
    assert listing.status_code == 200 and b"Download version" in listing.data
    response = client.get(f"/api/file/backups/{backup['id']}", query_string={"path": str(path)})
    assert response.status_code == 200 and response.data == b"original"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/api/file/backups/original-path.txt", query_string={"path": str(path)}).status_code == 404


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


@pytest.mark.parametrize("original", [b"original", b"\x00\x01binary", b"\xffnon-UTF8", b"<script>unsafe()</script>"])
def test_saved_backup_preview_and_restore_preserves_exact_bytes(editable, original):
    path, client = editable
    path.write_bytes(original)
    path.chmod(0o640)
    assert file_ops.write_file_bytes(path, b"current disk version")[0]
    source = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]}
    response = recovery_preview(client, path, source)
    assert response.status_code == 200
    state = response.get_json()
    assert not state["identical"] and state["exists"] and not state["truncated"]
    assert state["candidate_size"] == len(original)
    assert state["revision"] == file_ops.file_revision(b"current disk version")
    assert state["candidate_revision"] == file_ops.file_revision(original)
    assert "Current file" in state["diff"] and "Selected recovery version" in state["diff"]
    assert path.read_bytes() == b"current disk version"
    response = recovery_restore(client, path, source, state)
    assert response.status_code == 200 and path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o640
    assert backup_contents(path) == [b"current disk version", original]


@pytest.mark.parametrize("kind,binary,content,expected", [
    ("diff", False, "browser text draft", b"browser text draft"),
    ("diff", True, "00 01 ff", b"\x00\x01\xff"),
    ("terminal", False, "terminal text draft", b"terminal text draft"),
])
def test_browser_draft_direct_restore_uses_current_revision_and_backups(editable, kind, binary, content, expected):
    path, client = editable
    source = {"source": "draft", "kind": kind, "content": content, "is_binary": binary}
    response = recovery_preview(client, path, source)
    assert response.status_code == 200
    state = response.get_json()
    assert state["candidate_revision"] == file_ops.file_revision(expected)
    assert recovery_restore(client, path, source, state).status_code == 200
    assert path.read_bytes() == expected and backup_contents(path) == [b"original"]


def test_notebook_draft_validation_and_raw_backup_restore_are_distinct(editable):
    nbformat = pytest.importorskip("nbformat")
    from utils import notebook_ops

    path, client = editable
    path = path.with_suffix(".ipynb")
    path.write_bytes(b"old invalid notebook bytes")
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("print(42)")], metadata={"keep": True})
    source = {"source": "draft", "kind": "notebook", "content": json.dumps(notebook)}
    state = recovery_preview(client, path, source).get_json()
    assert recovery_restore(client, path, source, state).status_code == 200
    assert path.read_bytes() == notebook_ops.serialize_notebook(notebook)
    invalid = recovery_preview(client, path, {**source, "content": "not JSON"})
    assert invalid.status_code == 400
    backup = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]}
    state = recovery_preview(client, path, backup).get_json()
    assert recovery_restore(client, path, backup, state).status_code == 200
    assert path.read_bytes() == b"old invalid notebook bytes"


@pytest.mark.parametrize("source_type", ["backup", "draft"])
def test_recovery_recreates_missing_file_but_rejects_a_racing_creation(editable, source_type):
    path, client = editable
    assert file_ops.write_file_bytes(path, b"current")[0]
    source = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]} if source_type == "backup" else {
        "source": "draft", "kind": "diff", "content": "original"}
    path.rename(path.with_suffix(".moved"))
    state = recovery_preview(client, path, source).get_json()
    assert not state["exists"] and state["revision"] is None
    path.write_bytes(b"another process created this")
    assert recovery_restore(client, path, source, state).status_code == 409
    assert path.read_bytes() == b"another process created this"
    path.rename(path.with_suffix(".racing-copy"))
    assert recovery_restore(client, path, source, state).status_code == 200
    assert path.read_bytes() == b"original"


def test_restore_rejects_stale_file_and_changed_candidate_without_overwriting(editable):
    path, client = editable
    assert file_ops.write_file_bytes(path, b"current")[0]
    backup = file_ops.list_file_backups(path)[0]
    source = {"source": "backup", "backup_id": backup["id"]}
    state = recovery_preview(client, path, source).get_json()
    path.write_bytes(b"external edit")
    assert recovery_restore(client, path, source, state).status_code == 409
    assert path.read_bytes() == b"external edit"
    state = recovery_preview(client, path, source).get_json()
    file_ops.file_backup_path(path, backup["id"]).write_bytes(b"changed backup")
    assert recovery_restore(client, path, source, state).status_code == 409
    assert path.read_bytes() == b"external edit"
    assert len(file_ops.list_file_backups(path)) == 1


def test_truncated_comparison_still_restores_the_complete_file(editable):
    path, client = editable
    original = b"A" * (recovery_ops.PREVIEW_BYTES + 50) + b"old end"
    path.write_bytes(original)
    assert file_ops.write_file_bytes(path, original[:-7] + b"new end")[0]
    source = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]}
    state = recovery_preview(client, path, source).get_json()
    assert state["truncated"] and not state["identical"] and not state["diff"]
    assert len(state["candidate"]) <= recovery_ops.PREVIEW_BYTES
    assert recovery_restore(client, path, source, state).status_code == 200
    assert path.read_bytes() == original


def test_recovery_comparison_tracks_missing_final_newline_and_noop_saves(editable):
    path, client = editable
    path.write_bytes(b"same line")
    source = {"source": "draft", "kind": "diff", "content": "same line\n"}
    state = recovery_preview(client, path, source).get_json()
    assert not state["identical"] and "No newline at end of file" in state["diff"]
    assert recovery_restore(client, path, source, state).status_code == 200
    state = recovery_preview(client, path, source).get_json()
    assert state["identical"] and not state["diff"]
    assert recovery_restore(client, path, source, state).status_code == 200
    assert backup_contents(path) == [b"same line"]


def test_recovery_checks_csrf_path_binding_candidate_hash_and_input_types(editable):
    path, client = editable
    assert file_ops.write_file_bytes(path, b"current")[0]
    source = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]}
    state = recovery_preview(client, path, source).get_json()
    for action in ("preview", "restore"):
        assert client.post(f"/api/file/recovery/{action}", json={"path": str(path), **source},
                           headers={"X-CSRF-Token": "wrong"}).status_code == 403
    for invalid in (None, [], {}, {"path": str(path), "source": "other"}):
        assert client.post("/api/file/recovery/preview", json=invalid).status_code == 400
    assert recovery_preview(client, path.with_name("other.txt"), source).status_code == 404
    assert recovery_restore(client, path, source, {**state, "candidate_revision": "wrong"}).status_code == 409
    assert recovery_restore(client, path, source, {**state, "revision": None}).status_code == 409
    assert recovery_restore(client, path, source, {**state, "exists": "false"}).status_code == 400
    assert path.read_bytes() == b"current"


def test_recovery_uses_sudo_and_preserves_protected_file_metadata(editable):
    path, client = editable
    assert file_ops.write_file_bytes(path, b"current")[0]
    source = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]}
    subprocess.run(["sudo", "-n", "chown", "0:0", str(path)], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "600", str(path)], check=True, capture_output=True)
    try:
        state = recovery_preview(client, path, source).get_json()
        assert recovery_restore(client, path, source, state).status_code == 200
        assert file_ops.read_file_bytes(path) == (True, b"original")
        assert path.stat().st_uid == 0 and path.stat().st_mode & 0o777 == 0o600
        assert backup_contents(path) == [b"current", b"original"]
    finally:
        subprocess.run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(path)],
                       check=True, capture_output=True)


def test_wrapper_reports_the_real_file_path_for_recovery(tmp_path, monkeypatch):
    import io
    from urllib.parse import parse_qs
    from utils import editor_wrapper

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "actual.txt"
    path.write_text("wrapper source")
    monkeypatch.setenv("TERMINAL_SESSION_ID", "test")
    monkeypatch.setattr(editor_wrapper.sys, "argv", ["editor_wrapper", "actual.txt"])
    sent = []

    def response(request, **kwargs):
        sent.append(parse_qs(request.data.decode()))
        return io.BytesIO(b'{"saved":false}')

    monkeypatch.setattr(editor_wrapper.urllib.request, "urlopen", response)
    with pytest.raises(SystemExit):
        editor_wrapper.main()
    assert sent[0]["file"] == [str(path)]


def test_recovery_size_limits_and_invalid_hex_leave_files_unchanged(editable, monkeypatch):
    path, client = editable
    for source in ({"source": "draft", "kind": "diff", "content": "not hex", "is_binary": True},
                   {"source": "draft", "kind": "other", "content": "text"},
                   {"source": "draft", "kind": "notebook", "content": "{}"}):
        assert recovery_preview(client, path, source).status_code == 400
    with pytest.raises(ValueError, match="size limit"):
        recovery_ops.candidate_bytes(path, {"source": "draft", "kind": "diff", "content": "large"}, 2)
    assert file_ops.write_file_bytes(path, b"current")[0]
    backup = file_ops.list_file_backups(path)[0]
    with pytest.raises(ValueError, match="recovery size limit"):
        recovery_ops.candidate_bytes(path, {"source": "backup", "backup_id": backup["id"]}, 2)
    assert path.read_bytes() == b"current"


def test_failed_restore_backup_does_not_overwrite_current_file(editable, monkeypatch):
    path, client = editable
    assert file_ops.write_file_bytes(path, b"current")[0]
    source = {"source": "backup", "backup_id": file_ops.list_file_backups(path)[0]["id"]}
    state = recovery_preview(client, path, source).get_json()

    def cannot_backup(*args):
        raise OSError("Cannot preserve the current file")

    monkeypatch.setattr(file_ops, "_backup_before_save", cannot_backup)
    response = recovery_restore(client, path, source, state)
    assert response.status_code == 500
    assert path.read_bytes() == b"current"


def test_recreated_file_is_private_even_with_a_permissive_service_umask(editable):
    path, client = editable
    path.rename(path.with_suffix(".moved"))
    source = {"source": "draft", "kind": "diff", "content": "private recovery contents"}
    state = recovery_preview(client, path, source).get_json()
    previous = os.umask(0)
    try:
        assert recovery_restore(client, path, source, state).status_code == 200
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("as_root", [False, True])
def test_exclusive_recreation_does_not_truncate_a_last_moment_creation(editable, monkeypatch, as_root):
    path, _ = editable
    path.rename(path.with_suffix(".moved"))
    if as_root:
        real_run = file_ops.subprocess.run

        def raced_command(command, **kwargs):
            if "-c" in command and "_write_file_content" in command[command.index("-c") + 1]:
                path.write_bytes(b"external file wins")
            return real_run(command, **kwargs)

        monkeypatch.setattr(file_ops.subprocess, "run", raced_command)
    else:
        writer = file_ops._write_file_content

        def raced_write(target, content, *, exclusive=False):
            path.write_bytes(b"external file wins")
            return writer(target, content, exclusive=exclusive)

        monkeypatch.setattr(file_ops, "_write_file_content", raced_write)
    with pytest.raises(file_ops.FileConflict, match="now exists"):
        file_ops.write_file_bytes(path, b"must not overwrite", expected_missing=True, as_root=as_root)
    assert path.read_bytes() == b"external file wins"


def test_recovery_preserves_symlinks_and_checks_the_resolved_target(editable):
    path, client = editable
    alias = path.with_name("alias.txt")
    alias.symlink_to(path)
    source = {"source": "draft", "kind": "diff", "content": "restored alias content"}
    state = recovery_preview(client, alias, source).get_json()
    assert state["path"] == str(alias) and state["target"] == str(path)
    other = path.with_name("other.txt")
    other.write_text("original")
    alias.rename(alias.with_name("old-alias.txt"))
    alias.symlink_to(other)
    assert recovery_restore(client, alias, source, state).status_code == 409
    assert path.read_text() == other.read_text() == "original"
    state = recovery_preview(client, alias, source).get_json()
    assert recovery_restore(client, alias, source, state).status_code == 200
    assert alias.is_symlink() and other.read_text() == "restored alias content"


def test_recovery_through_a_dangling_notebook_alias_uses_its_logical_format(editable):
    nbformat = pytest.importorskip("nbformat")
    from utils import notebook_ops

    target, client = editable
    alias = target.with_name("alias.ipynb")
    alias.symlink_to(target)
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("print(1)")])
    source = {"source": "draft", "kind": "notebook", "content": json.dumps(notebook)}
    state = recovery_preview(client, alias, source).get_json()
    assert recovery_restore(client, alias, source, state).status_code == 200
    assert target.read_bytes() == notebook_ops.serialize_notebook(notebook)
    backup = {"source": "backup", "backup_id": file_ops.list_file_backups(alias)[0]["id"]}
    target.rename(target.with_suffix(".moved"))
    state = recovery_preview(client, alias, backup).get_json()
    assert not state["exists"]
    assert recovery_restore(client, alias, backup, state).status_code == 200
    assert alias.is_symlink() and target.read_bytes() == b"original"
    assert target.stat().st_mode & 0o777 == 0o600
