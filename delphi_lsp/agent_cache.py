from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
import argparse
import contextlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import re
import tempfile
import threading
import time
from types import ModuleType

from ._version import __version__
from .agent_context import AgentContext
from .agent_protocol import AgentProtocolError


DEFAULT_MAX_MEMORY_BYTES = 512 * 1024**2
WARNING_THRESHOLD_PERCENT = 80
DAEMON_SCHEMA = 2
DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_STARTUP_TIMEOUT = 120.0
_MAX_MESSAGE_BYTES = 1024 * 1024
_CONNECTION_TIMEOUT = 2.0
_MEMORY_SIZE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<suffix>[KMG]?)$", re.IGNORECASE)
_STARTUP_DIAGNOSTIC_BYTES = 16 * 1024
_STARTUP_TOKEN_RE = re.compile(r"(?i)(token\b[^\n\r]*?:?\s*['\"]?[A-Za-z0-9_-]+['\"]?|\b[a-zA-Z0-9_-]{32,})")


def estimate_deep_size(value: object) -> int:
    """Estimate the memory retained by an owned object graph.

    The traversal is intentionally best-effort: unsupported or opaque values
    still count themselves, but do not cause cache accounting to fail.
    """
    total = 0
    seen: set[int] = set()
    pending: list[object] = [value]

    while pending:
        current = pending.pop()
        if isinstance(current, (ModuleType, type)) or callable(current):
            continue
        identifier = id(current)
        if identifier in seen:
            continue
        seen.add(identifier)
        try:
            total += sys.getsizeof(current)
        except Exception:
            pass

        try:
            pending.extend(_children(current))
        except Exception:
            continue
    return total


@dataclass
class CacheStats:
    requests: int = 0
    warm_hits: int = 0
    rebuilds: int = 0
    invalidations: int = 0
    evictions: int = 0
    parallel_fallbacks: int = 0


@dataclass(frozen=True)
class BudgetResult:
    retained_bytes: int
    utilization_percent: float
    peak_utilization_percent: float
    warning_active: bool
    warning_triggered: bool
    compacted: bool


@dataclass(frozen=True)
class CacheBudget:
    max_bytes: int = DEFAULT_MAX_MEMORY_BYTES
    warning_percent: int = WARNING_THRESHOLD_PERCENT

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be greater than zero.")
        if not 0 < self.warning_percent <= 100:
            raise ValueError("warning_percent must be between 1 and 100.")

    def enforce(
        self,
        *,
        measure: Callable[[], int],
        evict_auxiliary: Callable[[], None],
        evict_navigation: Callable[[], None],
    ) -> BudgetResult:
        initial_retained = measure()
        initial_utilization_percent = initial_retained * 100.0 / self.max_bytes
        compacted = False
        retained = initial_retained

        if initial_retained > self.max_bytes:
            evict_auxiliary()
            compacted = True
            retained = measure()
            if retained > self.max_bytes:
                evict_navigation()
                retained = measure()

        current_utilization_percent = retained * 100.0 / self.max_bytes
        return BudgetResult(
            retained_bytes=retained,
            utilization_percent=current_utilization_percent,
            peak_utilization_percent=initial_utilization_percent,
            warning_active=current_utilization_percent >= self.warning_percent,
            warning_triggered=initial_utilization_percent >= self.warning_percent,
            compacted=compacted,
        )


def parse_memory_size(value: str) -> int:
    match = _MEMORY_SIZE.fullmatch(value.strip())
    if match is None:
        raise ValueError("Memory size must be a positive integer with optional K, M, or G suffix.")

    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
    }
    return int(match.group("count")) * multiplier[match.group("suffix").upper()]


def cache_warning(result: BudgetResult, max_bytes: int) -> str:
    if not result.warning_triggered:
        return ""

    if result.compacted:
        state = (
            f"Warning: Delphi cache peaked at {result.peak_utilization_percent:.1f}% of the {max_bytes} byte budget; "
            f"{result.retained_bytes} bytes remain retained after compaction."
            f"{' Cache compacted.' if result.compacted else ''}"
        )
    else:
        state = (
            f"Warning: Delphi cache currently at {result.utilization_percent:.1f}% of the {max_bytes} byte budget; "
            f"{result.retained_bytes} bytes retained."
        )
    return (
        f"{state} "
        "Increase --max-memory, stop unused daemons, or allow compact mode."
    )


