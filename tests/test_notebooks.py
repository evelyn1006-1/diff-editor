"""Notebook preservation, optimistic saves, optional tools, and real kernels."""

import copy
import fcntl
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

import app as editor

nbformat = pytest.importorskip("nbformat")
pytest.importorskip("nbclient")
pytest.importorskip("ipykernel")

notebooks = pytest.importorskip("utils.notebook_ops")


@pytest.fixture
def setup_notebook(tmp_path, monkeypatch, request):
    if hasattr(request, "param"):
        monkeypatch.setenv("MAX_NOTEBOOK_SIZE", str(request.param))
    monkeypatch.setenv("NOTEBOOK_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(notebooks, "JOB_ROOT", tmp_path / "jobs")
    path = tmp_path / "example.ipynb"
    notebook = nbformat.v4.new_notebook(
        metadata={"custom": {"nested": [1, 2, 3]}},
        cells=[nbformat.v4.new_markdown_cell("# Notebook"),
               nbformat.v4.new_code_cell("print('saved')", execution_count=7,
                                        outputs=[nbformat.v4.new_output("stream", name="stdout", text="saved output\n")]),
               nbformat.v4.new_raw_cell("raw content", metadata={"custom": True})],
    )
    path.write_bytes(notebooks.serialize_notebook(notebook))
    application = editor.create_app()
    application.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    with application.test_client() as client:
        with client.session_transaction() as session:
            session["csrf_token"] = "notebook-test"
        client.environ_base["HTTP_X_CSRF_TOKEN"] = "notebook-test"
        yield path, notebook, client, application


def get_notebook(path, client):
    response = client.get("/api/notebook", query_string={"path": str(path)})
    assert response.status_code == 200
    return response.get_json()


def start(path, client, notebook=None, **options):
    current = get_notebook(path, client)
    return client.post("/api/notebook/execute", json={
        "path": str(path), "notebook": notebook or current["notebook"],
        "revision": current["revision"], "timeout": 30, **options,
    })


def wait(client, job):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = client.get(f"/api/notebook/jobs/{job['job_id']}")
        assert response.status_code == 200
        result = response.get_json()
        if result["status"] not in notebooks.ACTIVE_STATES:
            return result
        time.sleep(0.1)
    pytest.fail("Notebook execution did not finish; worker output is preserved in its job directory")


def test_notebook_roundtrip_preserves_metadata_outputs_and_sources(setup_notebook):
    path, notebook, client, _ = setup_notebook
    notebook.cells[0].source = ["# Notebook\n", "Markdown text"]
    notebook.cells[0].attachments = {"drawing.svg": {"image/svg+xml": "<svg xmlns='http://www.w3.org/2000/svg'/>"}}
    path.write_bytes(notebooks.serialize_notebook(notebook))
    data = get_notebook(path, client)
    assert data["notebook"] == notebook
    edited = data["notebook"]
    edited["cells"][1]["source"] = "print('edited')"
    response = client.post("/api/file", json={"path": str(path), "content": json.dumps(edited), "revision": data["revision"]})
    assert response.status_code == 200
    assert notebooks.read_notebook(path)[0] == edited
    assert edited["cells"][1]["outputs"] == notebook.cells[1].outputs
    assert edited["cells"][0]["source"] == notebook.cells[0].source


def test_empty_new_notebook_and_regular_json_view(setup_notebook):
    path, _, client, _ = setup_notebook
    path.write_bytes(b"")
    data = get_notebook(path, client)
    assert data["empty"]
    assert len(data["notebook"]["cells"]) == 1
    response = client.get("/diff", query_string={"file": str(path)})
    assert b"notebook-workspace" in response.data
    response = client.get("/diff", query_string={"file": str(path), "raw": "1"})
    assert b"editor-container" in response.data and b"notebook-workspace" not in response.data


def test_missing_notebook_tools_falls_back_and_saves_regularly(setup_notebook, monkeypatch):
    path, _, client, _ = setup_notebook
    original_import = editor.importlib.import_module

    def missing(name, *args, **kwargs):
        if name == "utils.notebook_ops":
            raise ModuleNotFoundError("nbclient")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(editor.importlib, "import_module", missing)
    response = client.get("/diff", query_string={"file": str(path)})
    assert b"editor-container" in response.data
    assert b"Notebook tools are unavailable" in response.data
    assert b"notebook-workspace" not in response.data
    regular = client.get("/api/file", query_string={"path": str(path)})
    assert regular.status_code == 200
    assert regular.get_json()["language"] == "json"
    response = client.post("/api/file", json={"path": str(path), "content": "ordinary text edit",
                                             "revision": regular.get_json()["revision"]})
    assert response.status_code == 200
    assert path.read_text() == "ordinary text edit"
    assert client.get("/api/notebook", query_string={"path": str(path)}).status_code == 503
    assert client.post("/api/notebook/execute", json={"path": str(path)}).status_code == 503


def test_notebook_conflict_preserves_disk_and_local_edit(setup_notebook):
    path, notebook, client, _ = setup_notebook
    original = get_notebook(path, client)
    notebook.metadata.external_change = True
    path.write_bytes(notebooks.serialize_notebook(notebook))
    payload = {"path": str(path), "content": json.dumps(original["notebook"]), "revision": original["revision"]}
    response = client.post("/api/file", json=payload)
    assert response.status_code == 409
    response = client.post("/api/notebook/execute", json={
        "path": str(path), "notebook": original["notebook"], "revision": original["revision"],
    })
    assert response.status_code == 409
    assert notebooks.read_notebook(path)[0]["metadata"]["external_change"]


def test_contended_saves_keep_gevent_worker_responsive(setup_notebook):
    path, _, _, _ = setup_notebook
    # Isolate monkey-patching from pytest. A subprocess deadline also detects a
    # blocking flock, which would prevent gevent's own timeouts from firing.
    script = textwrap.dedent("""
        from gevent import monkey
        monkey.patch_all()
        import copy
        import sys
        from pathlib import Path
        import gevent
        from gevent.event import Event
        from utils import file_ops
        from utils import notebook_ops as notebooks

        path = Path(sys.argv[1])
        notebook, revision = notebooks.read_notebook(path)
        reading = Event()
        read_file_bytes = file_ops.read_file_bytes
        def yielding_read(path):
            reading.set()
            gevent.sleep(0.05)
            return read_file_bytes(path)
        file_ops.read_file_bytes = yielding_read
        outcomes = []
        def save(source):
            edited = copy.deepcopy(notebook)
            edited['cells'][1]['source'] = source
            try:
                notebooks.save_notebook(path, edited, revision, 100000)
                outcomes.append('saved')
            except notebooks.NotebookConflict:
                outcomes.append('conflict')

        first = gevent.spawn(save, 'first writer')
        assert reading.wait(timeout=5)
        second = gevent.spawn(save, 'second writer')
        gevent.joinall([first, second], raise_error=True)
        assert outcomes == ['saved', 'conflict'], outcomes
        assert notebooks.read_notebook(path)[0]['cells'][1]['source'] == 'first writer'
        print('Concurrent saves completed; stale writer rejected')
    """)
    result = subprocess.run([sys.executable, "-c", script, str(path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert notebooks.read_notebook(path)[0]["cells"][1]["source"] == "first writer"


def test_raw_notebook_saves_advance_revision_and_reject_stale_edits(setup_notebook):
    path, _, client, _ = setup_notebook
    current = client.get("/api/file", query_string={"path": str(path)}).get_json()
    assert current["revision"] == get_notebook(path, client)["revision"]
    for source in ("first JSON edit", "second JSON edit"):
        notebook = json.loads(current["content"])
        notebook["cells"][1]["source"] = source
        response = client.post("/api/file", json={
            "path": str(path), "content": json.dumps(notebook), "revision": current["revision"],
        })
        assert response.status_code == 200
        current = client.get("/api/file", query_string={"path": str(path)}).get_json()
        assert current["revision"] == response.get_json()["revision"]
    external = json.loads(current["content"])
    external["metadata"]["external_change"] = True
    path.write_bytes(notebooks.serialize_notebook(external))
    response = client.post("/api/file", json={
        "path": str(path), "content": current["content"], "revision": current["revision"],
    })
    assert response.status_code == 409
    assert notebooks.read_notebook(path)[0] == external


@pytest.mark.parametrize("revision", ["missing", None, "", "invalid", 7, True])
def test_notebook_save_requires_a_valid_revision(setup_notebook, revision):
    path, notebook, client, _ = setup_notebook
    original = path.read_bytes()
    notebook.cells[1].source = "local edit"
    payload = {"path": str(path), "content": json.dumps(notebook)}
    if revision != "missing":
        payload["revision"] = revision
    assert client.post("/api/file", json=payload).status_code == 409
    assert path.read_bytes() == original


@pytest.mark.parametrize("setup_notebook", [2048], indirect=True)
@pytest.mark.parametrize("endpoint", ["/api/file", "/api/notebook/execute"])
def test_oversized_notebook_input_does_not_replace_file_or_launch(setup_notebook, endpoint):
    path, notebook, client, _ = setup_notebook
    current = get_notebook(path, client)
    original = path.read_bytes()
    notebook.cells[1].source = "x" * 4096
    response = client.post(endpoint, json={
        "path": str(path), "revision": current["revision"],
        "content": json.dumps(notebook), "notebook": notebook,
    })
    assert response.status_code == 400
    assert "size limit" in response.get_json()["error"]
    assert path.read_bytes() == original
    assert get_notebook(path, client)["job"] is None


@pytest.mark.parametrize("setup_notebook", [2048], indirect=True)
def test_oversized_execution_result_stays_downloadable_and_file_stays_editable(setup_notebook):
    path, notebook, client, _ = setup_notebook
    notebook.cells = [nbformat.v4.new_code_cell("print('x' * 4096)")]
    response = start(path, client, notebook)
    assert response.status_code == 202
    job = response.get_json()
    state = wait(client, job)
    assert state["status"] == "failed" and not state["saved"]
    assert "size limit" in state["error"] and state["result_available"]
    current = get_notebook(path, client)
    assert current["notebook"] == notebook
    assert current["revision"] == job["input_revision"]
    assert path.stat().st_size <= 2048
    assert client.get("/api/file", query_string={"path": str(path)}).status_code == 200
    download = client.get(f"/api/notebook/jobs/{job['job_id']}/result")
    assert download.status_code == 200 and len(download.data) > 2048
    assert json.loads(download.data)["cells"][0]["outputs"][0]["text"] == "x" * 4096 + "\n"
    notebook.cells[0].source = "print('next edit')"
    response = client.post("/api/file", json={
        "path": str(path), "content": json.dumps(notebook), "revision": current["revision"],
    })
    assert response.status_code == 200


@pytest.mark.parametrize("request_limit,environment_limit,output_size,saved", [
    (None, None, 2, True),
    (None, 2048, 4096, False),
    (2048, 16384, 4096, False),
])
def test_worker_accepts_older_job_requests_without_bypassing_size_limits(
    setup_notebook, monkeypatch, request_limit, environment_limit, output_size, saved,
):
    path, notebook, _, _ = setup_notebook
    if environment_limit is None:
        monkeypatch.delenv("MAX_NOTEBOOK_SIZE", raising=False)
    else:
        monkeypatch.setenv("MAX_NOTEBOOK_SIZE", str(environment_limit))
    notebook.cells = [nbformat.v4.new_code_cell(f"print('x' * {output_size})")]
    original = notebooks.serialize_notebook(notebook)
    path.write_bytes(original)
    scope, job_id = "a" * 32, "b" * 32
    directory = notebooks.job_directory(scope, job_id)
    directory.mkdir(parents=True)
    request = {
        "path": str(path), "notebook": notebook, "revision": notebooks.revision(original),
        "environment": notebooks.resolve_environment("default"), "timeout": 30,
    }
    if request_limit is not None:
        request["max_size"] = request_limit
    (directory / "request.json").write_text(json.dumps(request))
    with (directory / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notebooks.execute_job(directory, lock.fileno())
    state = notebooks.job_status(scope, job_id)
    assert state["saved"] is saved and state["result_available"]
    result = notebooks.read_notebook(directory / "result.ipynb")[0]
    assert result["cells"][0]["outputs"][0]["text"] == "x" * output_size + "\n"
    if saved:
        assert state["status"] == "complete" and state["error"] is None
        assert notebooks.read_notebook(path)[0] == result
    else:
        assert state["status"] == "failed" and "size limit" in state["error"]
        assert path.read_bytes() == original


@pytest.mark.parametrize("invalid", ["not JSON", "[]", '{"nbformat":3}', '{"nbformat":4,"cells":[]}'])
def test_invalid_notebooks_do_not_overwrite_file(setup_notebook, invalid):
    path, _, client, _ = setup_notebook
    original = path.read_bytes()
    response = client.post("/api/file", json={"path": str(path), "content": invalid})
    assert response.status_code == 400
    assert path.read_bytes() == original


def test_notebook_validation_never_repairs_or_mutates_input(setup_notebook):
    _, notebook, _, _ = setup_notebook
    notebook.cells[0].id = notebook.cells[1].id
    original = copy.deepcopy(notebook)
    with pytest.raises(ValueError):
        notebooks.validate_notebook(notebook)
    assert notebook == original


@pytest.mark.parametrize("timeout", [-1, True, "5", 86401])
def test_notebook_rejects_invalid_execution_options(setup_notebook, timeout):
    path, _, client, _ = setup_notebook
    response = start(path, client, timeout=timeout)
    assert response.status_code == 400


def test_notebook_csrf_and_job_isolation(setup_notebook):
    path, notebook, client, application = setup_notebook
    assert client.post("/api/notebook/execute", json={}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert client.post("/api/notebook/markdown", json={}, headers={"X-CSRF-Token": "bad"}).status_code == 403
    notebook.cells = [nbformat.v4.new_code_cell("print('done')")]
    response = start(path, client, notebook)
    assert response.status_code == 202
    job = response.get_json()
    other = application.test_client()
    assert other.get(f"/api/notebook/jobs/{job['job_id']}").status_code == 404
    assert other.get(f"/api/notebook/jobs/{job['job_id']}/result").status_code == 404
    assert client.get("/api/notebook/jobs/invalid-id").status_code == 404
    assert wait(client, job)["status"] == "complete"


def test_execute_cells_share_variables_use_directory_and_shut_down(setup_notebook):
    path, notebook, client, _ = setup_notebook
    (path.parent / "input.txt").write_text("40")
    notebook.cells = [
        nbformat.v4.new_code_cell("from pathlib import Path\nx = int(Path('input.txt').read_text())"),
        nbformat.v4.new_code_cell("import os\nprint(x + 2)\nprint(os.getpid())"),
    ]
    notebook.cells[0].source = ["from pathlib import Path\n", "x = int(Path('input.txt').read_text())"]
    response = start(path, client, notebook)
    assert response.status_code == 202
    state = wait(client, response.get_json())
    assert state["status"] == "complete", state.get("error")
    assert state["saved"]
    result = notebooks.read_notebook(path)[0]
    output = result["cells"][1]["outputs"][0]["text"].splitlines()
    assert output[0] == "42"
    with pytest.raises(ProcessLookupError):
        os.kill(int(output[1]), 0)
    assert get_notebook(path, client)["job"]["status"] == "complete"


def test_overlapping_tabs_keep_access_to_the_same_execution(setup_notebook):
    path, notebook, client, application = setup_notebook
    other_tab = application.test_client()
    other_tab.set_cookie("diff_session", client.get_cookie("diff_session").value)
    notebook.cells = [nbformat.v4.new_code_cell("print('shared session')")]
    response = start(path, client, notebook, request_id="originating-tab-launch")
    assert response.status_code == 202
    job = response.get_json()
    assert job["request_id"] == "originating-tab-launch"
    # An overlapping request from the older cookie can finish after launch.
    get_notebook(path, other_tab)
    client.set_cookie("diff_session", other_tab.get_cookie("diff_session").value)
    state = wait(client, job)
    assert state["status"] == "complete"
    assert state["request_id"] == "originating-tab-launch"
    assert other_tab.get(f"/api/notebook/jobs/{job['job_id']}").status_code == 200
    assert get_notebook(path, other_tab)["job"]["request_id"] == "originating-tab-launch"


@pytest.mark.parametrize("choice", ["default", "kernel:python3", "python"])
def test_selected_environment_and_fresh_kernel_each_run(setup_notebook, choice):
    path, notebook, client, _ = setup_notebook
    options = {"environment": choice}
    expected_prefix = sys.prefix
    if choice == "python":
        venv = path.parent / "selected-env"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)],
                       check=True, capture_output=True)
        options["python_path"] = str(venv)
        expected_prefix = str(venv)
    notebook.cells = [nbformat.v4.new_code_cell(
        "assert 'previous_run' not in globals()\nprevious_run = True\nimport sys\nprint(sys.prefix)"
    )]
    for _ in range(2):
        response = start(path, client, notebook, **options)
        assert response.status_code == 202
        state = wait(client, response.get_json())
        assert state["status"] == "complete", state.get("error")
        assert notebooks.read_notebook(path)[0]["cells"][0]["outputs"][0]["text"].strip() == expected_prefix


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_failed_cell_saves_partial_outputs_and_clears_old_results(setup_notebook, failure):
    path, notebook, client, _ = setup_notebook
    notebook.cells = [
        nbformat.v4.new_code_cell("print('first cell')"),
        nbformat.v4.new_code_cell("raise ValueError('test failure')" if failure == "error" else "import time; time.sleep(5)"),
        nbformat.v4.new_code_cell("print('never runs')", execution_count=50,
                                 metadata={"execution": {"iopub.status.idle": "old timestamp"}},
                                 outputs=[nbformat.v4.new_output("stream", name="stdout", text="stale output")]),
    ]
    response = start(path, client, notebook, timeout=1 if failure == "timeout" else 30)
    assert response.status_code == 202
    job = response.get_json()
    state = wait(client, job)
    assert state["status"] == "failed" and state["saved"]
    result = notebooks.read_notebook(path)[0]
    assert result["cells"][0]["outputs"][0]["text"] == "first cell\n"
    assert result["cells"][1]["outputs"][-1]["output_type"] == "error"
    assert result["cells"][2]["outputs"] == []
    assert result["cells"][2]["execution_count"] is None
    assert "execution" not in result["cells"][2]["metadata"]
    assert "execution" in result["cells"][0]["metadata"]
    assert client.get(f"/api/notebook/jobs/{job['job_id']}/result").status_code == 200


def test_parallel_run_rejected_and_external_edits_preserved(setup_notebook):
    path, notebook, client, _ = setup_notebook
    notebook.cells = [nbformat.v4.new_code_cell("import time; time.sleep(2); print('executed')")]
    response = start(path, client, notebook)
    assert response.status_code == 202
    job = response.get_json()
    assert start(path, client, notebook).status_code == 409
    external = copy.deepcopy(notebook)
    external.metadata.external_change = "preserve me"
    path.write_bytes(notebooks.serialize_notebook(external))
    state = wait(client, job)
    assert state["status"] == "failed" and state["conflict"]
    assert not state["saved"] and state["result_available"]
    assert notebooks.read_notebook(path)[0]["metadata"]["external_change"] == "preserve me"
    download = client.get(f"/api/notebook/jobs/{job['job_id']}/result")
    assert download.status_code == 200
    assert json.loads(download.data)["cells"][0]["outputs"][0]["text"] == "executed\n"


def test_notebook_saved_with_sudo_preserves_owner_and_mode(setup_notebook):
    path, notebook, client, _ = setup_notebook
    subprocess.run(["sudo", "-n", "chown", "0:0", str(path)], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "600", str(path)], check=True, capture_output=True)
    try:
        current = get_notebook(path, client)
        notebook.cells[1].source = "print('privileged save')"
        response = client.post("/api/file", json={"path": str(path), "content": json.dumps(notebook), "revision": current["revision"]})
        assert response.status_code == 200
        assert path.stat().st_uid == 0 and path.stat().st_mode & 0o7777 == 0o600
        assert get_notebook(path, client)["notebook"]["cells"][1]["source"] == "print('privileged save')"
    finally:
        subprocess.run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(path)], check=True, capture_output=True)


