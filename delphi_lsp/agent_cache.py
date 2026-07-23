from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import fields, is_dataclass
import sys
from types import ModuleType


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
