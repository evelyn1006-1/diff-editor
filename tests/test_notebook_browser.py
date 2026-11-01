"""Exercise notebook editing, static rich outputs, and batch execution on mobile."""

import base64
import io
import json
import sys
import threading
import time

from PIL import Image
import pytest
from werkzeug.serving import make_server

import app as editor

nbformat = pytest.importorskip("nbformat")
pytest.importorskip("nbclient")
pytest.importorskip("ipykernel")

notebooks = pytest.importorskip("utils.notebook_ops")

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def notebook_page(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOK_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(notebooks, "JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("MAX_NOTEBOOK_SIZE", "2048")
    path = tmp_path / "recovery.ipynb"
    path.write_bytes(notebooks.serialize_notebook(nbformat.v4.new_notebook(
        cells=[nbformat.v4.new_code_cell("print(42)")],
    )))
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as automation:
            browser = automation.chromium.launch(executable_path="/opt/google/chrome/chrome", args=["--no-sandbox"])
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844})
                page = context.new_page()
                page.route("https://**", lambda route: route.abort())
                yield page, path, f"http://127.0.0.1:{server.server_port}/diff?file={path}"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize("save_mode", ["save", "before_action"])
def test_raw_json_editor_keeps_revisions_for_all_save_paths(notebook_page, save_mode):
    page, path, url = notebook_page
    # Supply a minimal Monaco widget for offline runs. Exercise the real page,
    # initialization, save handlers, session, and API through its model interface.
    page.add_init_script("""
        window.require = (dependencies, callback) => callback();
        window.require.config = () => {};
        window.monaco = { editor: {
            defineTheme() {},
            createModel(value) {
                const listeners = [];
                return {
                    getValue: () => value,
                    setValue(next) { value = next; listeners.forEach(listener => listener()); },
                    onDidChangeContent(listener) { listeners.push(listener); },
                };
            },
            createDiffEditor() {
                let models;
                return {
                    setModel(next) { models = next; },
                    getModifiedEditor: () => ({ getModel: () => models.modified }),
                };
            },
        } };
    """)
    page.goto(url + "&raw=1")
    playwright.expect(page.locator("#btn-save")).to_be_enabled()

    def edit(notebook):
        page.evaluate("value => diffEditor.getModifiedEditor().getModel().setValue(value)", json.dumps(notebook))

    def save():
        with page.expect_response(lambda response: response.url.endswith("/api/file")
                                  and response.request.method == "POST") as saved:
            if save_mode == "save":
                page.locator("#btn-save").click()
            else:
                page.evaluate("() => saveBeforeAction(document.getElementById('btn-save'), 'preview')")
        return saved.value

    for value in ("first edit", "second edit"):
        notebook = json.loads(path.read_bytes())
        notebook["cells"][0]["source"] = value
        edit(notebook)
        assert save().status == 200
        assert notebooks.read_notebook(path)[0]["cells"][0]["source"] == value

    local = json.loads(path.read_bytes())
    local["cells"][0]["source"] = "local draft"
    edit(local)
    external = json.loads(path.read_bytes())
    external["cells"][0]["source"] = "external edit"
    path.write_bytes(notebooks.serialize_notebook(external))
    assert save().status == 409
    playwright.expect(page.locator("#status")).to_contain_text("changed on disk")
    assert json.loads(page.evaluate("() => getCurrentModifiedContent()"))["cells"][0]["source"] == "local draft"
    assert notebooks.read_notebook(path)[0] == external