def _children(value: object) -> tuple[object, ...]:
    children: list[object] = []
    if isinstance(value, Mapping):
        try:
            for key, item in value.items():
                children.extend((key, item))
        except Exception:
            pass
    elif isinstance(value, Collection) and not isinstance(value, (str, bytes, bytearray)):
        try:
            children.extend(value)
        except Exception:
            pass

    is_dataclass_instance = is_dataclass(value) and not isinstance(value, type)
    dataclass_field_names: frozenset[str] = frozenset()
    if is_dataclass_instance:
        try:
            dataclass_fields = fields(value)
        except Exception:
            dataclass_fields = ()
        dataclass_field_names = frozenset(field.name for field in dataclass_fields)
        for field in dataclass_fields:
            try:
                children.append(getattr(value, field.name))
            except Exception:
                continue
    else:
        try:
            instance_values = vars(value)
        except Exception:
            instance_values = None
        if isinstance(instance_values, Mapping):
            children.extend(instance_values.values())

    for slot in _slot_names(type(value)):
        if slot in dataclass_field_names:
            continue
        try:
            children.append(getattr(value, slot))
        except Exception:
            continue
    return tuple(children)


def _slot_names(value_type: type[object]) -> tuple[str, ...]:
    names: list[str] = []
    for base in value_type.__mro__:
        try:
            slots = getattr(base, "__slots__", ())
        except Exception:
            continue
        if isinstance(slots, str):
            slots = (slots,)
        try:
            names.extend(name for name in slots if isinstance(name, str))
        except Exception:
            continue
    return tuple(names)


@dataclass(frozen=True)
class CacheMetadata:
    schema: int
    root: str
    pid: int
    port: int
    token: str
    version: str
    project_file: str
    max_memory_bytes: int
    workers: int
    idle_timeout: int
    started_at: float


@dataclass(frozen=True)
class CacheClientResponse:
    payload: dict[str, object]
    warning: str = ""


class CacheClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def cache_metadata_path(root: str | Path) -> Path:
    return Path(root).resolve() / ".delphi-lsp" / "agent-cache" / "daemon.json"


def _safe_metadata_path(root: str | Path, *, create: bool = False) -> Path:
    root_path = Path(root).resolve()
    for path in (root_path / ".delphi-lsp", root_path / ".delphi-lsp" / "agent-cache"):
        if path.exists() and path.is_symlink():
            raise CacheClientError("unsafe_metadata", "Cache metadata path is unsafe.")
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
            if os.name != "nt":
                os.chmod(path, 0o700)
    result = root_path / ".delphi-lsp" / "agent-cache" / "daemon.json"
    if result.exists() and result.is_symlink():
        raise CacheClientError("unsafe_metadata", "Cache metadata path is unsafe.")
    return result


def _metadata_mapping(metadata: CacheMetadata) -> dict[str, object]:
    return {field.name: getattr(metadata, field.name) for field in fields(metadata)}


