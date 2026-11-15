"""Verify save races, navigation warnings, and recovery through real page handlers."""

import threading
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.serving import make_server

import app as editor

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def file_page(tmp_path, offline_monaco, request, browser_engine):
    from terminal import terminal_bp

    path = tmp_path / getattr(request, "param", "example.txt")
    path.write_text("original")
    application = editor.create_app()
    application.register_blueprint(terminal_bp)
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as automation:
            if browser_engine == "webkit":
                try:
                    browser = automation.webkit.launch()
                except playwright.Error as exception:
                    if "Host system is missing dependencies" in str(exception) or "Executable doesn't exist" in str(exception):
                        pytest.skip("Optional WebKit runtime or native dependencies unavailable on this host")
                    raise
            else:
                browser = automation.chromium.launch(executable_path="/opt/google/chrome/chrome", args=["--no-sandbox"])
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844},
                                              is_mobile=browser_engine == "webkit", has_touch=browser_engine == "webkit")
                context.add_init_script(offline_monaco)
                context.route("https://**", lambda route: route.abort())
                page = context.new_page()
                yield page, path, f"http://127.0.0.1:{server.server_port}"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def edit(page, value, mode="monaco"):
    if mode == "textbox":
        page.locator("#textbox-editor").fill(value)
    else:
        page.evaluate("value => diffEditor.getModifiedEditor().getModel().setValue(value)", value)


def should_warn(page):
    return page.evaluate("""() => {
        const event = new Event('beforeunload', {cancelable: true});
        window.dispatchEvent(event);
        return event.defaultPrevented;
    }""")


@pytest.mark.parametrize("mode", ["monaco", "textbox"])
@pytest.mark.parametrize("save_path", ["save", "before_action"])
def test_typing_during_save_keeps_newer_edits_unsaved(file_page, mode, save_path):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if mode == "textbox":
        page.locator("#btn-textbox").click()
    assert not should_warn(page)
    edit(page, "first draft", mode)
    assert should_warn(page)
    pending = []
    page.route("**/api/file", lambda route: pending.append(route))
    if save_path == "save":
        page.locator("#btn-save").click()
    else:
        page.evaluate("""() => {
            window.actionResult = null;
            void saveBeforeAction(document.getElementById('btn-render'), 'render')
                .then(value => {window.actionResult = value;});
        }""")
    page.wait_for_function("() => fileSaveInFlight")
    edit(page, "newer unsaved draft", mode)
    page.keyboard.press("Control+s")
    assert should_warn(page) and len(pending) == 1
    response = pending[0].fetch()
    pending[0].fulfill(response=response)
    page.unroute("**/api/file")
    page.wait_for_function("() => !fileSaveInFlight")
    assert path.read_text() == "first draft"
    assert page.evaluate("getCurrentModifiedContent()") == "newer unsaved draft"
    assert page.evaluate("isModified") and should_warn(page)
    playwright.expect(page.locator("#status")).to_contain_text("unsaved")
    if save_path == "before_action":
        page.wait_for_function("() => window.actionResult === false")
    page.locator("#btn-save").click()
    page.wait_for_function("() => !fileSaveInFlight && !isModified")
    assert path.read_text() == "newer unsaved draft"
    assert not should_warn(page)


def test_navigation_warns_and_backup_link_downloads_previous_file(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "local draft")
    warnings = []

    def dismiss(dialog):
        warnings.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", dismiss)
    with page.expect_event("dialog"):
        page.locator(".logo").click(no_wait_after=True)
    assert warnings == ["beforeunload"]
    assert page.evaluate("getCurrentModifiedContent()") == "local draft"
    page.locator("#btn-save").click()
    page.wait_for_function("() => !fileSaveInFlight && !isModified")
    with page.expect_popup() as opened:
        page.get_by_role("button", name="Save options", exact=True).click()
        page.get_by_role("link", name="Backups", exact=True).click()
    history = opened.value
    playwright.expect(history.get_by_role("heading", name="Previous saved versions")).to_be_visible()
    with history.expect_download() as downloaded:
        history.get_by_role("link", name="Download version", exact=True).click()
    recovered = path.with_name("recovered.txt")
    downloaded.value.save_as(recovered)
    assert recovered.read_text() == "original" and path.read_text() == "local draft"
    history.close()
    page.locator(".logo").click()
    playwright.expect(page).to_have_url(base + "/")
    assert warnings == ["beforeunload"]


