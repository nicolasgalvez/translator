from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable


FilterCallback = Callable[[Any, dict[str, Any]], Any]
ActionCallback = Callable[[Any, dict[str, Any]], None]


@dataclass(order=True)
class _HookEntry:
    priority: int
    order: int
    callback: Callable = field(compare=False)


_filters: dict[str, list[_HookEntry]] = defaultdict(list)
_actions: dict[str, list[_HookEntry]] = defaultdict(list)
_order = count()


def add_filter(name: str, callback: FilterCallback, priority: int = 10) -> None:
    _filters[name].append(_HookEntry(priority, next(_order), callback))
    _filters[name].sort()


def apply_filters(name: str, value: Any, context: dict[str, Any] | None = None) -> Any:
    context = context or {}
    for entry in _filters.get(name, []):
        value = entry.callback(value, context)
    return value


def add_action(name: str, callback: ActionCallback, priority: int = 10) -> None:
    _actions[name].append(_HookEntry(priority, next(_order), callback))
    _actions[name].sort()


def do_action(name: str, payload: Any, context: dict[str, Any] | None = None) -> None:
    context = context or {}
    for entry in _actions.get(name, []):
        entry.callback(payload, context)


def clear_hooks() -> None:
    """Reset registered hooks. Intended for tests."""
    _filters.clear()
    _actions.clear()
