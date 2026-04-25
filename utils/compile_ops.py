"""
Compile planning, tooling checks, and execution helpers.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from utils.file_ops import is_likely_binary, is_writable_by_user, read_file_bytes, read_file_head


COMPILE_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".cs": "csharp",
}

COMPILE_LANGUAGE_CONFIGS: dict[str, dict[str, object]] = {
    "c": {
        "label": "C",
        "default_extension": "",
        "supports_optimization": True,
        "optimization_label": "Optimize (-O2)",
        "supports_warnings": True,
        "warning_label": "Extra warnings (-Wall -Wextra)",
        "supports_cross_compile": True,
    },
    "cpp": {
        "label": "C++",
        "default_extension": "",
        "supports_optimization": True,
        "optimization_label": "Optimize (-O2)",
        "supports_warnings": True,
        "warning_label": "Extra warnings (-Wall -Wextra)",
        "supports_cross_compile": True,
    },
    "go": {
        "label": "Go",
        "default_extension": "",
        "supports_optimization": False,
        "optimization_label": None,
        "supports_warnings": False,
        "warning_label": None,
        "supports_cross_compile": True,
    },
    "java": {
        "label": "Java",
        "default_extension": ".jar",
        "supports_optimization": False,
        "optimization_label": None,
        "supports_warnings": True,
        "warning_label": "Lint warnings (-Xlint)",
        "supports_cross_compile": False,
    },
    "rust": {
        "label": "Rust",
        "default_extension": "",
        "supports_optimization": True,
        "optimization_label": "Optimize (-O)",
        "supports_warnings": False,
        "warning_label": None,
        "supports_cross_compile": True,
    },
    "csharp": {
        "label": "C#",
        "default_extension": ".exe",
        "supports_optimization": True,
        "optimization_label": "Optimize (-optimize+)",
        "supports_warnings": True,
        "warning_label": "Higher warning level (-warn:4)",
        "supports_cross_compile": False,
    },
}

GO_PACKAGE_PATTERN = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\b", re.MULTILINE)
JAVA_PACKAGE_PATTERN = re.compile(
    r"^\s*package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*;",
    re.MULTILINE,
)
JAVA_MAIN_METHOD_PATTERN = re.compile(r"\bstatic\s+void\s+main\s*\(")

COMPILE_TOOLING_CACHE: dict[str, dict[str, object]] = {}
COMPILE_TOOLING_CACHE_LOCK = threading.Lock()


def _default_output_basename(path: Path) -> str:
    stem = path.stem.strip()
    if stem:
        return stem
    name = path.name.lstrip(".").strip()
    return name or "output"


def get_compile_config_for_path(path: Path) -> dict[str, object] | None:
    """Return compile metadata for source files supported by the browser compiler."""
    language = COMPILE_LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
    if not language:
        return None

    language_config = COMPILE_LANGUAGE_CONFIGS.get(language)
    if not language_config:
        return None

    default_extension = str(language_config.get("default_extension") or "")
    default_name = _default_output_basename(path) + default_extension

    return {
        "language": language,
        "label": language_config["label"],
        "default_directory": str(path.parent),
        "default_name": default_name,
        "supports_optimization": bool(language_config.get("supports_optimization")),
        "optimization_label": language_config.get("optimization_label"),
        "supports_warnings": bool(language_config.get("supports_warnings")),
        "warning_label": language_config.get("warning_label"),
    }


def _read_compile_source_text(path: Path, *, max_bytes: int | None = None) -> tuple[str | None, str | None]:
    """Read compile-target source text with the same sudo-aware file access as the editor."""
    if max_bytes is None:
        success, data = read_file_bytes(path)
    else:
        success, data = read_file_head(path, max_bytes=max_bytes)

    if not success:
        return None, str(data)

    content_bytes = data if isinstance(data, bytes) else str(data).encode("utf-8", errors="replace")
    if is_likely_binary(content_bytes):
        return None, "Source file does not look like text"

    return content_bytes.decode("utf-8", errors="replace"), None


def _extract_go_package_name(source_text: str) -> str | None:
    match = GO_PACKAGE_PATTERN.search(source_text or "")
    return match.group(1) if match else None


def _extract_java_package_name(source_text: str) -> str | None:
    match = JAVA_PACKAGE_PATTERN.search(source_text or "")
    return match.group(1) if match else None


def _build_go_compile_context(path: Path, source_text: str) -> tuple[dict[str, object] | None, str | None, int]:
    package_name = _extract_go_package_name(source_text)
    if not package_name:
        return None, "Unable to detect the Go package declaration.", 400
    if package_name != "main":
        return None, "Go browser compilation currently supports only package main files.", 400

    input_paths: list[str] = []
    for sibling in sorted(path.parent.glob("*.go")):
        if not sibling.is_file() or sibling.name.endswith("_test.go"):
            continue

        sibling_text, error = _read_compile_source_text(sibling, max_bytes=8192)
        if error:
            return None, f"Failed to inspect sibling Go file {sibling.name}: {error}", 500

        if _extract_go_package_name(sibling_text or "") == package_name:
            input_paths.append(str(sibling))

    if not input_paths:
        input_paths.append(str(path))

    source_count = len(input_paths)
    file_label = "file" if source_count == 1 else "files"
    return {
        "go_input_paths": input_paths,
        "artifact_note": f"Builds package main using {source_count} Go {file_label} from this directory.",
    }, None, 200


def _build_java_compile_context(path: Path, source_text: str) -> dict[str, object]:
    package_name = _extract_java_package_name(source_text)
    has_main_method = bool(JAVA_MAIN_METHOD_PATTERN.search(source_text or ""))
    main_class = None
    artifact_note = "Produces a JAR of compiled classes without a Main-Class manifest."

    if has_main_method:
        main_class = f"{package_name}.{path.stem}" if package_name else path.stem
        artifact_note = f"Produces a runnable JAR with Main-Class {main_class}."

    return {
        "java_package": package_name,
        "java_main_class": main_class,
        "artifact_note": artifact_note,
    }


def get_compile_context_for_path(path: Path) -> tuple[dict[str, object] | None, str | None, int]:
    """Return compile metadata plus language-specific compile planning details."""
    if path.suffix.lower() == ".csx":
        return None, "C# script files (.csx) are not supported by browser compilation.", 400

    compile_config = get_compile_config_for_path(path)
    if not compile_config:
        return None, "This file type is not compilable from the browser", 400

    compile_context = dict(compile_config)
    language = str(compile_context["language"])

    if language == "go":
        source_text, error = _read_compile_source_text(path)
        if error:
            return None, error, 500

        go_context, go_error, go_status = _build_go_compile_context(path, source_text or "")
        if go_error:
            return None, go_error, go_status
        compile_context.update(go_context or {})
    elif language == "java":
        source_text, error = _read_compile_source_text(path)
        if error:
            return None, error, 500
        compile_context.update(_build_java_compile_context(path, source_text or ""))

    return compile_context, None, 200


def build_compile_success_message(
    source_path: Path,
    target_path: Path,
    compile_context: dict[str, object],
) -> str:
    """Return a user-facing success message for a completed compile action."""
    language = str(compile_context.get("language") or "")
    if language == "java":
        if compile_context.get("java_main_class"):
            return f"Compiled {source_path.name} to runnable JAR {target_path.name}"
        return f"Compiled {source_path.name} to JAR {target_path.name} (no Main-Class manifest)"
    return f"Compiled {source_path.name} to {target_path.name}"


def command_exists(command: str) -> bool:
    """Return whether a command is currently available on PATH."""
    return shutil.which(command) is not None


def find_first_command(*commands: str) -> str | None:
    """Return the first command found on PATH from the provided candidates."""
    for command in commands:
        if command_exists(command):
            return command
    return None


def has_dotnet_sdk() -> bool:
    """Check for a usable dotnet SDK, not just the runtime host."""
    if not command_exists("dotnet"):
        return False

    try:
        result = subprocess.run(
            ["dotnet", "--list-sdks"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    return bool(result.stdout.strip())


def _detect_c_cross_targets() -> list[dict[str, str]]:
    """Scan PATH for C cross-compilers matching *-gcc or *-cc."""
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for bindir in os.environ.get("PATH", "").split(os.pathsep):
        if not bindir or not os.path.isdir(bindir):
            continue
        try:
            entries = os.listdir(bindir)
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(("-gcc", "-cc")):
                continue
            full = os.path.join(bindir, entry)
            if not os.path.isfile(full) or not os.access(full, os.X_OK):
                continue
            prefix = entry.rsplit("-", 1)[0]
            if prefix and prefix not in seen and not re.fullmatch(r"c\d{2}", prefix):
                seen.add(prefix)
                targets.append({"value": prefix, "label": prefix})
    return sorted(targets, key=lambda t: t["value"])


def _detect_cpp_cross_targets() -> list[dict[str, str]]:
    """Scan PATH for C++ cross-compilers matching *-g++ or *-c++."""
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for bindir in os.environ.get("PATH", "").split(os.pathsep):
        if not bindir or not os.path.isdir(bindir):
            continue
        try:
            entries = os.listdir(bindir)
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(("-g++", "-c++")):
                continue
            full = os.path.join(bindir, entry)
            if not os.path.isfile(full) or not os.access(full, os.X_OK):
                continue
            prefix = entry.rsplit("-", 1)[0]
            if prefix and prefix not in seen:
                seen.add(prefix)
                targets.append({"value": prefix, "label": prefix})
    return sorted(targets, key=lambda t: t["value"])


def _detect_go_targets() -> list[dict[str, str]]:
    """Return all GOOS/GOARCH pairs supported by the installed Go toolchain."""
    if not command_exists("go"):
        return []
    try:
        result = subprocess.run(
            ["go", "tool", "dist", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    targets: list[dict[str, str]] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line and "/" in line:
            targets.append({"value": line, "label": line})
    return targets


def _detect_rust_targets() -> list[dict[str, str]]:
    """Return installed Rust target triples."""
    if command_exists("rustup"):
        try:
            result = subprocess.run(
                ["rustup", "target", "list", "--installed"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            targets: list[dict[str, str]] = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line:
                    targets.append({"value": line, "label": line})
            return targets
        except (OSError, subprocess.SubprocessError):
            pass
    if command_exists("rustc"):
        try:
            result = subprocess.run(
                ["rustc", "--print", "target-list"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            targets = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line:
                    targets.append({"value": line, "label": line})
            return targets
        except (OSError, subprocess.SubprocessError):
            pass
    return []


def compute_compile_tooling_status(language: str) -> tuple[dict[str, object], int]:
    """Return compile-tooling availability for browser-compilable languages."""
    normalized = (language or "").strip().lower()
    if not normalized:
        return {"error": "No language specified"}, 400

    if normalized == "c":
        compiler = find_first_command("gcc", "cc")
        if compiler:
            return {
                "available": True,
                "compiler": compiler,
                "supports_cross_compile": True,
                "cross_compile_targets": _detect_c_cross_targets(),
            }, 200
        return {
            "available": False,
            "error": "C compilation requires gcc.",
            "install_command": "sudo apt update && sudo apt install gcc",
        }, 200

    if normalized == "cpp":
        compiler = find_first_command("g++", "c++")
        if compiler:
            return {
                "available": True,
                "compiler": compiler,
                "supports_cross_compile": True,
                "cross_compile_targets": _detect_cpp_cross_targets(),
            }, 200
        return {
            "available": False,
            "error": "C++ compilation requires g++.",
            "install_command": "sudo apt update && sudo apt install g++",
        }, 200

    if normalized == "go":
        if command_exists("go"):
            return {
                "available": True,
                "compiler": "go",
                "supports_cross_compile": True,
                "cross_compile_targets": _detect_go_targets(),
            }, 200
        return {
            "available": False,
            "error": "Go compilation requires the Go toolchain.",
            "install_command": "sudo apt update && sudo apt install golang-go",
        }, 200

    if normalized == "java":
        javac = find_first_command("javac")
        jar = find_first_command("jar")
        if javac and jar:
            return {
                "available": True,
                "compiler": javac,
                "archiver": jar,
                "supports_cross_compile": False,
                "cross_compile_targets": [],
            }, 200
        return {
            "available": False,
            "error": "Java compilation requires javac and jar from a JDK.",
            "install_command": "sudo apt update && sudo apt install default-jdk",
        }, 200

    if normalized == "rust":
        if command_exists("rustc"):
            return {
                "available": True,
                "compiler": "rustc",
                "supports_cross_compile": True,
                "cross_compile_targets": _detect_rust_targets(),
            }, 200
        return {
            "available": False,
            "error": "Rust compilation requires rustc.",
            "install_command": "sudo apt update && sudo apt install rustc cargo",
        }, 200

    if normalized == "csharp":
        compiler = find_first_command("csc", "mono-csc", "cli-csc", "mcs")
        if compiler:
            return {
                "available": True,
                "compiler": compiler,
                "supports_cross_compile": False,
                "cross_compile_targets": [],
            }, 200
        return {
            "available": False,
            "error": "C# compilation requires csc or Mono's mcs.",
            "install_command": "sudo apt update && sudo apt install mono-mcs mono-devel",
        }, 200

    return {"error": "Unsupported language"}, 400


def get_compile_tooling_status(language: str) -> tuple[dict[str, object], int]:
    """
    Return compile-tooling availability for browser-compilable languages.

    Successful detections are cached in-process until app restart. Missing-tool
    results are recomputed so newly installed compilers become visible without a
    worker restart.
    """
    normalized = (language or "").strip().lower()
    if not normalized:
        return {"error": "No language specified"}, 400

    with COMPILE_TOOLING_CACHE_LOCK:
        cached = COMPILE_TOOLING_CACHE.get(normalized)
    if cached is not None:
        return dict(cached), 200

    status, http_status = compute_compile_tooling_status(normalized)
    if http_status == 200 and status.get("available") is True:
        with COMPILE_TOOLING_CACHE_LOCK:
            COMPILE_TOOLING_CACHE[normalized] = dict(status)
    return status, http_status


def _combine_subprocess_output(*chunks: str | None) -> str:
    """Join stdout/stderr fragments into a single readable message."""
    return "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())


def _get_compile_input_paths(source_path: Path, compile_context: dict[str, object]) -> list[Path]:
    """Return the source paths a compile action reads from disk."""
    language = str(compile_context.get("language") or "")
    if language == "go":
        return [
            Path(str(path))
            for path in (compile_context.get("go_input_paths") or [])
            if str(path).strip()
        ] or [source_path]
    return [source_path]


def _compile_requires_sudo(
    source_path: Path,
    target_path: Path,
    compile_context: dict[str, object],
) -> bool:
    """Use sudo when compile inputs or the output location need elevated access."""
    input_paths = _get_compile_input_paths(source_path, compile_context)
    if any(not os.access(path, os.R_OK) for path in input_paths):
        return True
    return not is_writable_by_user(target_path)


def _run_compile_command(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    use_sudo: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a compile command, optionally through passwordless sudo."""
    if use_sudo and env:
        env_cmd = ["env", *[f"{key}={value}" for key, value in env.items()]]
        run_cmd = ["sudo", "-n", *env_cmd, *cmd]
    elif use_sudo:
        run_cmd = ["sudo", "-n", *cmd]
    else:
        run_cmd = cmd
    run_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if env is not None:
        run_kwargs["env"] = env
    if use_sudo:
        run_kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(run_cmd, **run_kwargs)