def test_hex_editor_keeps_format_after_bytes_become_valid_text(file_page):
    from utils.file_ops import bytes_to_hex_view

    page, path, base = file_page
    path.write_bytes(b"\x00binary")
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    for value in (b"first text", b"second text"):
        edit(page, bytes_to_hex_view(value))
        page.locator("#btn-save").click()
        page.wait_for_function("() => !fileSaveInFlight && !isModified")
        assert path.read_bytes() == value
    assert not should_warn(page)


@pytest.mark.parametrize("file_page", ["example.log"], indirect=True)
@pytest.mark.parametrize("save_during_read", [False, True])
def test_delayed_log_refresh_never_replaces_edits_or_a_new_save(file_page, save_during_read):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    if not save_during_read:
        path.write_text("external log append")
    pending = []

    def hold_read(route):
        pending.append((route, route.fetch()))
        page.evaluate("window.heldLogRead = true")

    page.route("**/api/file?*", hold_read)
    page.wait_for_function("() => window.heldLogRead === true", timeout=5000)
    assert len(pending) == 1
    edit(page, "local draft")
    if save_during_read:
        page.locator("#btn-save").click()
        page.wait_for_function("() => !fileSaveInFlight && !isModified")
        assert path.read_text() == "local draft"
    route, response = pending[0]
    route.fulfill(response=response)
    page.wait_for_timeout(100)
    assert page.evaluate("getCurrentModifiedContent()") == "local draft"
    assert should_warn(page) is not save_during_read
    if not save_during_read:
        page.locator("#btn-save").click()
        playwright.expect(page.locator("#status")).to_contain_text("changed on disk")
        assert path.read_text() == "external log append"
        assert page.evaluate("getCurrentModifiedContent()") == "local draft"


@pytest.mark.parametrize("close", ["#editor-close", "#editor-cancel", "Escape"])
def test_terminal_editor_protects_dirty_buffer_on_close(file_page, close):
    page, _, base = file_page
    page.goto(base + "/terminal")
    page.route("**/terminal/editor_response", lambda route: route.fulfill(json={"ok": True}))
    page.evaluate("""() => {
        socket = {id: 'test'};
        terminalToken = 'test';
        openEditorModal('test.txt', 'original');
    }""")
    assert not should_warn(page)
    page.locator("#editor-content").fill("unsaved terminal draft")
    assert should_warn(page)
    if close == "Escape":
        page.keyboard.press("Escape")
    else:
        page.locator(close).click()
    confirm = page.get_by_role("dialog", name="Save modified buffer?")
    playwright.expect(confirm).to_be_visible()
    confirm.locator('[data-action="cancel"]').click()
    playwright.expect(page.locator("#editor-content")).to_have_value("unsaved terminal draft")
    assert should_warn(page)
    page.locator("#editor-close").click()
    confirm.locator('[data-action="discard"]').click()
    playwright.expect(page.locator("#editor-overlay")).to_be_hidden()
    assert not should_warn(page)


def test_terminal_editor_waits_for_pending_save_before_closing(file_page):
    page, _, base = file_page
    page.goto(base + "/terminal")
    pending = []
    page.route("**/terminal/editor_response", lambda route: pending.append(route))
    page.evaluate("""() => {
        socket = {id: 'test'};
        terminalToken = 'test';
        openEditorModal('test.txt', 'original');
    }""")
    page.locator("#editor-content").fill("terminal draft")
    page.locator("#editor-save").click()
    playwright.expect(page.locator("#editor-content")).to_have_attribute("readonly", "")
    page.keyboard.press("Escape")
    page.keyboard.press("Control+Enter")
    assert should_warn(page) and len(pending) == 1
    playwright.expect(page.locator("#editor-overlay")).to_be_visible()
    pending[0].fulfill(json={"ok": True})
    playwright.expect(page.locator("#editor-overlay")).to_be_hidden()
    assert not should_warn(page)


def wait_for_browser_draft(page):
    playwright.expect(page.locator(".draft-recovery")).to_be_visible()
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("Recovery draft stored in this browser")


