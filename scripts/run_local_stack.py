"""Lanza API, un único worker y el cliente web con estado local compartido.

El supervisor es deliberadamente de desarrollo: no reemplaza un gestor de
procesos de producción. No imprime el entorno ni intenta liberar puertos
ocupados por procesos ajenos.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = PROJECT_ROOT / "outputs" / "local-stack"
API_PORT = 8000
WEB_PORT = 5173


class LocalStackError(RuntimeError):
    """Error operacional seguro del launcher local."""


class StackAlreadyRunningError(LocalStackError):
    """Ya existe un supervisor vivo para este state root."""


class PortInUseError(LocalStackError):
    """Un puerto requerido pertenece a otro proceso."""


class ChildProcessError(LocalStackError):
    """Un proceso hijo no alcanzó o perdió su estado saludable."""


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path


@dataclass(slots=True)
class InstanceGuard:
    path: Path
    instance_id: str

    @classmethod
    def acquire(
        cls,
        path: Path,
        *,
        pid: int,
        instance_id: str,
        process_is_alive: Callable[[int], bool] | None = None,
    ) -> InstanceGuard:
        alive = process_is_alive or _process_is_alive
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "instance_id": instance_id,
            "pid": pid,
            "started_at": datetime.now(UTC).isoformat(),
        }
        for _attempt in range(2):
            try:
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(payload, stream, sort_keys=True)
                return cls(path=path, instance_id=instance_id)
            except FileExistsError:
                current = _read_json(path)
                current_pid = current.get("pid")
                if isinstance(current_pid, int) and alive(current_pid):
                    raise StackAlreadyRunningError(
                        f"el stack local ya está supervisado por el PID {current_pid}"
                    ) from None
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        raise StackAlreadyRunningError("otro launcher adquirió el lock en simultáneo")

    def release(self) -> None:
        current = _read_json(self.path)
        if current.get("instance_id") == self.instance_id:
            self.path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _node_major(path: Path) -> int:
    completed = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=5,
    )
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){2}\s*", completed.stdout)
    if match is None:
        raise LocalStackError("no se pudo interpretar la versión de Node.js")
    return int(match.group(1))


def bundled_node_candidate() -> Path:
    return (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / ("node.exe" if os.name == "nt" else "node")
    )


def choose_node(
    *,
    path_node: Path | None,
    bundled_node: Path,
    version_reader: Callable[[Path], int] = _node_major,
) -> Path:
    candidates = [bundled_node, path_node]
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        try:
            if version_reader(candidate) >= 20:
                return candidate.resolve()
        except (OSError, subprocess.SubprocessError, LocalStackError):
            continue
    raise LocalStackError(
        "se requiere Node.js >=20; el Node de PATH no sirve y no se encontró el runtime bundled"
    )


def build_shared_environment(
    *,
    base_environment: Mapping[str, str],
    state_root: Path,
    node_executable: Path,
) -> dict[str, str]:
    root = state_root.resolve()
    private = root / "private"
    environment = dict(base_environment)
    environment.update(
        {
            "DEFORESTATION_API_PRIVATE_ROOT": str(private),
            "DEFORESTATION_API_DATABASE": str(private / "jobs.sqlite3"),
            "DEFORESTATION_API_STORAGE": str(private / "objects"),
            "DEFORESTATION_ANALYSIS_OUTPUT_ROOT": str(root / "runs"),
            "DEFORESTATION_WORKER_ID": f"local-stack-{os.getpid()}",
        }
    )
    current_path = environment.get("PATH", "")
    environment["PATH"] = os.pathsep.join(
        item for item in (str(node_executable.parent.resolve()), current_path) if item
    )
    return environment


def build_process_plan(
    *, project_root: Path, uv_executable: Path, pnpm_executable: Path
) -> tuple[ProcessSpec, ...]:
    return (
        ProcessSpec(
            "api",
            (
                str(uv_executable),
                "run",
                "--package",
                "deforestation-api",
                "deforestation-api",
            ),
            project_root,
        ),
        ProcessSpec(
            "worker",
            (
                str(uv_executable),
                "run",
                "--package",
                "deforestation-worker",
                "deforestation-worker",
            ),
            project_root,
        ),
        ProcessSpec(
            "web",
            (str(pnpm_executable), "dev"),
            project_root / "apps" / "web",
        ),
    )


def assert_ports_available(host: str, ports: Sequence[int]) -> None:
    conflicts: list[int] = []
    for port in ports:
        probe = socket.socket()
        try:
            probe.bind((host, port))
        except OSError:
            conflicts.append(port)
        finally:
            probe.close()
    if conflicts:
        rendered = ", ".join(str(port) for port in conflicts)
        raise PortInUseError(f"puerto(s) ocupado(s): {rendered}; no se detuvo ningún proceso ajeno")


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(\b(?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s&,]+"),
    re.compile(r'(?i)("(?:private_key|token|password|secret|api[_-]?key)"\s*:\s*")[^"]*'),
    re.compile(r"(?i)([?&](?:x-goog-signature|x-amz-signature|signature|sig)=)[^&\s]+"),
)


def sanitize_log_text(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def _stream_logs(name: str, stream: IO[str], destination: IO[str], echo: bool) -> None:
    for line in stream:
        safe = sanitize_log_text(line.rstrip("\r\n"))
        destination.write(safe + "\n")
        destination.flush()
        if echo:
            print(f"[{name}] {safe}", flush=True)


def _start_processes(
    plan: Sequence[ProcessSpec], environment: Mapping[str, str], log_root: Path, *, echo: bool
) -> tuple[list[subprocess.Popen[str]], list[IO[str]], list[threading.Thread]]:
    processes: list[subprocess.Popen[str]] = []
    logs: list[IO[str]] = []
    readers: list[threading.Thread] = []
    try:
        for spec in plan:
            log: IO[str] = (log_root / f"{spec.name}.log").open("a", encoding="utf-8", buffering=1)
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(
                spec.command,
                cwd=spec.cwd,
                env=dict(environment),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            assert process.stdout is not None
            reader = threading.Thread(
                target=_stream_logs,
                args=(spec.name, process.stdout, log, echo),
                daemon=True,
                name=f"local-stack-log-{spec.name}",
            )
            reader.start()
            processes.append(process)
            logs.append(log)
            readers.append(reader)
    except BaseException:
        stop_children(processes)
        for log in logs:
            log.close()
        raise
    return processes, logs, readers


def _http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return bool(200 <= response.status < 400)
    except OSError:
        return False


def verify_readiness(
    worker: Any,
    *,
    timeout_seconds: float = 60,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    api_ready = False
    web_ready = False
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            raise ChildProcessError("el worker terminó antes de quedar operativo")
        api_ready = api_ready or _http_ready(f"http://127.0.0.1:{API_PORT}/health")
        web_ready = web_ready or _http_ready(f"http://127.0.0.1:{WEB_PORT}/")
        if api_ready and web_ready:
            return
        sleep(0.2)
    missing = ", ".join(
        name for name, ready in (("API", api_ready), ("web", web_ready)) if not ready
    )
    raise ChildProcessError(f"timeout esperando readiness de {missing}")


def stop_children(processes: Sequence[Any], *, timeout_seconds: float = 10) -> None:
    """Detiene sólo los procesos cuyo handle creó este supervisor."""
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        if os.name == "nt" and hasattr(process, "send_signal"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        elif os.name != "nt" and hasattr(process, "pid"):
            kill_process_group = os.killpg  # type: ignore[attr-defined]
            kill_process_group(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout_seconds)


def _find_executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise LocalStackError(f"no se encontró el ejecutable requerido: {name}")
    return Path(value)


def _pnpm_executable(node: Path) -> Path:
    runtime_root = node.parents[2]
    bundled = runtime_root / "bin" / "fallback" / ("pnpm.cmd" if os.name == "nt" else "pnpm")
    if bundled.is_file():
        return bundled
    return _find_executable("pnpm")


def _run_supervisor(state_root: Path, *, quiet: bool) -> int:
    instance_id = str(uuid4())
    lock = state_root / "launcher.json"
    guard = InstanceGuard.acquire(lock, pid=os.getpid(), instance_id=instance_id)
    processes: list[subprocess.Popen[str]] = []
    logs: list[IO[str]] = []
    try:
        (state_root / "ready.json").unlink(missing_ok=True)
        assert_ports_available("127.0.0.1", [API_PORT, WEB_PORT])
        path_node_value = shutil.which("node")
        node = choose_node(
            path_node=Path(path_node_value) if path_node_value else None,
            bundled_node=bundled_node_candidate(),
        )
        environment = build_shared_environment(
            base_environment=os.environ,
            state_root=state_root,
            node_executable=node,
        )
        for directory in (
            Path(environment["DEFORESTATION_API_STORAGE"]),
            Path(environment["DEFORESTATION_ANALYSIS_OUTPUT_ROOT"]),
        ):
            directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log_root = state_root / "logs" / stamp
        log_root.mkdir(parents=True, exist_ok=False)
        plan = build_process_plan(
            project_root=PROJECT_ROOT,
            uv_executable=_find_executable("uv"),
            pnpm_executable=_pnpm_executable(node),
        )
        processes, logs, _readers = _start_processes(plan, environment, log_root, echo=not quiet)
        worker = processes[1]
        verify_readiness(worker)
        ready = {
            "api_url": f"http://127.0.0.1:{API_PORT}",
            "instance_id": instance_id,
            "log_root": str(log_root),
            "pid": os.getpid(),
            "web_url": f"http://127.0.0.1:{WEB_PORT}",
        }
        (state_root / "ready.json").write_text(
            json.dumps(ready, indent=2, sort_keys=True), encoding="utf-8"
        )
        if not quiet:
            print(f"Stack listo: {ready['web_url']}")
            print(f"Logs: {log_root}")
            print("Ctrl+C detiene únicamente API, worker y web iniciados por este launcher.")
        stop_request = state_root / "stop.request"
        while True:
            failed = [
                spec.name
                for spec, process in zip(plan, processes, strict=True)
                if process.poll() is not None
            ]
            if failed:
                raise ChildProcessError(
                    f"proceso(s) terminado(s) inesperadamente: {', '.join(failed)}"
                )
            request = _read_json(stop_request)
            if request.get("instance_id") == instance_id:
                stop_request.unlink(missing_ok=True)
                return 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        (state_root / "ready.json").unlink(missing_ok=True)
        stop_children(processes)
        for log in logs:
            log.close()
        guard.release()


def _start_background(state_root: Path) -> int:
    lock = state_root / "launcher.json"
    current = _read_json(lock)
    pid = current.get("pid")
    if isinstance(pid, int) and _process_is_alive(pid):
        raise StackAlreadyRunningError(f"el stack local ya está supervisado por el PID {pid}")
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "ready.json").unlink(missing_ok=True)
    bootstrap = (state_root / "background-supervisor.log").open("a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--supervisor",
            "--state-root",
            str(state_root),
        ],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=bootstrap,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
    bootstrap.close()
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        ready = _read_json(state_root / "ready.json")
        if ready.get("web_url"):
            print(f"Stack listo en background: {ready['web_url']}")
            print(f"Logs: {ready['log_root']}")
            return 0
        supervisor = _read_json(lock)
        supervisor_pid = supervisor.get("pid")
        if isinstance(supervisor_pid, int) and not _process_is_alive(supervisor_pid):
            break
        time.sleep(0.25)
    raise LocalStackError(
        f"el stack background no quedó listo; revisá {state_root / 'background-supervisor.log'}"
    )


def _request_stop(state_root: Path) -> int:
    current = _read_json(state_root / "launcher.json")
    pid = current.get("pid")
    instance_id = current.get("instance_id")
    if not isinstance(pid, int) or not isinstance(instance_id, str) or not _process_is_alive(pid):
        raise LocalStackError("no hay un supervisor local vivo para detener")
    (state_root / "stop.request").write_text(
        json.dumps({"instance_id": instance_id}), encoding="utf-8"
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            print("Stack local detenido.")
            return 0
        time.sleep(0.25)
    raise LocalStackError("el supervisor no completó el shutdown dentro del plazo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true", help="inicia el supervisor desacoplado")
    mode.add_argument("--stop", action="store_true", help="detiene un supervisor background propio")
    mode.add_argument("--supervisor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.stop:
            return _request_stop(args.state_root.resolve())
        if args.background:
            return _start_background(args.state_root.resolve())
        return _run_supervisor(args.state_root.resolve(), quiet=args.supervisor)
    except LocalStackError as error:
        print(f"Error seguro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