def test_notebook_markdown_uses_existing_renderer_and_attachments(setup_notebook):
    path, _, client, _ = setup_notebook
    response = client.post("/api/notebook/markdown", json={
        "path": str(path), "source": "# Heading\n\n![drawing](attachment:shape.svg)\n\n<script>bad()</script>",
        "attachments": {"shape.svg": {"image/svg+xml": "<svg xmlns='http://www.w3.org/2000/svg'/>"}},
    })
    assert response.status_code == 200
    rendered = response.get_json()["html"]
    assert "<h1>Heading</h1>" in rendered
    assert "data:image/svg+xml," in rendered
    assert "<script>bad()" not in rendered


@pytest.fixture
def protected_notebook(setup_notebook):
    path, notebook, client, application = setup_notebook
    private = path.parent / "protected account"
    folder = private / "notebook folder"
    folder.mkdir(parents=True)
    path = folder / path.name
    path.write_bytes(notebooks.serialize_notebook(notebook))
    (folder / "input.txt").write_text("protected input")
    subprocess.run(["sudo", "-n", "chown", "-hR", "0:0", str(private)], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "750", str(private)], check=True, capture_output=True)
    # Match the reported case: the leaf directory/file allow writes, but an
    # ancestor prevents traversal by the editor's account.
    subprocess.run(["sudo", "-n", "chmod", "777", str(folder)], check=True, capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "666", str(path)], check=True, capture_output=True)
    try:
        yield path, notebook, client, application
    finally:
        subprocess.run(["sudo", "-n", "chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(private)],
                       check=True, capture_output=True)
        subprocess.run(["sudo", "-n", "chmod", "-R", "u+rwX", str(private)], check=True, capture_output=True)


def test_execute_inside_protected_ancestor_and_keep_jobs_accessible(protected_notebook):
    path, notebook, client, _ = protected_notebook
    notebook.cells = [nbformat.v4.new_code_cell(
        "import json, os\nfrom pathlib import Path\n"
        "Path('generated.txt').write_text('generated result')\n"
        "print(json.dumps({'uid': os.geteuid(), 'pid': os.getpid(), 'cwd': os.getcwd(), 'input': Path('input.txt').read_text()}))"
    )]
    for _ in range(2):
        response = start(path, client, notebook)
        assert response.status_code == 202
        job = response.get_json()
        assert job["elevated"]
        state = wait(client, job)
        assert state["status"] == "complete", state.get("error")
        assert state["saved"] and state["elevated"]
        result = get_notebook(path, client)["notebook"]
        output = json.loads(result["cells"][0]["outputs"][0]["text"])
        assert output["uid"] == 0
        assert output["cwd"] == str(path.parent)
        assert output["input"] == "protected input"
        with pytest.raises(ProcessLookupError):
            os.kill(output["pid"], 0)
        assert client.get(f"/api/notebook/jobs/{job['job_id']}/result").status_code == 200
        with client.session_transaction() as session:
            scope = editor.hashlib.sha256(("notebook:" + session["csrf_token"]).encode()).hexdigest()[:32]
        directory = notebooks.job_directory(scope, job["job_id"])
        assert (directory / "status.json").stat().st_uid == os.getuid()
        assert (directory / "result.ipynb").stat().st_uid == os.getuid()


def test_elevated_run_lock_timeout_and_kernel_cleanup(protected_notebook):
    path, notebook, client, _ = protected_notebook
    notebook.cells = [nbformat.v4.new_code_cell("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(30)")]
    response = start(path, client, notebook, timeout=1)
    assert response.status_code == 202
    job = response.get_json()
    assert start(path, client, notebook, timeout=1).status_code == 409
    state = wait(client, job)
    assert state["status"] == "failed" and state["saved"] and state["elevated"]
    result = get_notebook(path, client)["notebook"]
    output = result["cells"][0]["outputs"]
    assert output[-1]["ename"] == "CellTimeoutError"
    with pytest.raises(ProcessLookupError):
        os.kill(int(output[0]["text"].strip()), 0)


@pytest.mark.parametrize("choice", ["python", "kernel"])
def test_environment_inside_protected_ancestor(protected_notebook, monkeypatch, choice):
    path, notebook, client, _ = protected_notebook
    # Build a real virtualenv without downloading anything.
    venv = path.parent / "private env"
    subprocess.run(["sudo", "-n", sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)],
                   check=True, capture_output=True)
    if choice == "python":
        options = {"environment": "python", "python_path": str(venv)}
    else:
        kernel_root = path.parents[2] / "kernels"
        kernel = kernel_root / "protected-python"
        kernel.mkdir(parents=True)
        (kernel / "kernel.json").write_text(json.dumps({
            "argv": [str(venv / "bin/python"), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
            "language": "python", "display_name": "Protected Python", "env": {"NOTEBOOK_TEST_ENV": "preserved"},
        }))
        monkeypatch.setenv("JUPYTER_PATH", str(kernel_root.parent))
        # KernelSpecManager expects JUPYTER_PATH/kernels/<name>.
        options = {"environment": "kernel:protected-python"}
    notebook.cells = [nbformat.v4.new_code_cell("import sys, os\nprint(sys.prefix)\nprint(os.environ.get('NOTEBOOK_TEST_ENV', ''))")]
    response = start(path, client, notebook, **options)
    assert response.status_code == 202
    state = wait(client, response.get_json())
    assert state["status"] == "complete", state.get("error")
    lines = get_notebook(path, client)["notebook"]["cells"][0]["outputs"][0]["text"].splitlines()
    assert lines[0] == str(venv)
    if choice == "kernel":
        assert lines[1] == "preserved"


def test_accessible_notebook_execution_stays_unprivileged(setup_notebook):
    path, notebook, client, _ = setup_notebook
    notebook.cells = [nbformat.v4.new_code_cell("import os; print(os.geteuid())")]
    response = start(path, client, notebook)
    assert response.status_code == 202
    state = wait(client, response.get_json())
    assert state["status"] == "complete" and not state["elevated"]
    assert get_notebook(path, client)["notebook"]["cells"][0]["outputs"][0]["text"].strip() == str(os.geteuid())