@pytest.mark.parametrize("outcome", ["complete", "conflict", "oversized", "lost_response"])
def test_finished_job_download_and_status_survive_refresh(notebook_page, tmp_path, outcome):
    page, path, url = notebook_page
    page.goto(url)
    source = page.get_by_label("Cell 1 source")
    oversized = outcome in {"oversized", "lost_response"}
    source.fill("import time; time.sleep(1); print('x' * 4096)" if oversized
                else "import time; time.sleep(1); print(42)")
    if outcome == "lost_response":
        def lose_response(route):
            response = route.fetch()
            assert response.status == 202
            job = response.json()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                state = page.request.get(url.split('/diff?')[0] + f"/api/notebook/jobs/{job['job_id']}").json()
                if state["status"] not in notebooks.ACTIVE_STATES:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("Execution did not finish before losing its launch response")
            route.abort()
        page.route("**/api/notebook/execute", lose_response)
        page.get_by_role("button", name="Execute All & Save", exact=True).click()
    else:
        with page.expect_response(lambda response: response.url.endswith("/api/notebook/execute")) as started:
            page.get_by_role("button", name="Execute All & Save", exact=True).click()
        assert started.value.status == 202
    if outcome == "conflict":
        external = json.loads(path.read_bytes())
        external["metadata"]["external_change"] = True
        path.write_bytes(notebooks.serialize_notebook(external))
    expected = "size limit" if oversized else "file changed during execution" if outcome == "conflict" else "outputs saved"
    playwright.expect(page.get_by_role("status")).to_contain_text(expected, timeout=45000)
    playwright.expect(page.locator("#notebook-result")).to_be_visible()
    playwright.expect(page.locator("#notebook-run")).to_be_enabled()
    if outcome == "lost_response":
        # A recovered launch also needs the revision saved before execution,
        # even when its oversized outputs could not replace that input file.
        source.fill("print('new edit after a lost response')")
        page.get_by_role("button", name="Save", exact=True).click()
        playwright.expect(page.get_by_role("status")).to_have_text("Notebook saved")
    page.reload()
    playwright.expect(page.get_by_role("status")).to_contain_text(expected, timeout=45000)
    playwright.expect(page.locator("#notebook-result")).to_be_visible()
    playwright.expect(page.locator("#notebook-run")).to_be_enabled()
    if outcome == "conflict":
        playwright.expect(page.locator("#notebook-reload")).to_be_visible()
        assert notebooks.read_notebook(path)[0]["metadata"]["external_change"]
    if oversized:
        assert path.stat().st_size <= 2048
    with page.expect_download() as downloaded:
        page.locator("#notebook-result").click()
    destination = tmp_path / "downloaded.ipynb"
    downloaded.value.save_as(destination)
    output = notebooks.read_notebook(destination)[0]["cells"][0]["outputs"][0]["text"]
    assert output == ("x" * 4096 + "\n" if oversized else "42\n")


@pytest.mark.parametrize("failure", ["conflict", "lost_response"])
def test_another_tabs_execution_never_replaces_local_draft(notebook_page, failure):
    page, path, url = notebook_page
    release = path.with_suffix(".release")
    page.goto(url)
    page.get_by_label("Cell 1 source").fill(
        "import time\nfrom pathlib import Path\n"
        f"while not Path({release.name!r}).exists(): time.sleep(0.05)\nprint('tab A')"
    )
    other = page.context.new_page()
    other.route("https://**", lambda route: route.abort())
    other.goto(url)
    draft = "print('tab B unsaved draft')"
    other.get_by_label("Cell 1 source").fill(draft)
    with page.expect_response(lambda response: response.url.endswith("/api/notebook/execute")) as started:
        page.get_by_role("button", name="Execute All & Save", exact=True).click()
    assert started.value.status == 202
    try:
        if failure == "lost_response":
            # B's launch never reaches the server. A's job must not be mistaken
            # for a successful launch from B just because B's response is uncertain.
            other.route("**/api/notebook/execute", lambda route: route.abort())
            other.get_by_role("button", name="Execute All & Save", exact=True).click()
            expected_error = "Failed to fetch"
        else:
            with other.expect_response(lambda response: response.url.endswith("/api/notebook/execute")) as rejected:
                other.get_by_role("button", name="Execute All & Save", exact=True).click()
            assert rejected.value.status == 409
            expected_error = "changed on disk"
        playwright.expect(other.get_by_role("status")).to_contain_text(expected_error)
    finally:
        # Keep A active until B has handled its error, even on fast machines.
        release.touch()
    playwright.expect(page.get_by_role("status")).to_contain_text("outputs saved", timeout=45000)
    playwright.expect(other.get_by_label("Cell 1 source")).to_have_value(draft)
    playwright.expect(other.get_by_role("status")).to_contain_text(expected_error)
    playwright.expect(other.locator("#notebook-save")).to_be_enabled()
    playwright.expect(other.locator("#notebook-run")).to_be_enabled()