@pytest.mark.parametrize("wrapper", [True, False])
def test_terminal_recovery_uses_the_file_path_across_working_directories(file_page, wrapper):
    page, path, base = file_page
    other_path = path.with_name("other.txt")
    other_path.write_text("original")

    def open_editor(target, file_path, cwd):
        options = {"recoveryPath": str(file_path)} if wrapper else {
            "mode": "direct", "filePath": file_path.name, "recoveryCwd": str(file_path.parent),
        }
        target.evaluate("""data => {
            currentCwd = data.cwd;
            openEditorModal('Commit message', 'original', data.options);
        }""", {"cwd": cwd, "options": options})

    page.goto(base + "/terminal")
    open_editor(page, path, "/working")
    page.locator("#editor-content").fill("draft for the actual file")
    wait_for_browser_draft(page)
    page.get_by_role("button", name="Save options", exact=True).click()
    backup_url = page.get_by_role("link", name="Backups", exact=True).get_attribute("href")
    assert parse_qs(urlsplit(backup_url).query)["path"] == [str(path)]

    other = page.context.new_page()
    other.goto(base + "/terminal")
    open_editor(other, other_path, "/working")
    other.get_by_role("button", name="Save options", exact=True).click()
    other.get_by_role("button", name="Recover browser drafts", exact=True).click()
    playwright.expect(other.locator(".draft-recovery")).to_contain_text("No earlier browser drafts")
    playwright.expect(other.get_by_role("button", name="Restore draft", exact=True)).to_have_count(0)

    open_editor(other, path, "/elsewhere")
    other.get_by_role("button", name="Restore draft", exact=True).click()
    playwright.expect(other.locator("#editor-content")).to_have_value("draft for the actual file")
    assert path.read_text() == other_path.read_text() == "original"


@pytest.mark.parametrize("replace_all", [False, True])
def test_terminal_replacements_capture_drafts_without_typing_or_navigation(file_page, replace_all):
    page, path, base = file_page
    page.goto(base + "/terminal")
    page.evaluate("path => openEditorModal('test.txt', 'original original', {recoveryPath: path})", str(path))
    page.locator("#editor-search-toggle").click()
    page.locator("#editor-replace-toggle").click()
    page.locator("#editor-find-input").fill("original")
    page.locator("#editor-replace-input").fill("replacement")
    page.locator("#editor-replace-all" if replace_all else "#editor-replace-one").click()
    expected = "replacement replacement" if replace_all else "replacement original"
    playwright.expect(page.locator("#editor-content")).to_have_value(expected)
    # Capture must finish before another tab or page lifecycle event can flush it.
    wait_for_browser_draft(page)

    other = page.context.new_page()
    other.goto(base + "/terminal")
    other.evaluate("path => openEditorModal('test.txt', 'original original', {recoveryPath: path})", str(path))
    other.get_by_role("button", name="Restore draft", exact=True).click()
    playwright.expect(other.locator("#editor-content")).to_have_value(expected)
    assert path.read_text() == "original"


@pytest.mark.parametrize("mode", ["monaco", "textbox", "notebook", "terminal"])
@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"], indirect=True)
def test_browser_draft_survives_tab_close_and_requires_restore(file_page, mode):
    page, path, base = file_page
    if mode == "notebook":
        pytest.importorskip("nbclient")
        path = path.with_suffix(".ipynb")
        path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {"custom": "preserve"},
            "cells": [{"cell_type": "code", "id": "test-cell", "metadata": {}, "source": "original",
                       "execution_count": None, "outputs": []}]}))

    def open_editor(target):
        if mode == "terminal":
            target.goto(base + "/terminal")
            target.evaluate("() => openEditorModal('test.txt', 'original')")
        else:
            target.goto(f"{base}/diff?file={path}")
            if mode == "notebook":
                playwright.expect(target.get_by_label("Cell 1 source")).to_have_value("original")
            else:
                playwright.expect(target.locator("#btn-save")).to_be_enabled()
                if mode == "textbox":
                    target.locator("#btn-textbox").click()

    open_editor(page)
    if mode == "terminal":
        page.locator("#editor-content").fill("recover this terminal draft")
    elif mode == "notebook":
        page.get_by_label("Cell 1 source").fill("recover this notebook draft")
    else:
        edit(page, "recover this file draft", mode)
    wait_for_browser_draft(page)
    assert "recover this" not in path.read_text()
    context = page.context
    page.close(run_before_unload=False)
    reopened = context.new_page()
    open_editor(reopened)
    playwright.expect(reopened.get_by_role("button", name="Restore draft", exact=True)).to_be_visible()
    if mode == "terminal":
        playwright.expect(reopened.locator("#editor-content")).to_have_value("original")
    elif mode == "notebook":
        playwright.expect(reopened.get_by_label("Cell 1 source")).to_have_value("original")
    else:
        assert reopened.evaluate("getCurrentModifiedContent()") == "original"
    reopened.get_by_role("button", name="Restore draft", exact=True).click()
    if mode == "terminal":
        playwright.expect(reopened.locator("#editor-content")).to_have_value("recover this terminal draft")
    elif mode == "notebook":
        playwright.expect(reopened.get_by_label("Cell 1 source")).to_have_value("recover this notebook draft")
    else:
        reopened.wait_for_function("() => getCurrentModifiedContent() === 'recover this file draft'")
    assert should_warn(reopened)
    assert "recover this" not in path.read_text()