def _write_metadata(metadata: CacheMetadata) -> None:
    path = _safe_metadata_path(metadata.root, create=True)
    data = json.dumps(_metadata_mapping(metadata), sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".daemon-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_metadata_record(root: str | Path) -> object | None:
    path = _safe_metadata_path(root)
    if not path.exists():
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if os.name != "nt" and (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise CacheClientError("unsafe_metadata", "Cache metadata path is unsafe.")
            raw = json.loads(os.read(descriptor, _MAX_MESSAGE_BYTES).decode("utf-8"))
        finally:
            os.close(descriptor)
    except CacheClientError:
        raise
    except (OSError, ValueError, UnicodeError):
        return None
    return raw


def _read_metadata(root: str | Path) -> CacheMetadata | None:
    raw = _read_metadata_record(root)
    required = {field.name for field in fields(CacheMetadata)}
    if not isinstance(raw, dict) or set(raw) != required:
        return None
    values = tuple(
        raw[name]
        for name in (
            "schema",
            "root",
            "pid",
            "port",
            "token",
            "version",
            "project_file",
            "max_memory_bytes",
            "workers",
            "idle_timeout",
            "started_at",
        )
    )
    if (type(values[0]) is not int or values[0] != DAEMON_SCHEMA or not isinstance(values[1], str)
            or type(values[2]) is not int or values[2] <= 0 or type(values[3]) is not int or not 0 < values[3] < 65536
            or not isinstance(values[4], str) or len(values[4]) < 32 or not isinstance(values[5], str)
            or not isinstance(values[6], str) or type(values[7]) is not int or values[7] <= 0
            or type(values[8]) is not int or not 0 <= values[8] <= 32
            or type(values[9]) is not int or values[9] <= 0 or type(values[10]) not in (int, float)):
        return None
    canonical = str(Path(root).resolve())
    if values[1] != canonical:
        return None
    return CacheMetadata(*values)


def _remove_incompatible_metadata_if_stale(root: str | Path) -> None:
    path = _safe_metadata_path(root)
    if not path.exists():
        return
    raw = _read_metadata_record(root)
    canonical = str(Path(root).resolve())
    pid = raw.get("pid") if isinstance(raw, dict) and raw.get("root") == canonical else None
    if type(pid) is int and pid > 0 and _pid_alive(pid):
        raise CacheClientError("unavailable", "Live cache daemon is unavailable.")
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _remove_metadata_if_owned(metadata: CacheMetadata) -> None:
    path = _safe_metadata_path(metadata.root)
    current = _read_metadata(metadata.root)
    if current is not None and current.pid == metadata.pid and hmac.compare_digest(current.token, metadata.token):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reap_child_if_exited(pid: int) -> bool:
    waitpid = getattr(os, "waitpid", None)
    nohang = getattr(os, "WNOHANG", None)
    if not callable(waitpid) or nohang is None:
        return False
    try:
        return waitpid(pid, nohang)[0] == pid
    except (ChildProcessError, OSError):
        return False


def _start_lock_path(root: str | Path) -> Path:
    return _safe_metadata_path(root, create=True).with_name("start.lock")


@contextlib.contextmanager
def _start_lock(root: str | Path, timeout: float):
    path = _start_lock_path(root)
    token = secrets.token_urlsafe(24)
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                payload = json.dumps({"pid": os.getpid(), "token": token, "started_at": time.time()}).encode("utf-8")
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            break
        except FileExistsError:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                owner = record.get("pid") if isinstance(record, dict) else None
                started = record.get("started_at") if isinstance(record, dict) else None
                stale = type(owner) is not int or not _pid_alive(owner) or type(started) not in (int, float)
            except (OSError, ValueError, UnicodeError):
                stale = True
            if stale:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise CacheClientError("startup_locked", "Cache startup is busy.")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and hmac.compare_digest(str(current.get("token", "")), token):
                path.unlink()
        except (OSError, ValueError, UnicodeError):
            pass


def _client_exchange(metadata: CacheMetadata, request: dict[str, object]) -> CacheClientResponse:
    try:
        with socket.create_connection(("127.0.0.1", metadata.port), timeout=2) as connection:
            connection.settimeout(3)
            request_without_token = {key: value for key, value in request.items() if key != "token"}
            connection.sendall(json.dumps({"token": metadata.token, **request_without_token}, separators=(",", ":")).encode("utf-8") + b"\n")
            response = _read_line(connection)
    except OSError as error:
        raise CacheClientError("unavailable", "Cache daemon is unavailable.") from error
    try:
        decoded = json.loads(response.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise CacheClientError("invalid_response", "Cache daemon returned an invalid response.") from error
    if not isinstance(decoded, dict):
        raise CacheClientError("invalid_response", "Cache daemon returned an invalid response.")
    if "error" in decoded:
        error = decoded["error"]
        if isinstance(error, dict):
            raise CacheClientError(str(error.get("code", "error")), str(error.get("message", "Cache request failed.")))
        raise CacheClientError("error", "Cache request failed.")
    payload = decoded.get("payload")
    if not isinstance(payload, dict):
        raise CacheClientError("invalid_response", "Cache daemon returned an invalid response.")
    return CacheClientResponse(payload, str(decoded.get("warning", "")))


def _read_line(connection: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= _MAX_MESSAGE_BYTES:
        piece = connection.recv(min(65536, _MAX_MESSAGE_BYTES + 1 - len(chunks)))
        if not piece:
            break
        chunks.extend(piece)
        if b"\n" in piece:
            line, _, _ = chunks.partition(b"\n")
            return line
    raise CacheClientError("invalid_response", "Cache daemon returned an invalid response.")


def _truncate_and_sanitize_startup_diagnostics(raw: bytes, *, max_bytes: int = _STARTUP_DIAGNOSTIC_BYTES) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if max_bytes > 0 and len(text) > max_bytes:
        text = text[-max_bytes:]
    text = re.sub(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", " ", text)
    text = _STARTUP_TOKEN_RE.sub("<redacted>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _read_startup_tail(diagnostics: object, *, max_bytes: int = _STARTUP_DIAGNOSTIC_BYTES) -> bytes:
    with contextlib.suppress(Exception):
        diagnostics.seek(0, os.SEEK_END)
        end = diagnostics.tell()
        diagnostics.seek(max(0, end - max_bytes), os.SEEK_SET)
        return diagnostics.read()
    return b""


class _CacheService:
    def __init__(self, metadata: CacheMetadata) -> None:
        self.metadata = metadata
        self.context = AgentContext.open(
            metadata.root,
            metadata.project_file or None,
            workers=metadata.workers,
            worker_memory_budget_bytes=metadata.max_memory_bytes,
        )
        self.budget = CacheBudget(metadata.max_memory_bytes)
        self.stats = CacheStats()
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_activity = self.started
        self.cache_state = "warming"
        self.last_revision = self.context.workspace.workspace_revision
        self.last_budget = BudgetResult(0, 0.0, 0.0, False, False, False)
        self.prewarm_seconds = 0.0
        self.shutdown = threading.Event()

    def prewarm(self) -> None:
        started = time.monotonic()
        try:
            self.context.handle({"action": "find", "query": "", "max_items": 1, "max_chars": 256})
            self.last_revision = self.context.workspace.workspace_revision
            self.cache_state = "warm"
        except AgentProtocolError as error:
            if error.code != "project_required":
                raise
            self.cache_state = "ready"
        self.last_budget = self.budget.enforce(
            measure=lambda: estimate_deep_size(self.context.cache_roots()),
            evict_auxiliary=self.context.evict_auxiliary_caches,
            evict_navigation=self.context.evict_navigation_caches,
        )
        if self.last_budget.compacted:
            self.stats.evictions += 1
            self.cache_state = "compact"
        self.prewarm_seconds = time.monotonic() - started
        self.stats.parallel_fallbacks = self.context.parallel_stats.fallbacks

    def request(self, request: dict[str, object]) -> CacheClientResponse:
        with self.lock:
            action = request.get("action")
            if action == "status":
                warning = "" if request.get("_startup_probe") is True else self._consume_warning()
                return CacheClientResponse(self.status(), warning)
            if action == "stop":
                self.shutdown.set()
                return CacheClientResponse({"stopping": True})
            self.last_activity = time.monotonic()
            before = self.last_revision
            was_warm = self.context.navigation_cache_is_warm
            try:
                response = self.context.handle(request).to_mapping()
            except AgentProtocolError as error:
                raise CacheClientError(error.code, error.message) from None
            except (OSError, UnicodeError):
                raise CacheClientError("source_unavailable", "Source is unavailable.") from None
            except Exception:
                raise CacheClientError("internal_error", "Internal cache error.") from None
            after = str(response["workspace_revision"])
            self.last_revision = after
            self.stats.requests += 1
            if was_warm:
                self.stats.warm_hits += 1
            else:
                self.stats.rebuilds += 1
                self.stats.parallel_fallbacks += self.context.parallel_stats.fallbacks
            if before != after:
                self.stats.invalidations += 1
            self.last_budget = self.budget.enforce(
                measure=lambda: estimate_deep_size(self.context.cache_roots()),
                evict_auxiliary=self.context.evict_auxiliary_caches,
                evict_navigation=self.context.evict_navigation_caches,
            )
            if self.last_budget.compacted:
                self.stats.evictions += 1
                self.cache_state = "compact"
            else:
                self.cache_state = "warm" if self.context.navigation_cache_is_warm else "ready"
            return CacheClientResponse(response, self._consume_warning())

    def _consume_warning(self) -> str:
        warning = cache_warning(self.last_budget, self.budget.max_bytes)
        if self.last_budget.warning_triggered and not self.last_budget.warning_active:
            self.last_budget = replace(self.last_budget, warning_triggered=False)
        return warning

    def status(self) -> dict[str, object]:
        now = time.monotonic()
        idle = max(0.0, now - self.last_activity)
        return {
            "pid": self.metadata.pid, "root": self.metadata.root, "project_file": self.metadata.project_file,
            "version": self.metadata.version, "uptime": now - self.started, "idle_seconds": idle,
            "last_activity_at": time.time() - idle,
            "max_memory_bytes": self.budget.max_bytes, "current_bytes": self.last_budget.retained_bytes,
            "current_utilization_percent": self.last_budget.utilization_percent,
            "peak_utilization_percent": self.last_budget.peak_utilization_percent,
            "warning_active": self.last_budget.warning_active, "warning_threshold_percent": WARNING_THRESHOLD_PERCENT,
            "cache_state": self.cache_state, "requests": self.stats.requests, "warm_hits": self.stats.warm_hits,
            "rebuilds": self.stats.rebuilds, "invalidations": self.stats.invalidations, "evictions": self.stats.evictions,
            "workers_configured": "auto" if self.metadata.workers == 0 else self.metadata.workers,
            "workers_effective": self.context.parallel_stats.effective_workers,
            "parallel_files_completed": self.context.parallel_stats.files_completed,
            "prewarm_seconds": self.prewarm_seconds,
            "parallel_seconds": self.context.parallel_stats.elapsed_seconds,
            "parallel_fallbacks": self.stats.parallel_fallbacks,
            "idle_timeout": self.metadata.idle_timeout, "idle_remaining": max(0.0, self.metadata.idle_timeout - idle),
            "workspace_revision": self.context.workspace.workspace_revision,
        }


def _serve_connection(connection: socket.socket, service: _CacheService) -> None:
    try:
        line = _read_line(connection)
        request = json.loads(line.decode("utf-8"))
        if not isinstance(request, dict) or not hmac.compare_digest(str(request.pop("token", "")), service.metadata.token):
            response: dict[str, object] = {"error": {"code": "authentication_failed", "message": "authentication failed"}}
        else:
            try:
                result = service.request(request)
                response = {"payload": result.payload, "warning": result.warning}
            except CacheClientError as error:
                response = {"error": {"code": error.code, "message": error.message}}
    except Exception:
        response = {"error": {"code": "invalid_request", "message": "Invalid cache request."}}
    with contextlib.suppress(OSError):
        connection.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")


def run_cache_daemon(
    root: str | Path,
    *,
    project_file: str = "",
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    workers: int = 0,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
) -> None:
    canonical = str(Path(root).resolve())
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.25)
    metadata = CacheMetadata(
        DAEMON_SCHEMA,
        canonical,
        os.getpid(),
        listener.getsockname()[1],
        secrets.token_urlsafe(32),
        __version__,
        project_file,
        max_memory_bytes,
        workers,
        idle_timeout,
        time.time(),
    )
    try:
        service = _CacheService(metadata)
        service.prewarm()
        _write_metadata(metadata)
        while not service.shutdown.is_set() and time.monotonic() - service.last_activity < idle_timeout:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(_CONNECTION_TIMEOUT)
                _serve_connection(connection, service)
    finally:
        listener.close()
        _remove_metadata_if_owned(metadata)


def _start_cache_unlocked(
    root: str | Path,
    *,
    project_file: str | Path | None = None,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    workers: int = 0,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> CacheMetadata:
    canonical = str(Path(root).resolve())
    project = str((Path(canonical) / project_file).resolve()) if project_file and not Path(project_file).is_absolute() else (str(Path(project_file).resolve()) if project_file else "")
    existing = _read_metadata(canonical)
    if existing and _pid_alive(existing.pid):
        try:
            _client_exchange(existing, {"action": "status", "_startup_probe": True})
        except CacheClientError as error:
            raise CacheClientError("unavailable", "Live cache daemon is unavailable.") from error
        if (existing.project_file, existing.max_memory_bytes, existing.workers, existing.idle_timeout) != (
            project,
            max_memory_bytes,
            workers,
            idle_timeout,
        ):
            raise CacheClientError("configuration_conflict", "A live cache daemon has conflicting configuration.")
        return existing
    if existing:
        _remove_metadata_if_owned(existing)
    else:
        _remove_incompatible_metadata_if_stale(canonical)
    command = [
        sys.executable,
        "-m",
        "delphi_lsp.agent_cache",
        "serve",
        "--root",
        canonical,
        "--max-memory",
        str(max_memory_bytes),
        "--workers",
        str(workers),
        "--idle-timeout",
        str(idle_timeout),
    ]
    if project:
        command.extend(("--project-file", project))
    options: dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with tempfile.TemporaryFile() as diagnostics:
        options["stderr"] = diagnostics
        process = subprocess.Popen(command, **options)
        deadline = time.monotonic() + startup_timeout
        startup_ready = False
        try:
            while time.monotonic() < deadline:
                metadata = _read_metadata(canonical)
                if metadata:
                    try:
                        _client_exchange(metadata, {"action": "status", "_startup_probe": True})
                        startup_ready = True
                        return metadata
                    except CacheClientError:
                        pass
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if process.poll() is None:
                process.kill()
            with contextlib.suppress(Exception):
                process.wait()
            metadata = _read_metadata(canonical)
            if metadata and metadata.pid == process.pid:
                _remove_metadata_if_owned(metadata)
            diagnostics.seek(0)
            raw = _read_startup_tail(diagnostics)
            message = _truncate_and_sanitize_startup_diagnostics(raw)
            base = "Cache daemon did not become ready."
            if message:
                raise CacheClientError("startup_failed", f"{base} {message}")
            raise CacheClientError("startup_failed", base)
        finally:
            if not startup_ready:
                if process.poll() is None:
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait()


def start_cache(
    root: str | Path,
    *,
    project_file: str | Path | None = None,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    workers: int = 0,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> CacheMetadata:
    if type(workers) is not int or not 0 <= workers <= 32:
        raise ValueError("workers must be auto or an integer from 1 through 32.")
    if not math.isfinite(startup_timeout) or startup_timeout <= 0:
        raise ValueError("startup_timeout must be greater than zero.")
    with _start_lock(root, startup_timeout):
        return _start_cache_unlocked(
            root,
            project_file=project_file,
            max_memory_bytes=max_memory_bytes,
            workers=workers,
            idle_timeout=idle_timeout,
            startup_timeout=startup_timeout,
        )


def query_cache(root: str | Path, request: dict[str, object]) -> CacheClientResponse:
    metadata = _read_metadata(root)
    if metadata is None:
        _remove_incompatible_metadata_if_stale(root)
        raise CacheClientError("cache_not_running", "Cache daemon is not running.")
    if not _pid_alive(metadata.pid):
        _remove_metadata_if_owned(metadata)
        raise CacheClientError("cache_not_running", "Cache daemon is not running.")
    return _client_exchange(metadata, request)


def cache_status(root: str | Path) -> dict[str, object]:
    return query_cache(root, {"action": "status"}).payload


def stop_cache(root: str | Path) -> None:
    metadata = _read_metadata(root)
    if metadata is None:
        _remove_incompatible_metadata_if_stale(root)
        return
    if not _pid_alive(metadata.pid):
        _remove_metadata_if_owned(metadata)
        return
    try:
        _client_exchange(metadata, {"action": "stop"})
    except CacheClientError:
        pass
    deadline = time.monotonic() + 3
    stopped = False
    while _pid_alive(metadata.pid) and time.monotonic() < deadline:
        if _reap_child_if_exited(metadata.pid):
            stopped = True
            break
        time.sleep(0.05)
    if not stopped and _pid_alive(metadata.pid):
        raise CacheClientError("stop_failed", "Cache daemon did not stop.")
    _remove_metadata_if_owned(metadata)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    command = parser.add_subparsers(dest="command", required=True)
    serve = command.add_parser("serve")
    serve.add_argument("--root", required=True)
    serve.add_argument("--project-file", default="")
    serve.add_argument("--max-memory", type=int, default=DEFAULT_MAX_MEMORY_BYTES)
    serve.add_argument("--workers", type=int, default=0)
    serve.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT)
    args = parser.parse_args(argv)
    if args.command == "serve":
        run_cache_daemon(
            args.root,
            project_file=args.project_file,
            max_memory_bytes=args.max_memory,
            workers=args.workers,
            idle_timeout=args.idle_timeout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
