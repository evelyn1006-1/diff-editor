"""Notebook round trips and one-shot execution jobs shared by web workers."""

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from stat import S_ISDIR
import subprocess
import sys
import tempfile
import threading
import time

import nbformat
from jupyter_client import AsyncKernelManager
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError, CellTimeoutError

from app_runtime import notebook_size_limit
from utils.file_ops import (
    EDITOR_GID,
    EDITOR_UID,
    FileConflict as NotebookConflict,
    check_path_is_directory,
    check_path_is_file,
    file_revision as revision,
    read_file_bytes,
    require_file_revision as _require_revision,
    resolve_path_with_sudo,
    write_file_bytes,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# sudo supplies the original account IDs. Elevated workers share that account's
# private job store instead of creating a separate root-owned execution history.
JOB_ROOT = Path(os.environ.get("NOTEBOOK_JOB_ROOT", str(Path(tempfile.gettempdir()) / f"diff-editor-notebooks-{EDITOR_UID}")))
ACTIVE_STATES = {"queued", "running"}


def validate_notebook(notebook: object) -> dict:
    if not isinstance(notebook, dict) or notebook.get("nbformat") != 4:
        raise ValueError("Only Jupyter notebook format 4 is supported")
    try:
        # Unlike validate(), iter_validate never repairs or mutates the input.
        error = next(nbformat.validator.iter_validate(notebook), None)
        if error is not None:
            location = "/" + "/".join(map(str, error.absolute_path))
            raise ValueError(f"Invalid notebook at {location}: {error.validator} validation failed")
        ids = [cell["id"] for cell in notebook["cells"] if "id" in cell]
        if len(set(ids)) != len(ids):
            raise ValueError("Notebook cell IDs must be unique")
        json.dumps(notebook, allow_nan=False)
    except (nbformat.ValidationError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid notebook: {getattr(error, 'message', str(error))}") from None
    return notebook


def parse_notebook(content: bytes | str) -> dict:
    if not content.strip():
        return dict(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell()]))
    try:
        notebook = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid notebook JSON: {error}") from None
    return validate_notebook(notebook)


def serialize_notebook(notebook: dict) -> bytes:
    validate_notebook(notebook)
    return (json.dumps(notebook, ensure_ascii=False, allow_nan=False, indent=1) + "\n").encode("utf-8")


def read_notebook(path: Path) -> tuple[dict, str]:
    ok, content = read_file_bytes(path)
    if not ok:
        raise OSError(content)
    return parse_notebook(content), revision(content)


def _root() -> Path:
    JOB_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = JOB_ROOT.lstat()
    if not S_ISDIR(info.st_mode) or info.st_uid != EDITOR_UID:
        raise OSError("Notebook job directory must be a real directory owned by the editor account")
    if info.st_mode & 0o077:
        JOB_ROOT.chmod(0o700)
    return JOB_ROOT


def _path_key(path: Path) -> str:
    return hashlib.sha256(os.fsencode(resolve_path_with_sudo(path))).hexdigest()


def save_notebook(path: Path, notebook: dict, expected_revision: str, max_size: int) -> str:
    content = serialize_notebook(notebook)
    if len(content) > max_size:
        raise ValueError("Notebook exceeds the size limit")
    ok, message = write_file_bytes(path, content, expected_revision=expected_revision)
    if not ok:
        raise OSError(message)
    return revision(content)


def environments() -> list[dict]:
    choices = [{"id": "default", "label": f"Default Python ({sys.version_info.major}.{sys.version_info.minor})"}]
    for name, item in sorted(KernelSpecManager().get_all_specs().items()):
        spec = item["spec"]
        if spec.get("language", "").lower() == "python":
            choices.append({"id": f"kernel:{name}", "label": spec.get("display_name", name)})
    return choices


def _sudo_command(command: list[str]) -> list[str]:
    # Keep registered-kernel variables and explicitly selected environments the
    # same as for an ordinary run. No shell interprets any argument.
    return ["sudo", "-n", "-E", "--", *command]


def _execution_requires_sudo(path: Path, environment: dict) -> bool:
    if os.geteuid() == 0:
        return False
    executable = environment["argv"][0]
    executable = shutil.which(executable, path=environment.get("env", {}).get("PATH")) or executable
    return not (os.access(path.parent, os.R_OK | os.W_OK | os.X_OK)
                and os.access(path, os.R_OK | os.W_OK)
                and os.access(executable, os.X_OK))