def test_restored_draft_preserves_stale_revision_and_newer_disk_file(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "old browser draft")
    wait_for_browser_draft(page)
    path.write_text("newer disk version")
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    playwright.expect(page.get_by_role("button", name="Restore draft", exact=True)).to_be_visible()
    assert page.evaluate("getCurrentModifiedContent()") == "newer disk version"
    page.get_by_role("button", name="Restore draft", exact=True).click()
    page.wait_for_function("() => getCurrentModifiedContent() === 'old browser draft'")
    page.locator("#btn-save").click()
    playwright.expect(page.locator("#status")).to_contain_text("changed on disk")
    assert path.read_text() == "newer disk version"
    assert page.evaluate("getCurrentModifiedContent()") == "old browser draft"


def test_separate_tabs_keep_separate_drafts_and_discard_is_explicit(file_page):
    page, path, base = file_page
    url = f"{base}/diff?file={path}"
    page.goto(url)
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "tab one draft")
    wait_for_browser_draft(page)
    other = page.context.new_page()
    other.goto(url)
    playwright.expect(other.locator("#btn-save")).to_be_enabled()
    edit(other, "tab two draft")
    wait_for_browser_draft(other)
    third = page.context.new_page()
    third.goto(url)
    playwright.expect(third.get_by_role("button", name="Restore draft", exact=True)).to_have_count(2)
    playwright.expect(third.locator(".draft-recovery")).to_contain_text("tab one draft")
    playwright.expect(third.locator(".draft-recovery")).to_contain_text("tab two draft")
    third.on("dialog", lambda dialog: dialog.accept())
    third.locator(".draft-recovery-row").filter(has_text="tab one draft").get_by_role("button", name="Discard draft").click()
    playwright.expect(third.get_by_role("button", name="Restore draft", exact=True)).to_have_count(1)
    assert other.evaluate("getCurrentModifiedContent()") == "tab two draft"
    assert path.read_text() == "original"


def test_successful_save_clears_own_recovery_draft(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "saved draft")
    wait_for_browser_draft(page)
    page.locator("#btn-save").click()
    page.wait_for_function("() => !fileSaveInFlight && !isModified")
    playwright.expect(page.locator(".draft-recovery")).to_be_hidden()
    page.reload()
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    playwright.expect(page.get_by_role("button", name="Restore draft", exact=True)).to_have_count(0)
    assert path.read_text() == "saved draft"


def test_storage_failure_keeps_manual_save_and_download_working(file_page, tmp_path):
    page, path, base = file_page
    page.add_init_script("""
        IDBObjectStore.prototype.put = () => { throw new DOMException('Storage quota exceeded', 'QuotaExceededError'); };
    """)
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "draft despite full storage")
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("Browser draft recovery unavailable")
    page.get_by_role("button", name="Save options", exact=True).click()
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download current draft", exact=True).click()
    target = tmp_path / "downloaded-draft.txt"
    downloaded.value.save_as(target)
    assert target.read_text() == "draft despite full storage"
    page.locator("#btn-save").click()
    page.wait_for_function("() => !fileSaveInFlight && !isModified")
    assert path.read_text() == "draft despite full storage"


@pytest.mark.parametrize("failure", ["quota", "abort"])
def test_coalesced_draft_flush_reports_storage_failure_and_allows_retry(file_page, failure):
    page, _, base = file_page
    page.goto(base + "/terminal")
    result = page.evaluate("""async failure => {
        const container = document.createElement('div');
        document.body.appendChild(container);
        let content = 'first snapshot';
        const recovery = createDraftRecovery({
            kind: 'diff', path: '/coalesced-storage-test', container,
            dirty: () => true, snapshot: () => ({content}),
        });
        const originalPut = IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put = function (...args) {
            if (failure === 'quota') throw new DOMException('Storage quota exceeded', 'QuotaExceededError');
            const request = originalPut.apply(this, args);
            this.transaction.abort();
            return request;
        };
        try {
            const first = recovery.flush();
            content = 'latest snapshot';
            const second = recovery.flush();
            const failed = await Promise.all([first, second]);
            IDBObjectStore.prototype.put = originalPut;
            const retried = await recovery.flush();
            const stored = await new Promise((resolve, reject) => {
                const opening = indexedDB.open('diff-editor-recovery', 1);
                opening.onerror = () => reject(opening.error);
                opening.onsuccess = () => {
                    const db = opening.result;
                    const tx = db.transaction('drafts', 'readonly');
                    const request = tx.objectStore('drafts').getAll();
                    tx.oncomplete = () => { db.close(); resolve(request.result); };
                    tx.onabort = () => { db.close(); reject(tx.error); };
                };
            });
            return {failed, retried, contents: stored.map(record => record.content)};
        } finally {
            IDBObjectStore.prototype.put = originalPut;
            recovery.dispose(true);
            container.remove();
        }
    }""", failure)
    assert result == {"failed": [False, False], "retried": True, "contents": ["latest snapshot"]}


