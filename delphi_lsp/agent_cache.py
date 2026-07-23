from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import fields, is_dataclass
import sys
from types import ModuleType
from typing import Any


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
        except (TypeError, ValueError):
            pass

        pending.extend(_children(current))
    return total


def _children(value: object) -> tuple[object, ...]:
    children: list[object] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            children.extend((key, item))
    elif isinstance(value, Collection) and not isinstance(value, (str, bytes, bytearray)):
        children.extend(value)

    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            try:
                children.append(getattr(value, field.name))
            except (AttributeError, TypeError, ValueError):
                continue

    try:
        instance_values: Any = vars(value)
    except (TypeError, ValueError):
        instance_values = None
    if isinstance(instance_values, Mapping):
        children.extend(instance_values.values())

    for slot in _slot_names(type(value)):
        try:
            children.append(getattr(value, slot))
        except (AttributeError, TypeError, ValueError):
            continue
    return tuple(children)


def _slot_names(value_type: type[object]) -> tuple[str, ...]:
    names: list[str] = []
    for base in value_type.__mro__:
        slots = getattr(base, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        try:
            names.extend(name for name in slots if isinstance(name, str))
        except TypeError:
            continue
    return tuple(names)
