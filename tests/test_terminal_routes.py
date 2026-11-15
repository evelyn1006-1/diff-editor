"""Terminal-process routes that depend on deployment paths and session state."""

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


def test_pager_poll_replays_relative_commands_in_the_session_cwd(terminal_client, tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("live pager contents\n")
    session = SimpleNamespace(pid=None, is_root=False, effective_cwd=str(tmp_path))
    monkeypatch.setattr(terminal.pty_manager, "validate_token", lambda session_id, token: token == "token")
    monkeypatch.setattr(terminal.pty_manager, "get_session", lambda session_id: session)
    response = terminal_client().post("/terminal/pager_poll", json={"session_id": "sid", "command": "less notes.txt"},
                                      headers={"X-Terminal-Session": "sid", "X-Terminal-Token": "token"})
    assert response.status_code == 200
    assert response.get_json()["content"] == "live pager contents\n"