def test_finished_job_reload_error_releases_controls_and_preserves_download(notebook_page):
    page, _, url = notebook_page
    page.goto(url)
    page.get_by_label("Cell 1 source").fill("print('finished')")
    with page.expect_response(lambda response: response.url.endswith("/api/notebook/execute")) as started:
        page.get_by_role("button", name="Execute All & Save", exact=True).click()
    assert started.value.status == 202
    page.route("**/api/notebook?*", lambda route: route.fulfill(status=400, json={"error": "Notebook too large"}))
    playwright.expect(page.get_by_role("status")).to_contain_text("reloading the notebook failed", timeout=45000)
    playwright.expect(page.locator("#notebook-run")).to_be_enabled()
    playwright.expect(page.locator("#notebook-result")).to_be_visible()
    playwright.expect(page.locator("#notebook-reload")).to_be_visible()
    playwright.expect(page.get_by_label("Cell 1 source")).not_to_have_attribute("readonly", "")


def test_mobile_notebook_editor_outputs_execution_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOK_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(notebooks, "JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setenv("DEFAULT_ROOT", str(tmp_path))
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "pink").save(image, format="PNG")
    png = base64.b64encode(image.getvalue()).decode()
    notebook = nbformat.v4.new_notebook(metadata={"custom": {"preserve": True}}, cells=[
        nbformat.v4.new_code_cell("print('saved')", execution_count=4, outputs=[
            nbformat.v4.new_output("stream", name="stdout", text="saved text output\n"),
            nbformat.v4.new_output("display_data", data={"text/html": "<table><tr><td>saved table</td></tr></table><script>parent.__notebookUnsafe=true</script>"}),
            nbformat.v4.new_output("display_data", data={"image/png": png}),
            nbformat.v4.new_output("display_data", data={"image/svg+xml": "<svg xmlns='http://www.w3.org/2000/svg' width='100' height='40'><text x='0' y='20'>saved SVG</text></svg>"}),
            nbformat.v4.new_output("error", ename="ValueError", evalue="saved error", traceback=["ValueError: saved error"]),
        ]),
        nbformat.v4.new_markdown_cell("# Notebook heading\n\n![attached](attachment:tiny.png)",
                                     attachments={"tiny.png": {"image/png": png}}),
        nbformat.v4.new_raw_cell("raw cell content"),
    ])
    path = tmp_path / "mobile.ipynb"
    path.write_bytes(notebooks.serialize_notebook(notebook))
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as automation:
            browser = automation.chromium.launch(executable_path="/opt/google/chrome/chrome", args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                page.on("dialog", lambda dialog: dialog.accept())
                # Basic editing must work even when optional CDN editors/math are offline.
                page.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
                page.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/diff?file={path}")
                playwright.expect(page.get_by_label("Cell 1 source")).to_have_value("print('saved')")
                playwright.expect(page.get_by_text("saved text output", exact=False)).to_be_visible()
                playwright.expect(page.frame_locator('iframe[title="Saved HTML output"]').get_by_text("saved table")).to_be_visible()
                playwright.expect(page.frame_locator('iframe[title="Saved SVG output"]').locator("text")).to_have_text("saved SVG")
                playwright.expect(page.frame_locator('iframe[title="Markdown preview"]').get_by_role("heading", name="Notebook heading")).to_be_visible()
                playwright.expect(page.get_by_label("Cell 2 source")).not_to_be_visible()
                assert page.evaluate("window.__notebookUnsafe === undefined")
                image_output = page.get_by_alt_text("Saved notebook output")
                playwright.expect(image_output).to_be_visible()
                assert image_output.evaluate("image => image.naturalWidth") == 2
                attached = page.frame_locator('iframe[title="Markdown preview"]').get_by_alt_text("attached")
                playwright.expect(attached).to_be_visible()
                assert attached.evaluate("image => image.naturalWidth") == 2
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.get_by_label("Cell 1 source").evaluate("source => getComputedStyle(source).fontSize") == "16px"

                source = page.get_by_label("Cell 1 source")
                source.fill("import time; time.sleep(1)\nx = 40\nprint(x + 2)")
                page.get_by_role("button", name="Save", exact=True).click()
                playwright.expect(page.get_by_role("status")).to_have_text("Notebook saved")
                saved = notebooks.read_notebook(path)[0]
                assert saved["metadata"]["custom"]["preserve"]
                assert saved["cells"][0]["outputs"][0]["text"] == "saved text output\n"
                assert saved["cells"][1]["attachments"]["tiny.png"]["image/png"] == png

                source.focus()
                page.get_by_role("button", name="+ Markdown", exact=True).click()
                page.get_by_label("Cell 2 source").fill("# New cell")
                page.get_by_role("button", name="Move cell 2 down", exact=True).click()
                playwright.expect(page.get_by_label("Cell 3 source")).to_have_value("# New cell")
                page.get_by_role("button", name="Delete cell 3", exact=True).click()
                playwright.expect(page.locator(".notebook-cell")).to_have_count(3)
                page.get_by_role("button", name="Undo cell action", exact=True).click()
                playwright.expect(page.locator(".notebook-cell")).to_have_count(4)
                page.get_by_label("Cell 3 type").select_option("code")
                page.get_by_label("Cell 3 source").fill("print(x + 3)")
                page.get_by_label("Python environment").select_option("python")
                page.locator("#notebook-python").fill(sys.executable)
                with page.expect_response(lambda response: response.url.endswith("/api/notebook/execute") and response.request.method == "POST") as started:
                    page.get_by_role("button", name="Execute All & Save", exact=True).click()
                assert started.value.status == 202
                playwright.expect(page.get_by_label("Cell 1 source")).to_have_attribute("readonly", "")
                page.reload()
                playwright.expect(page.get_by_role("status")).to_have_text("Execution complete — outputs saved.", timeout=45000)
                playwright.expect(page.get_by_role("button", name="Execute All & Save", exact=True)).to_be_enabled()
                result = notebooks.read_notebook(path)[0]
                assert result["cells"][0]["outputs"][0]["text"] == "42\n"
                assert result["cells"][2]["outputs"][0]["text"] == "43\n"
                assert result["metadata"]["custom"]["preserve"]
                assert result["cells"][1]["attachments"]["tiny.png"]["image/png"] == png
                assert result["cells"][3]["source"] == "raw cell content"
                page.locator(".notebook-workspace").evaluate("element => element.scrollTop = 0")
                page.screenshot(path="/tmp/diff-editor-notebook-mobile.png")
                page.set_viewport_size({"width": 1200, "height": 950})
                playwright.expect(page.get_by_label("Cell 1 source")).to_be_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path="/tmp/diff-editor-notebook-desktop.png")
                assert errors == []
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_notebook_browser_reports_save_conflict_without_losing_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOK_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(notebooks, "JOB_ROOT", tmp_path / "jobs")
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("original")])
    path = tmp_path / "conflict.ipynb"
    path.write_bytes(notebooks.serialize_notebook(notebook))
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as automation:
            browser = automation.chromium.launch(executable_path="/opt/google/chrome/chrome", args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.route("https://**", lambda route: route.abort())
                page.goto(f"http://127.0.0.1:{server.server_port}/diff?file={path}")
                page.get_by_label("Cell 1 source").fill("local draft")
                external = json.loads(path.read_bytes())
                external["cells"][0]["source"] = "external edit"
                path.write_bytes(notebooks.serialize_notebook(external))
                page.get_by_role("button", name="Save", exact=True).click()
                playwright.expect(page.get_by_role("status")).to_contain_text("changed on disk")
                playwright.expect(page.get_by_label("Cell 1 source")).to_have_value("local draft")
                assert notebooks.read_notebook(path)[0]["cells"][0]["source"] == "external edit"
                playwright.expect(page.get_by_role("button", name="Reload from disk")).to_be_visible()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize("width", [390, 1200])
def test_markdown_preview_edit_done_and_reordering(tmp_path, monkeypatch, width):
    monkeypatch.setenv("NOTEBOOK_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(notebooks, "JOB_ROOT", tmp_path / "jobs")
    notebook = nbformat.v4.new_notebook(cells=[
        nbformat.v4.new_markdown_cell("# Original heading", metadata={"preserve": True}),
        nbformat.v4.new_code_cell("x = 1"), nbformat.v4.new_raw_cell("raw content"),
    ])
    path = tmp_path / "markdown.ipynb"
    path.write_bytes(notebooks.serialize_notebook(notebook))
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as automation:
            browser = automation.chromium.launch(executable_path="/opt/google/chrome/chrome", args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.route("https://**", lambda route: route.abort())
                page.goto(f"http://127.0.0.1:{server.server_port}/diff?file={path}")
                preview = page.frame_locator('iframe[title="Markdown preview"]')
                playwright.expect(preview.get_by_role("heading", name="Original heading")).to_be_visible()
                playwright.expect(page.get_by_label("Cell 1 source")).not_to_be_visible()
                page.get_by_role("button", name="Edit cell 1", exact=True).click()
                source = page.get_by_label("Cell 1 source")
                playwright.expect(source).to_be_visible()
                playwright.expect(source).to_be_focused()
                playwright.expect(preview.get_by_role("heading", name="Original heading")).not_to_be_visible()
                source.fill("# Edited heading\n\nUpdated paragraph")
                page.get_by_role("button", name="Move cell 1 down", exact=True).click()
                playwright.expect(page.get_by_label("Cell 2 source")).to_be_visible()
                playwright.expect(page.get_by_label("Cell 2 source")).to_have_value("# Edited heading\n\nUpdated paragraph")
                page.get_by_role("button", name="Done editing cell 2", exact=True).click()
                playwright.expect(page.get_by_label("Cell 2 source")).not_to_be_visible()
                playwright.expect(preview.get_by_role("heading", name="Edited heading")).to_be_visible()
                playwright.expect(preview.get_by_text("Updated paragraph")).to_be_visible()
                page.get_by_role("button", name="Save", exact=True).click()
                playwright.expect(page.get_by_role("status")).to_have_text("Notebook saved")
                saved = notebooks.read_notebook(path)[0]
                assert saved["cells"][1]["source"] == "# Edited heading\n\nUpdated paragraph"
                assert saved["cells"][1]["metadata"] == {"preserve": True}
                page.reload()
                playwright.expect(preview.get_by_role("heading", name="Edited heading")).to_be_visible()
                playwright.expect(page.get_by_label("Cell 2 source")).not_to_be_visible()
                playwright.expect(page.get_by_label("Cell 3 source")).to_be_visible()
                page.get_by_role("button", name="Edit cell 2", exact=True).click()
                page.get_by_role("button", name="Done editing cell 2", exact=True).click()
                playwright.expect(page.get_by_role("button", name="Save", exact=True)).to_be_disabled()
                page.get_by_role("button", name="+ Markdown", exact=True).click()
                playwright.expect(page.get_by_label("Cell 3 source")).to_be_visible()
                playwright.expect(page.get_by_label("Cell 3 source")).to_be_focused()
                page.get_by_label("Cell 3 source").fill("# New Markdown")
                page.get_by_role("button", name="Done editing cell 3", exact=True).click()
                playwright.expect(page.get_by_label("Cell 3 source")).not_to_be_visible()
                assert len(notebooks.read_notebook(path)[0]["cells"]) == 3
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