def test_failed_coalesced_preservation_keeps_current_edits_before_restoring(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "older recovery draft")
    wait_for_browser_draft(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    playwright.expect(page.get_by_role("button", name="Restore draft", exact=True)).to_be_visible()
    page.evaluate("""async () => {
        window.originalRecoveryPut = IDBObjectStore.prototype.put;
        IDBObjectStore.prototype.put = () => { throw new DOMException('Storage quota exceeded', 'QuotaExceededError'); };
        diffEditor.getModifiedEditor().getModel().setValue('current edits must survive');
        [...document.querySelectorAll('.draft-recovery-row button')]
            .find(button => button.textContent === 'Restore draft').click();
        await fileRecovery.flush();
    }""")
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("Browser draft recovery unavailable")
    assert page.evaluate("getCurrentModifiedContent()") == "current edits must survive"
    assert should_warn(page) and path.read_text() == "original"

    # Once storage works again, retrying must preserve the edits and allow recovery.
    assert page.evaluate("""async () => {
        IDBObjectStore.prototype.put = window.originalRecoveryPut;
        return await fileRecovery.flush();
    }""")
    page.get_by_role("button", name="Restore draft", exact=True).click()
    page.wait_for_function("() => getCurrentModifiedContent() === 'older recovery draft'")
    page.evaluate("async () => await fileRecovery.flush()")
    page.reload()
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("current edits must survive")
    assert path.read_text() == "original"


def test_saved_recovered_draft_is_not_offered_again_on_reopen(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "recover then save")
    wait_for_browser_draft(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    page.get_by_role("button", name="Restore draft", exact=True).click()
    page.wait_for_function("() => getCurrentModifiedContent() === 'recover then save'")
    page.locator("#btn-save").click()
    page.wait_for_function("() => !fileSaveInFlight && !isModified")
    page.reload()
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    # An older copy may remain for explicit cleanup, but it is already saved.
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    page.get_by_role("button", name="Restore draft", exact=True).click()
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("already matches the editor")
    assert not should_warn(page) and path.read_text() == "recover then save"


def test_large_unicode_browser_draft_is_recoverable(file_page):
    page, path, base = file_page
    value = "Recoverable math αβγ and chemistry 🧪\n" * 6000
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, value)
    wait_for_browser_draft(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    page.get_by_role("button", name="Restore draft", exact=True).click()
    page.wait_for_function("value => getCurrentModifiedContent() === value", arg=value)
    assert path.read_text() == "original"


def test_edits_during_save_recover_with_the_new_saved_revision(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "saved snapshot")
    pending = []
    page.route("**/api/file", lambda route: pending.append(route))
    page.locator("#btn-save").click()
    page.wait_for_function("() => fileSaveInFlight")
    edit(page, "newer recovery draft")
    pending[0].fulfill(response=pending[0].fetch())
    page.unroute("**/api/file")
    page.wait_for_function("() => !fileSaveInFlight")
    page.evaluate("async () => await fileRecovery.flush()")
    assert path.read_text() == "saved snapshot"
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    page.get_by_role("button", name="Restore draft", exact=True).click()
    page.wait_for_function("() => getCurrentModifiedContent() === 'newer recovery draft'")
    page.locator("#btn-save").click()
    page.wait_for_function("() => !fileSaveInFlight && !isModified")
    assert path.read_text() == "newer recovery draft"


def test_restoring_an_older_draft_also_preserves_current_unsaved_edits(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "older recovery draft")
    wait_for_browser_draft(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    playwright.expect(page.get_by_role("button", name="Restore draft", exact=True)).to_be_visible()
    edit(page, "current edits must survive")
    wait_for_browser_draft(page)
    page.get_by_role("button", name="Restore draft", exact=True).click()
    page.wait_for_function("() => getCurrentModifiedContent() === 'older recovery draft'")
    page.evaluate("async () => await fileRecovery.flush()")
    page.reload()
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("current edits must survive")
    playwright.expect(page.locator(".draft-recovery")).to_contain_text("older recovery draft")
    assert path.read_text() == "original"


@pytest.mark.parametrize("width", [390, 600, 601, 800, 1200])
def test_phone_status_icons_preserve_restored_toolbar_spacing(file_page, width):
    page, path, base = file_page
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    layout = page.locator("header").evaluate("""header => ({
        padding: getComputedStyle(header).paddingLeft,
        gap: getComputedStyle(header.querySelector('nav')).gap,
        spacer: getComputedStyle(header.querySelector('.spacer')).display,
        logo: header.querySelector('.logo').textContent,
        logoFont: getComputedStyle(header.querySelector('.logo')).fontSize,
        buttonPadding: getComputedStyle(document.getElementById('btn-wrap')).paddingLeft,
    })""")
    assert layout == {"padding": "16px", "gap": "8px" if width <= 600 else "16px",
                      "spacer": "block", "logo": "File Browser", "logoFont": "17.6px",
                      "buttonPadding": "9.6px" if width <= 600 else "12.8px"}
    status = page.locator("#status")
    assert status.evaluate("el => el.tagName") == "SPAN"
    assert status.evaluate("el => getComputedStyle(el).fontSize") == ("0px" if width <= 600 else "13.6px")
    for css_class, message, expected in [
        ("status", "comparing to original", "⇄"),
        ("status modified", "Modified", "✎"),
        ("status", "Saving...", "↻"),
        ("status", "Loading file...", "↻"),
        ("status saved", "File saved", "✓"),
        ("status error", "Save failed: full details must remain available", "!"),
    ]:
        page.evaluate("args => {const el=document.getElementById('status');el.className=args[0];el.textContent=args[1];}",
                      [css_class, message])
        playwright.expect(status).to_have_attribute("data-status-icon", expected)
        assert status.text_content() == message
        if width <= 600:
            assert status.evaluate("el => getComputedStyle(el, '::before').content") == f'"{expected}"'
        else:
            assert status.evaluate("el => getComputedStyle(el, '::before').content") == "none"
    if width <= 600:
        messages = []

        def dismiss(dialog):
            messages.append(dialog.message)
            dialog.dismiss()

        page.once("dialog", dismiss)
        status.click()
        assert messages == ["Save failed: full details must remain available"]
    else:
        assert status.get_attribute("role") is None


def test_recover_menu_keeps_an_explicit_empty_or_current_result_visible(file_page):
    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    panel = page.locator(".draft-recovery")
    playwright.expect(panel).to_be_visible()
    playwright.expect(panel).to_contain_text("No earlier browser drafts")
    page.wait_for_timeout(200)
    playwright.expect(panel).to_be_visible()
    edit(page, "current draft shown explicitly")
    wait_for_browser_draft(page)
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    playwright.expect(panel).to_contain_text("Current draft is already open in the editor")
    playwright.expect(panel).to_contain_text("current draft shown explicitly")
    assert path.read_text() == "original"


def test_recover_menu_brings_scrolled_notebook_drafts_into_view(file_page):
    pytest.importorskip("nbclient")
    page, path, base = file_page
    path = path.with_suffix(".ipynb")
    path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
        {"cell_type": "code", "id": f"cell-{i}", "metadata": {}, "source": f"original {i}",
         "outputs": [], "execution_count": None} for i in range(20)]}))
    page.goto(f"{base}/diff?file={path}")
    page.get_by_label("Cell 1 source").fill("a notebook recovery draft")
    wait_for_browser_draft(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    playwright.expect(page.get_by_role("button", name="Restore draft", exact=True)).to_be_visible()
    page.locator(".notebook-workspace").evaluate("el => {el.scrollTop=el.scrollHeight;}")
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    page.wait_for_function("""() => {
        const panel = document.querySelector('.draft-recovery');
        const bounds = panel.getBoundingClientRect();
        return bounds.top >= 0 && bounds.bottom <= innerHeight;
    }""")
    assert json.loads(path.read_text())["cells"][0]["source"] == "original 0"


@pytest.mark.parametrize("original", ["original backup text", "<script>window.__recoveryUnsafe=true</script>"])
def test_saved_versions_preview_compare_restore_and_refresh_history(file_page, original):
    from utils import file_ops

    page, path, base = file_page
    path.write_text(original)
    assert file_ops.write_file_bytes(path, b"current disk text")[0]
    page.goto(f"{base}/backups?path={path}")
    page.locator('#backup-versions button[data-action="preview"]').first.click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    playwright.expect(viewer.locator(".recovery-preview-content")).to_contain_text("-current disk text")
    assert path.read_text() == "current disk text"
    viewer.get_by_role("button", name="Preview version", exact=True).click()
    playwright.expect(viewer.locator(".recovery-preview-content")).to_have_text(original)
    assert page.evaluate("window.__recoveryUnsafe === undefined")
    page.on("dialog", lambda dialog: dialog.accept())
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    playwright.expect(viewer).to_contain_text("Version restored to file")
    playwright.expect(page.locator("#backup-versions tbody tr")).to_have_count(2)
    assert path.read_text() == original
    # The new backup of the overwritten file is immediately usable for undo.
    page.locator('#backup-versions button[data-action="restore"]').first.click()
    page.wait_for_function("() => document.querySelector('#backup-versions tbody').children.length === 3")
    assert path.read_text() == "current disk text"


def test_saved_version_restore_cancellation_and_stale_comparison_keep_file(file_page):
    from utils import file_ops

    page, path, base = file_page
    assert file_ops.write_file_bytes(path, b"current")[0]
    page.goto(f"{base}/backups?path={path}")
    page.locator('button[data-action="preview"]').click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    playwright.expect(viewer.get_by_role("button", name="Restore to file", exact=True)).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.dismiss())
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    assert path.read_text() == "current" and len(file_ops.list_file_backups(path)) == 1
    path.write_text("external changes")
    page.on("dialog", lambda dialog: dialog.accept())
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    playwright.expect(viewer).to_contain_text("changed on disk")
    playwright.expect(viewer.get_by_role("button", name="Restore to file", exact=True)).to_be_disabled()
    assert path.read_text() == "external changes"
    viewer.get_by_role("button", name="Refresh comparison", exact=True).click()
    playwright.expect(viewer.locator(".recovery-preview-content")).to_contain_text("-external changes")
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    playwright.expect(viewer).to_contain_text("Version restored to file")
    assert path.read_text() == "original"


