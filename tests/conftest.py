"""Keep save recovery artifacts isolated from the editor's live backups."""

import pytest


@pytest.fixture(autouse=True)
def isolated_file_backups(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_BACKUP_ROOT", str(tmp_path / "save-backups"))

