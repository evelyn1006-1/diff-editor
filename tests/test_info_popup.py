"""Check metadata editing through the rendered popup in a real browser."""

import grp
import os
import pwd
import subprocess
import threading

import pytest
from werkzeug.serving import make_server

import app as editor

playwright = pytest.importorskip("playwright.sync_api")


def test_info_popup_metadata_editing(tmp_path, monkeypatch):
    path = tmp_path / "editable.txt"
    path.write_text("Editable metadata test")
    target = tmp_path / "target.txt"
    target.write_text("Symlink target must remain unchanged")
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setenv("DEFAULT_ROOT", str(tmp_path))
    app = editor.create_app()
    app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as automation:
            browser = automation.chromium.launch(
                executable_path="/opt/google/chrome/chrome", args=["--no-sandbox"],
            )
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.evaluate("path => { void showInfoModal(path); }", str(path))
                modal = page.locator(".modal-overlay").filter(has=page.locator(".info-modal-title"))
                mode = modal.get_by_label("Permissions (octal)")
                mode.fill("4755")
                modal.get_by_role("button", name="Save permissions", exact=True).click()
                playwright.expect(modal.locator('[data-edit="permissions"] .info-edit-status')).to_have_text("Saved.")
                assert path.stat().st_mode & 0o7777 == 0o4755

                # Saving owner must refresh the actual mode without discarding
                # a pending edit in the separate permissions form.
                mode.fill("0700")
                modal.get_by_label("Owner", exact=True).fill("root")
                modal.get_by_label("Group", exact=True).fill("root")
                modal.get_by_role("button", name="Save owner", exact=True).click()
                playwright.expect(modal.locator('[data-edit="ownership"] .info-edit-status')).to_have_text("Saved.")
                playwright.expect(mode).to_have_value("0700")
                playwright.expect(modal.locator("[data-permissions-symbolic]")).to_have_text("-rwxr-xr-x")
                assert path.stat().st_uid == 0
                modal.get_by_role("button", name="Save permissions", exact=True).click()
                playwright.expect(modal.locator('[data-edit="permissions"] .info-edit-status')).to_have_text("Saved.")
                assert path.stat().st_mode & 0o7777 == 0o700

                # An unrelated owner draft must survive a permissions save.
                owner = modal.get_by_label("Owner", exact=True)
                owner.fill("no_such_diff_editor_browser_owner_93852")
                mode.fill("0750")
                modal.get_by_role("button", name="Save permissions", exact=True).click()
                playwright.expect(modal.locator('[data-edit="permissions"] .info-edit-status')).to_have_text("Saved.")
                playwright.expect(owner).to_have_value("no_such_diff_editor_browser_owner_93852")
                modal.get_by_role("button", name="Save owner", exact=True).click()
                playwright.expect(modal.locator('[data-edit="ownership"] .info-edit-status')).to_contain_text("Unknown owner")
                playwright.expect(owner).to_be_enabled()
                assert path.stat().st_uid == 0
                owner.fill(pwd.getpwuid(os.getuid()).pw_name)
                modal.get_by_label("Group", exact=True).fill(grp.getgrgid(os.getgid()).gr_name)
                modal.get_by_role("button", name="Save owner", exact=True).click()
                playwright.expect(modal.locator('[data-edit="ownership"] .info-edit-status')).to_have_text("Saved.")
                page.screenshot(path="/tmp/diff-editor-info-desktop.png")
                modal.get_by_role("button", name="Close", exact=True).click()

                page.set_viewport_size({"width": 390, "height": 844})
                page.evaluate("path => { void showInfoModal(path); }", str(path))
                playwright.expect(modal.get_by_label("Permissions (octal)")).to_have_value("0750")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path="/tmp/diff-editor-info-mobile.png")
                modal.get_by_role("button", name="Close", exact=True).click()

                page.evaluate("path => { void showInfoModal(path); }", str(link))
                playwright.expect(modal.get_by_text("Symlink permissions cannot be changed.")).to_be_visible()
                playwright.expect(modal.get_by_label("Permissions (octal)")).to_have_count(0)
                modal.get_by_label("Owner", exact=True).fill("root")
                modal.get_by_label("Group", exact=True).fill("root")
                modal.get_by_role("button", name="Save owner", exact=True).click()
                playwright.expect(modal.locator('[data-edit="ownership"] .info-edit-status')).to_have_text("Saved.")
                assert link.lstat().st_uid == 0
                assert target.stat().st_uid == os.getuid()
                assert errors == []
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        subprocess.run(
            ["sudo", "-n", "chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["sudo", "-n", "chmod", "-R", "u+rwX", str(tmp_path)], check=True, capture_output=True,
        )