@pytest.mark.parametrize("mode", ["diff", "hex", "notebook", "terminal"])
def test_current_browser_draft_can_be_compared_and_restored_directly(file_page, mode):
    from utils import file_ops

    page, path, base = file_page
    value = "directly restored browser draft"
    expected_bytes = value.encode()
    if mode == "hex":
        path.write_bytes(b"\x00binary original")
        expected_bytes = b"ASCII bytes restored from hex"
        value = file_ops.bytes_to_hex_view(expected_bytes)
    if mode == "notebook":
        pytest.importorskip("nbclient")
        path = path.with_suffix(".ipynb")
        path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {"keep": True}, "cells": [
            {"cell_type": "code", "id": "code-cell", "metadata": {}, "source": "original",
             "outputs": [], "execution_count": None}]}))
        page.goto(f"{base}/diff?file={path}")
        page.get_by_label("Cell 1 source").fill(value)
    elif mode == "terminal":
        page.goto(base + "/terminal")
        page.evaluate("path => openEditorModal('Friendly label, not a filename', 'original', {recoveryPath:path})", str(path))
        page.locator("#editor-content").fill(value)
    else:
        page.goto(f"{base}/diff?file={path}")
        playwright.expect(page.locator("#btn-save")).to_be_enabled()
        edit(page, value)
    before = path.read_bytes()
    wait_for_browser_draft(page)
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    row = page.locator(".draft-recovery-row").first if mode == "hex" else page.locator(".draft-recovery-row").filter(has_text=value)
    row.get_by_role("button", name="Preview / compare", exact=True).click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    if mode != "hex":
        playwright.expect(viewer.locator(".recovery-preview-content")).to_contain_text(value)
    else:
        playwright.expect(viewer).to_contain_text("Binary data is shown as hex")
        playwright.expect(viewer.get_by_role("button", name="Restore to file", exact=True)).to_be_enabled()
    assert path.read_bytes() == before
    page.on("dialog", lambda dialog: dialog.accept())
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    playwright.expect(viewer).to_contain_text("Version restored to file")
    if mode == "notebook":
        assert json.loads(path.read_text())["cells"][0]["source"] == value
        assert json.loads(path.read_text())["metadata"]["keep"]
        playwright.expect(page.locator("#notebook-save")).to_be_disabled()
    elif mode == "terminal":
        assert path.read_text() == value
        assert not page.evaluate("editorContentChanged()")
    else:
        assert path.read_bytes() == expected_bytes
        assert not page.evaluate("isModified")
        playwright.expect(page.locator("#status")).to_have_class("status saved")
        # The editor now holds the restored file's revision for later saves.
        with page.expect_response(lambda response: response.url.endswith("/api/file")
                                  and response.request.method == "POST") as saved:
            page.locator("#btn-save").click()
        assert saved.value.status == 200
        assert path.read_bytes() == expected_bytes
    assert len(file_ops.list_file_backups(path)) == 1


