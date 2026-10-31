"""Exercise the editor as a non-root account against real protected folders."""

import os
import subprocess

import pytest

import app as editor
from utils import file_ops


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("Permission regressions require a non-root test process")

    private = tmp_path / "private"
    (private / "nested").mkdir(parents=True)
    (private / "nested" / "data.txt").write_text("protected content")
    (private / "source.txt").write_text("protected source")
    (private / "alias").symlink_to(private / "nested", target_is_directory=True)
    public = tmp_path / "public"
    (public / "locked").mkdir(parents=True)
    (public / "locked" / "hidden.txt").write_text("hidden content")
    (public / "link").symlink_to(private / "nested", target_is_directory=True)
    recycle = tmp_path / "recycle"
    recycle.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    monkeypatch.setattr(file_ops, "RECYCLE_BIN", recycle)
    monkeypatch.setattr(file_ops, "RECYCLE_META_DIR", metadata)
    monkeypatch.setattr(editor, "RECYCLE_BIN", recycle)
    monkeypatch.setattr(editor, "RECYCLE_META_DIR", metadata)

    def protect(path):
        subprocess.run(
            ["sudo", "-n", "chown", "0:0", str(path)], check=True, capture_output=True,
        )
        subprocess.run(
            ["sudo", "-n", "chmod", "700", str(path)], check=True, capture_output=True,
        )

    protect(private / "nested")
    protect(private)
    protect(public / "locked")
    try:
        yield tmp_path, protect
    finally:
        # Return only these test artifacts to the invoking account for cleanup.
        subprocess.run(
            ["sudo", "-n", "chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["sudo", "-n", "chmod", "-R", "u+rwX", str(tmp_path)],
            check=True, capture_output=True,
        )


@pytest.fixture
def client(workspace):
    app = editor.create_app()
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["csrf_token"] = "test-token"
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "test-token"
        yield client


def assert_ok(response):
    assert response.status_code == 200, response.get_json()
    return response.get_json()


@pytest.mark.parametrize("folder,files,dirs", [
    ("private/nested", ["data.txt"], []),
    ("private", ["nested/data.txt", "source.txt"], ["alias/", "nested/"]),
    ("public", ["locked/hidden.txt"], ["link/", "locked/"]),
])
def test_delete_preview_includes_protected_descendants(client, workspace, folder, files, dirs):
    root, _ = workspace
    data = assert_ok(client.get("/api/dir/preview", query_string={"path": str(root / folder)}))
    assert data["files"] == files
    assert data["dirs"] == dirs
    assert data["total_files"] == len(files)
    assert data["total_dirs"] == len(dirs)


@pytest.mark.parametrize("folder", ["private/nested", "private", "public/locked"])
def test_delete_protected_directory(client, workspace, folder):
    root, _ = workspace
    path = root / folder
    assert_ok(client.post("/api/dir/delete", json={"path": str(path)}))
    assert not file_ops.check_path_exists(path)[0]
    entries = list(file_ops.RECYCLE_BIN.iterdir())
    assert len(entries) == 1
    assert file_ops.get_recycle_metadata(entries[0])["original_path"] == str(path)


@pytest.mark.parametrize("source", ["file", "directory", "protected_file", "protected_directory"])
@pytest.mark.parametrize("destination", ["private", "private/nested"])
@pytest.mark.parametrize("action", ["move", "copy", "symlink"])
def test_transfer_into_another_accounts_directory(client, workspace, source, destination, action):
    root, _ = workspace
    if source == "file":
        path = root / "source.txt"
        path.write_text("source content")
    elif source == "directory":
        path = root / "source"
        path.mkdir()
        (path / "content.txt").write_text("source content")
    elif source == "protected_file":
        path = root / "private/source.txt"
    else:
        path = root / "public/locked"
    target = root / destination / "moved"
    is_dir = source in {"directory", "protected_directory"}
    endpoint = "/api/rename" if action == "move" else f"/api/{'dir' if is_dir else 'file'}/copy"
    assert_ok(client.post(endpoint, json={
        "path": str(path), "directory": str(target.parent), "new_name": target.name,
        "symlink": action == "symlink",
    }))
    assert file_ops.check_path_exists(path)[0] == (action != "move")
    assert file_ops.check_path_exists(target)[0]
    assert file_ops.stat_path(target)[1]["is_symlink"] == (action == "symlink")
    content = target / "content.txt" if source == "directory" else target
    if source == "protected_directory":
        content = target / "hidden.txt"
    assert file_ops.read_file_bytes(content)[0]


@pytest.mark.parametrize("endpoint", ["/api/rename", "/api/file/copy", "/api/dir/copy"])
def test_transfer_can_create_protected_destination_parents(client, workspace, endpoint):
    root, _ = workspace
    source = root / "source.txt"
    if endpoint == "/api/dir/copy":
        source.mkdir()
        (source / "data.txt").write_text("source content")
    else:
        source.write_text("source content")
    target = root / "private/new/deep/moved.txt"
    assert_ok(client.post(endpoint, json={
        "path": str(source), "directory": str(target.parent), "new_name": target.name,
        "create_dirs": True,
    }))
    content = target / "data.txt" if endpoint == "/api/dir/copy" else target
    assert file_ops.read_file_bytes(content) == (True, b"source content")


@pytest.mark.parametrize("existing", ["source.txt", "nested", "dangling"])
@pytest.mark.parametrize("endpoint", ["/api/rename", "/api/file/copy", "/api/dir/copy"])
def test_transfer_respects_collisions_and_overwrite_inside_protected_folder(client, workspace, existing, endpoint):
    root, _ = workspace
    if existing == "dangling":
        # The parent is already protected, so create this fixture with sudo.
        subprocess.run(["sudo", "-n", "ln", "-s", "missing", str(root / "private/dangling")],
                       check=True, capture_output=True)
    source = root / "source.txt"
    if endpoint == "/api/dir/copy":
        source.mkdir()
        (source / "data.txt").write_text("replacement")
    else:
        source.write_text("replacement")
    payload = {"path": str(source), "directory": str(root / "private"), "new_name": existing}
    response = client.post(endpoint, json=payload)
    assert response.status_code == 409, response.get_json()
    content = source / "data.txt" if endpoint == "/api/dir/copy" else source
    assert content.read_text() == "replacement"
    assert_ok(client.post(endpoint, json={**payload, "overwrite": True}))
    target = root / "private" / existing
    content = target / "data.txt" if endpoint == "/api/dir/copy" else target
    assert file_ops.read_file_bytes(content) == (True, b"replacement")
    assert len(list(file_ops.RECYCLE_BIN.iterdir())) == 1


@pytest.mark.parametrize("alias", ["nested", "alias"])
@pytest.mark.parametrize("action,endpoint", [("move", "/api/rename"), ("copy", "/api/dir/copy")])
def test_protected_directory_cannot_transfer_into_itself(client, workspace, alias, action, endpoint):
    root, _ = workspace
    response = client.post(endpoint, json={
        "path": str(root / "private/nested"),
        "directory": str(root / "private" / alias), "new_name": "inside",
    })
    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error"] == f"Cannot {action} a directory into itself"
    assert file_ops.check_path_exists(root / "private/nested/data.txt")[0]


def test_permanent_delete_below_protected_parent(client, workspace):
    root, protect = workspace
    parent = file_ops.RECYCLE_BIN / "protected"
    folder = parent / "folder"
    folder.mkdir(parents=True)
    (folder / "data.txt").write_text("trash")
    protect(parent)
    assert_ok(client.post("/api/dir/delete", json={"path": str(folder)}))
    assert not file_ops.check_path_exists(folder)[0]


def test_permanent_delete_symlink_preserves_referent(workspace):
    root, protect = workspace
    parent = file_ops.RECYCLE_BIN / "protected"
    parent.mkdir()
    link = parent / "link"
    link.symlink_to(root / "private/nested", target_is_directory=True)
    protect(parent)
    ok, message = file_ops.permanently_delete_path(link)
    assert ok, message
    assert not file_ops.check_path_exists(link)[0]
    assert file_ops.read_file_bytes(root / "private/nested/data.txt") == (True, b"protected content")


def test_missing_and_non_directory_destinations_still_fail(client, workspace):
    root, _ = workspace
    for destination in (root / "missing", root / "private/source.txt"):
        response = client.post("/api/rename", json={
            "path": str(root / "private/nested/data.txt"),
            "directory": str(destination), "new_name": "moved.txt",
        })
        assert response.status_code == 400, response.get_json()
    assert file_ops.check_path_exists(root / "private/nested/data.txt")[0]


@pytest.mark.parametrize("kind", ["file", "dir"])
def test_copy_symlink_cannot_overwrite_protected_referent(client, workspace, kind):
    root, _ = workspace
    if kind == "dir":
        source = root / "private/alias"
        target = root / "private/nested"
    else:
        source = root / "link"
        target = root / "private/source.txt"
        source.symlink_to(target)
    response = client.post(f"/api/{kind}/copy", json={
        "path": str(source), "directory": str(target.parent),
        "new_name": target.name, "overwrite": True,
    })
    assert response.status_code == 400, response.get_json()
    assert "via a symlink source" in response.get_json()["error"]
    assert file_ops.check_path_exists(target)[0]
    assert not list(file_ops.RECYCLE_BIN.iterdir())


def test_copy_file_to_itself_through_protected_directory_alias(client, workspace):
    root, _ = workspace
    response = client.post("/api/file/copy", json={
        "path": str(root / "private/nested/data.txt"),
        "directory": str(root / "private/alias"), "new_name": "data.txt", "overwrite": True,
    })
    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error"] == "Source and destination are the same file"
    assert file_ops.read_file_bytes(root / "private/nested/data.txt") == (True, b"protected content")
    assert not list(file_ops.RECYCLE_BIN.iterdir())


@pytest.mark.parametrize("action", ["move", "copy_file", "copy_dir", "preview", "delete"])
def test_ordinary_operations_do_not_use_sudo(client, workspace, monkeypatch, action):
    root, _ = workspace
    source = root / "ordinary"
    source.mkdir()
    (source / "data.txt").write_text("ordinary content")
    file_ops.RECYCLE_BIN.chmod(0o1777)
    file_ops.RECYCLE_META_DIR.chmod(0o1777)

    def unexpected_command(*args, **kwargs):
        pytest.fail(f"Unnecessary subprocess for ordinary operation: {args}")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", unexpected_command)
        if action == "preview":
            data = assert_ok(client.get("/api/dir/preview", query_string={"path": str(source)}))
            assert data["files"] == ["data.txt"]
        elif action == "delete":
            assert_ok(client.post("/api/dir/delete", json={"path": str(source)}))
        else:
            endpoint = {
                "move": "/api/rename", "copy_file": "/api/file/copy", "copy_dir": "/api/dir/copy",
            }[action]
            path = source / "data.txt" if action == "copy_file" else source
            assert_ok(client.post(endpoint, json={
                "path": str(path), "directory": str(root), "new_name": "ordinary-result",
            }))


def test_preview_lists_dangling_and_cyclic_links(client, workspace):
    root, _ = workspace
    source = root / "links"
    source.mkdir()
    (source / "dangling").symlink_to("missing")
    (source / "cycle").symlink_to("cycle")
    data = assert_ok(client.get("/api/dir/preview", query_string={"path": str(source)}))
    assert data["files"] == ["cycle", "dangling"]
    assert data["dirs"] == []