def resolve_environment(choice: str, python_path: str = "") -> dict:
    if choice == "default":
        return {"argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                "display_name": "Default Python", "language": "python"}
    if choice == "python":
        if not isinstance(python_path, str) or not python_path.strip() or "\x00" in python_path:
            raise ValueError("Specify a Python executable or virtualenv directory")
        path = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(python_path.strip()))))
        if check_path_is_directory(path)[0]:
            path /= "bin/python"
        if not check_path_is_file(path)[0]:
            raise ValueError("The selected Python executable is missing or not executable")
        command = [str(path), "-c", "import ipykernel"]
        if not os.access(path, os.X_OK):
            executable = subprocess.run(_sudo_command(["test", "-x", str(path)]),
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            if executable.returncode:
                raise ValueError("The selected Python executable is missing or not executable")
            command = _sudo_command(command)
        check = subprocess.run(command, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=15)
        if check.returncode:
            raise ValueError("The selected environment needs ipykernel installed (python -m pip install ipykernel)")
        return {"argv": [str(path), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                "display_name": f"Python: {path}", "language": "python"}
    if isinstance(choice, str) and choice.startswith("kernel:"):
        try:
            kernel_spec = KernelSpecManager().get_kernel_spec(choice[7:])
            spec = kernel_spec.to_dict()
            spec["argv"] = [argument.replace("{resource_dir}", kernel_spec.resource_dir) for argument in spec["argv"]]
            spec["env"] = {key: value.replace("{resource_dir}", kernel_spec.resource_dir)
                           for key, value in spec.get("env", {}).items()}
        except Exception as error:
            raise ValueError(f"Cannot load the selected kernel: {error}") from None
        if spec.get("language", "").lower() != "python":
            raise ValueError("Select a Python kernel")
        return spec
    raise ValueError("Unknown Python environment")


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    if os.geteuid() == 0:
        os.chown(temporary, EDITOR_UID, EDITOR_GID)
    temporary.replace(path)


def job_directory(scope: str, job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", scope) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid notebook job")
    return _root() / scope / job_id


def job_status(scope: str, job_id: str) -> dict | None:
    directory = job_directory(scope, job_id)
    try:
        state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not state.get("pid"):
        try:
            state["pid"] = int((directory / "pid").read_text())
        except FileNotFoundError:
            pass
    if state["status"] in ACTIVE_STATES and state.get("pid"):
        try:
            os.kill(state["pid"], 0)
        except PermissionError:
            pass  # An elevated worker exists, but is owned by root.
        except ProcessLookupError:
            state.update(status="failed", error="The execution process stopped unexpectedly. It has not been restarted.")
    return {**state, "job_id": job_id, "result_available": (directory / "result.ipynb").is_file()}


def latest_job(scope: str, path: Path) -> dict | None:
    try:
        job_id = (_root() / scope / f"{_path_key(path)}.latest").read_text(encoding="utf-8")
        return job_status(scope, job_id)
    except FileNotFoundError:
        return None


def start_execution(scope: str, path: Path, notebook: dict, expected_revision: str,
                    environment: dict, timeout: int, max_size: int, request_id: str | None = None) -> dict:
    validate_notebook(notebook)
    _require_revision(expected_revision)
    if request_id is not None and (not isinstance(request_id, str)
                                   or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)):
        raise ValueError("Invalid notebook execution request ID")
    # Verify the revision before launching any stateful execution.
    _, current_revision = read_notebook(path)
    if current_revision != expected_revision:
        raise NotebookConflict("The notebook changed on disk. Reload it before executing; your edits are still here.")
    key = _path_key(path)
    use_sudo = _execution_requires_sudo(path, environment)
    if use_sudo:
        permission = subprocess.run(_sudo_command(["true"]), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5)
        if permission.returncode:
            raise ValueError(f"Notebook execution requires passwordless sudo: {permission.stderr.strip()}")
    lock = (_root() / f"{key}.run.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NotebookConflict("This notebook already has an execution in progress") from None
        # Preserve the current edits before starting a possibly long-running job.
        expected_revision = save_notebook(path, notebook, expected_revision, max_size)
        job_id = secrets.token_hex(16)
        directory = job_directory(scope, job_id)
        directory.mkdir(mode=0o700, parents=True)
        _atomic_json(directory / "request.json", {
            "path": str(path), "notebook": notebook, "revision": expected_revision,
            "environment": environment, "timeout": timeout, "max_size": max_size, "request_id": request_id,
        })
        _atomic_json(directory / "status.json", {"status": "queued", "path": str(path), "saved": False,
                                                "input_revision": expected_revision, "elevated": use_sudo,
                                                "request_id": request_id})
        command = [sys.executable, "-m", "utils.notebook_ops", "--job", str(directory), "--lock-fd", "0"]
        if use_sudo:
            command = _sudo_command(command)
        with (directory / "worker.log").open("ab") as log:
            process = subprocess.Popen(
                command, cwd=PROJECT_ROOT,
                # sudo closes extra descriptors, but preserves standard input.
                # Carry the same flock on fd 0 through both launch paths.
                stdin=lock, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        (directory / "pid").write_text(str(process.pid))
        # The child inherits the same flock; closing our descriptor must not unlock it.
        latest = _root() / scope / f"{key}.latest"
        temporary = latest.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(job_id, encoding="utf-8")
        temporary.replace(latest)
        threading.Thread(target=process.wait, daemon=True).start()
        return job_status(scope, job_id)
    finally:
        lock.close()


def execute_job(directory: Path, lock_fd: int) -> None:
    request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    # A detached worker can load newer code than the web process that queued
    # its request. Older requests predate the per-job size limit.
    max_size = request["max_size"] if "max_size" in request else notebook_size_limit()
    path = Path(request["path"])
    # Real .ipynb files commonly store source and MIME text as arrays of lines.
    # The nbformat reader rejoins these into the strings nbclient executes.
    notebook = nbformat.reads(json.dumps(request["notebook"]), as_version=nbformat.NO_CONVERT)
    state = {"status": "running", "pid": os.getpid(), "path": str(path), "saved": False,
             "cell": None, "total": len(notebook.cells), "started_at": time.time(),
             "input_revision": request["revision"], "elevated": os.geteuid() == 0,
             "request_id": request.get("request_id")}
    _atomic_json(directory / "status.json", state)

    def cell_started(cell, cell_index, **kwargs):
        state["cell"] = cell_index + 1
        _atomic_json(directory / "status.json", state)

    cleared = False

    def prepare_cells(**kwargs):
        nonlocal cleared
        if not cleared:
            for cell in notebook.cells:
                if cell.cell_type == "code":
                    cell.outputs = []
                    cell.execution_count = None
                    cell.metadata.pop("execution", None)
            cleared = True

    error = None
    try:
        with tempfile.TemporaryDirectory(prefix="kernel-", dir=directory) as temporary:
            kernel_dir = Path(temporary) / "notebook-python"
            kernel_dir.mkdir()
            _atomic_json(kernel_dir / "kernel.json", request["environment"])
            async def run_kernel():
                manager = AsyncKernelManager(
                    kernel_name="notebook-python", connection_file=str(Path(temporary) / "connection.json"),
                    kernel_spec_manager=KernelSpecManager(kernel_dirs=[temporary], ensure_native_kernel=False),
                )
                client = NotebookClient(notebook, km=manager, timeout=request["timeout"] or None,
                                        allow_errors=False, force_raise_errors=True, allow_error_names=[],
                                        skip_cells_with_tag="", ipython_hist_file=":memory:",
                                        on_cell_start=prepare_cells, on_cell_execute=cell_started, record_timing=True)
                try:
                    await client.async_execute(cwd=str(path.parent), cleanup_kc=True)
                finally:
                    # Also cover failures during kernel startup, before the
                    # client's execution context can enter its cleanup block.
                    if client.kc is not None:
                        client.kc.stop_channels()
                    if manager.has_kernel:
                        await manager.shutdown_kernel(now=True)

            asyncio.run(run_kernel())
    except (CellExecutionError, CellTimeoutError) as exception:
        if isinstance(exception, CellTimeoutError) and state["cell"]:
            notebook.cells[state["cell"] - 1].outputs.append(nbformat.v4.new_output(
                "error", ename="CellTimeoutError", evalue=f"Cell exceeded {request['timeout']} seconds",
                traceback=[f"CellTimeoutError: Cell exceeded {request['timeout']} seconds"],
            ))
        # The actual cell traceback stays in the notebook, never in job logs/status.
        error = f"Execution stopped at cell {state['cell']}: {type(exception).__name__}. See the cell output."
    except Exception as exception:
        error = f"Execution failed: {type(exception).__name__}: {exception}"
    try:
        result = serialize_notebook(notebook)
        temporary_result = directory / "result.ipynb.tmp"
        temporary_result.write_bytes(result)
        if os.geteuid() == 0:
            os.chown(temporary_result, EDITOR_UID, EDITOR_GID)
        temporary_result.replace(directory / "result.ipynb")
        state["result_available"] = True
        try:
            state["revision"] = save_notebook(path, notebook, request["revision"], max_size)
            state["saved"] = True
        except NotebookConflict:
            state["conflict"] = True
            error = "The file changed during execution. The executed result is available to download; the newer file was preserved."
        except (OSError, ValueError) as exception:
            error = f"The executed result could not be saved: {exception}. Download it below."
    except Exception as exception:
        error = f"Could not preserve execution results: {exception}"
    state.update(status="failed" if error else "complete", error=error, finished_at=time.time())
    # sudo's monitor can keep another copy of stdin open after this worker
    # finishes. Release the shared flock before announcing a terminal status.
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    _atomic_json(directory / "status.json", state)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    arguments = parser.parse_args()
    try:
        execute_job(arguments.job, arguments.lock_fd)
    finally:
        fcntl.flock(arguments.lock_fd, fcntl.LOCK_UN)
        os.close(arguments.lock_fd)
