"""Terminal-process routes that depend on deployment paths and session state."""

import json
import re
from types import SimpleNamespace

import pytest

import app as editor
import terminal


@pytest.fixture
def terminal_client():
    def build():
        application = editor.create_app()
        application.register_blueprint(terminal.terminal_bp)
        application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        return application.test_client()
    return build


def editor_links(page: bytes) -> tuple[str, dict]:
    html = page.decode()
    backups = re.search(r'id="editor-save"[^>]*data-backups-url="([^"]*)"', html).group(1)
    recovery = json.loads(re.search(r"window\.RECOVERY_URLS = (\{.*?\});", html).group(1))
    return backups, recovery


def test_terminal_page_reaches_editor_routes_through_configured_prefix(terminal_client, monkeypatch):
    # nginx serves /terminal without a forwarded prefix; bare editor paths
    # would fall through to a different backend.
    monkeypatch.setenv("EDITOR_URL_PREFIX", "/diff/")
    backups, recovery = editor_links(terminal_client().get("/terminal").data)
    assert backups == "/diff/backups"
    assert recovery == {"preview": "/diff/api/file/recovery/preview",
                        "restore": "/diff/api/file/recovery/restore"}


def test_terminal_page_keeps_same_process_editor_routes_by_default(terminal_client, monkeypatch):
    monkeypatch.delenv("EDITOR_URL_PREFIX", raising=False)
    backups, recovery = editor_links(terminal_client().get("/terminal").data)
    assert backups == "/backups"
    assert recovery["preview"] == "/api/file/recovery/preview"


def test_prefix_replaces_rather_than_duplicates_a_forwarded_prefix(terminal_client, monkeypatch):
    monkeypatch.setenv("EDITOR_URL_PREFIX", "/diff")
    page = terminal_client().get("/terminal", headers={"X-Forwarded-Prefix": "/diff"}).data
    assert editor_links(page)[0] == "/diff/backups"


def test_pager_poll_replays_relative_commands_in_the_session_cwd(terminal_client, tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("live pager contents\n")
    session = SimpleNamespace(pid=None, is_root=False, effective_cwd=str(tmp_path))
    monkeypatch.setattr(terminal.pty_manager, "validate_token", lambda session_id, token: token == "token")
    monkeypatch.setattr(terminal.pty_manager, "get_session", lambda session_id: session)
    response = terminal_client().post("/terminal/pager_poll", json={"session_id": "sid", "command": "less notes.txt"},
                                      headers={"X-Terminal-Session": "sid", "X-Terminal-Token": "token"})
    assert response.status_code == 200
    assert response.get_json()["content"] == "live pager contents\n"
