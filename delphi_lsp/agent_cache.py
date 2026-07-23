from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, fields, is_dataclass
import sys
import re
from types import ModuleType


DEFAULT_MAX_MEMORY_BYTES = 512 * 1024**2
WARNING_THRESHOLD_PERCENT = 80
_MEMORY_SIZE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<suffix>[KMG]?)$", re.IGNORECASE)


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

    compacted = " Cache compacted." if result.compacted else ""
    return (
        "Warning: Delphi cache reached "
        f"{result.peak_utilization_percent:.1f}% of {max_bytes} bytes.{compacted} "
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