def test_direct_restore_preserves_other_unsaved_editor_changes(file_page):
    from utils import file_ops

    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "older draft to restore")
    wait_for_browser_draft(page)
    path.write_text("new disk baseline")
    page.on("dialog", lambda dialog: dialog.accept())
    page.reload()
    playwright.expect(page.get_by_role("button", name="Restore draft", exact=True)).to_be_visible()
    edit(page, "keep these separate unsaved edits")
    wait_for_browser_draft(page)
    old = page.locator(".draft-recovery-row").filter(has_text="older draft to restore")
    old.get_by_role("button", name="Restore to file", exact=True).click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    playwright.expect(viewer).to_contain_text("Version restored to file")
    assert path.read_text() == "older draft to restore"
    assert page.evaluate("getCurrentModifiedContent()") == "keep these separate unsaved edits"
    assert page.evaluate("isModified")
    backup = file_ops.list_file_backups(path)[0]
    assert file_ops.file_backup_path(path, backup["id"]).read_text() == "new disk baseline"


def test_saved_version_can_restore_a_deleted_file_from_its_page(file_page):
    from utils import file_ops

    page, path, base = file_page
    assert file_ops.write_file_bytes(path, b"current")[0]
    path.rename(path.with_suffix(".moved"))
    page.goto(f"{base}/backups?path={path}")
    page.on("dialog", lambda dialog: dialog.accept())
    page.locator('button[data-action="restore"]').click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    playwright.expect(viewer).to_contain_text("Version restored to file")
    assert path.read_text() == "original"