def _prepare_sudo_tempdir_for_cleanup(path: Path) -> None:
    """Relax sudo-created temp files so TemporaryDirectory cleanup can remove them."""
    try:
        subprocess.run(
            ["sudo", "-n", "chmod", "-R", "a+rwX", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def compile_source_file(
    source_path: Path,
    target_path: Path,
    compile_context: dict[str, object],
    tooling_status: dict[str, object],
    *,
    optimize: bool = False,
    warnings: bool = False,
    cross_compile_target: str | None = None,
) -> tuple[bool, str]:
    """Compile a source file to the requested output path."""
    language = str(compile_context.get("language") or "")
    compiler = str(tooling_status.get("compiler") or "")
    timeout = 300
    use_sudo = _compile_requires_sudo(source_path, target_path, compile_context)
    cross = (cross_compile_target or "").strip()

    try:
        if language == "java":
            archiver = str(tooling_status.get("archiver") or "")
            main_class = str(compile_context.get("java_main_class") or "")
            if not compiler or not archiver:
                return False, "Java compilation requires javac and jar."

            temp_dir_obj = tempfile.TemporaryDirectory(prefix="diff-editor-java-")
            temp_dir = Path(temp_dir_obj.name)
            try:
                classes_dir = temp_dir / "classes"
                classes_dir.mkdir(parents=True, exist_ok=True)

                javac_cmd = [compiler]
                if warnings:
                    javac_cmd.append("-Xlint")
                javac_cmd.extend(["-d", str(classes_dir), str(source_path)])

                javac_result = _run_compile_command(
                    javac_cmd,
                    cwd=source_path.parent,
                    timeout=timeout,
                    use_sudo=use_sudo,
                )
                javac_output = _combine_subprocess_output(javac_result.stdout, javac_result.stderr)
                if javac_result.returncode != 0:
                    return False, javac_output or "javac failed"

                if not any(classes_dir.rglob("*.class")):
                    return False, "javac completed without producing class files"

                jar_cmd = [archiver, "--create", "--file", str(target_path)]
                if main_class:
                    jar_cmd.extend(["--main-class", main_class])
                jar_cmd.extend(["-C", str(classes_dir), "."])
                jar_result = _run_compile_command(
                    jar_cmd,
                    cwd=source_path.parent,
                    timeout=timeout,
                    use_sudo=use_sudo,
                )
                jar_output = _combine_subprocess_output(javac_output, jar_result.stdout, jar_result.stderr)
                if jar_result.returncode != 0:
                    return False, jar_output or "jar failed"
                return True, jar_output
            finally:
                if use_sudo:
                    _prepare_sudo_tempdir_for_cleanup(temp_dir)
                temp_dir_obj.cleanup()

        env: dict[str, str] | None = None

        if language == "c":
            if cross:
                cmd = [f"{cross}-gcc"]
                if not command_exists(cmd[0]):
                    cmd = [f"{cross}-cc"]
            else:
                cmd = [compiler or "gcc"]
            if optimize:
                cmd.append("-O2")
            if warnings:
                cmd.extend(["-Wall", "-Wextra"])
            cmd.extend([str(source_path), "-o", str(target_path)])
        elif language == "cpp":
            if cross:
                cmd = [f"{cross}-g++"]
                if not command_exists(cmd[0]):
                    cmd = [f"{cross}-c++"]
            else:
                cmd = [compiler or "g++"]
            if optimize:
                cmd.append("-O2")
            if warnings:
                cmd.extend(["-Wall", "-Wextra"])
            cmd.extend([str(source_path), "-o", str(target_path)])
        elif language == "go":
            go_input_paths = [
                str(p)
                for p in (compile_context.get("go_input_paths") or [str(source_path)])
                if str(p).strip()
            ]
            cmd = [compiler or "go", "build", "-o", str(target_path), *go_input_paths]
            if cross:
                parts = cross.split("/")
                if len(parts) == 2:
                    env = dict(os.environ)
                    env["GOOS"] = parts[0]
                    env["GOARCH"] = parts[1]
                else:
                    return False, f"Invalid Go cross-compile target: {cross}"
        elif language == "rust":
            cmd = [compiler or "rustc"]
            if optimize:
                cmd.append("-O")
            if cross:
                cmd.extend(["--target", cross])
            cmd.extend([str(source_path), "-o", str(target_path)])
        elif language == "csharp":
            cmd = [compiler or "csc"]
            if optimize:
                cmd.append("-optimize+")
            if warnings:
                cmd.append("-warn:4")
            cmd.extend([f"-out:{target_path}", str(source_path)])
        else:
            return False, "Unsupported language"

        result = _run_compile_command(
            cmd,
            cwd=source_path.parent,
            timeout=timeout,
            use_sudo=use_sudo,
            env=env,
        )
        output = _combine_subprocess_output(result.stdout, result.stderr)
        if result.returncode != 0:
            return False, output or "Compilation failed"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Compile operation timed out"
    except OSError as e:
        return False, str(e)