def test_lost_restore_response_is_not_replayed(file_page):
    from utils import file_ops

    page, path, base = file_page
    page.goto(f"{base}/diff?file={path}")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()
    edit(page, "restore once")
    wait_for_browser_draft(page)
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    page.locator(".draft-recovery-row").get_by_role("button", name="Preview / compare", exact=True).click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    playwright.expect(viewer.get_by_role("button", name="Restore to file", exact=True)).to_be_enabled()
    requests = []

    def lose_response(route):
        response = route.fetch()
        requests.append(response.status)
        route.abort()

    page.route("**/api/file/recovery/restore", lose_response)
    page.on("dialog", lambda dialog: dialog.accept())
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    playwright.expect(viewer).to_contain_text("restore response was lost")
    playwright.expect(viewer.get_by_role("button", name="Restore to file", exact=True)).to_be_disabled()
    assert requests == [200] and path.read_text() == "restore once"
    assert len(file_ops.list_file_backups(path)) == 1
    viewer.get_by_role("button", name="Refresh comparison", exact=True).click()
    playwright.expect(viewer).to_contain_text("This version is already on disk")
    assert requests == [200]


def test_restore_uses_the_path_that_was_actually_previewed(file_page, monkeypatch):
    page, path, base = file_page
    other = path.with_name("other.txt")
    other.write_text("original")
    monkeypatch.setenv("RECOVERY_TEST_NAME", path.name)
    target = str(path.parent) + "/$RECOVERY_TEST_NAME"
    page.goto(base + "/terminal")
    page.evaluate("path => openEditorModal('Environment target', 'original', {recoveryPath:path})", target)
    page.locator("#editor-content").fill("restore only the previewed file")
    wait_for_browser_draft(page)
    page.get_by_role("button", name="Save options", exact=True).click()
    page.get_by_role("button", name="Recover browser drafts", exact=True).click()
    page.locator(".draft-recovery-row").get_by_role("button", name="Preview / compare", exact=True).click()
    viewer = page.get_by_role("region", name="Recovery preview and comparison")
    playwright.expect(viewer).to_contain_text(str(path))
    playwright.expect(viewer.get_by_role("button", name="Restore to file", exact=True)).to_be_enabled()
    monkeypatch.setenv("RECOVERY_TEST_NAME", other.name)
    page.on("dialog", lambda dialog: dialog.accept())
    viewer.get_by_role("button", name="Restore to file", exact=True).click()
    playwright.expect(viewer).to_contain_text("Version restored to file")
    assert path.read_text() == "restore only the previewed file"
    assert other.read_text() == "original"
