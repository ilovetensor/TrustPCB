"""A small, fast S-expression reader for KiCad files.

A node is a Python ``list``. The first item is the tag (``"segment"``), the rest are
atoms (``str``) or child nodes. Quoted strings and bare atoms both become ``str``; KiCad
never relies on the difference. Numbers stay as text until a caller converts them.

The tokenizer is one compiled regular expression driven by ``finditer``; a 0.5 MB board
parses in a few tens of milliseconds.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

Node = list  # list[str | Node]

_TOKEN = re.compile(r'(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+)')
_ESCAPE = re.compile(r"\\(.)", re.S)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


class SexprError(ValueError):
    """Raised when the text is not a balanced S-expression."""


def _unescape(raw: str) -> str:
    return _ESCAPE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), raw)


def parse(text: str) -> Node:
    """Parse ``text`` and return its first top-level form."""
    stack: list[Node] = []
    current: Node | None = None
    root: Node | None = None
    for match in _TOKEN.finditer(text):
        kind = match.lastindex
        if kind == 1:
            child: Node = []
            if current is not None:
                current.append(child)
                stack.append(current)
            current = child
            if root is None:
                root = child
        elif kind == 2:
            if current is None:
                raise SexprError("unbalanced ')'")
            current = stack.pop() if stack else None
            if current is None:
                return root  # type: ignore[return-value]
        elif current is not None:
            if kind == 3:
                value = match.group(3)
                current.append(_unescape(value) if "\\" in value else value)
            else:
                current.append(match.group(4))
    if root is None:
        raise SexprError("no expression found")
    raise SexprError("unbalanced '(' (unexpected end of input)")


def find(node: Node, tag: str) -> Node | None:
    """First direct child list whose tag is ``tag``."""
    for child in node:
        if child.__class__ is list and child and child[0] == tag:
            return child
    return None


def findall(node: Node, tag: str) -> Iterator[Node]:
    """Every direct child list whose tag is ``tag``."""
    for child in node:
        if child.__class__ is list and child and child[0] == tag:
            yield child


def arg(node: Node, tag: str, default: Any = None, index: int = 1) -> Any:
    """``node``'s child ``tag`` argument at ``index`` (default the first), else ``default``."""
    child = find(node, tag)
    if child is None or len(child) <= index:
        return default
    return child[index]


def num(node: Node, tag: str, default: float | None = None, index: int = 1) -> float | None:
    """Like :func:`arg` but converted to ``float``."""
    value = arg(node, tag, None, index)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def flag(node: Node, tag: str) -> bool:
    """A KiCad boolean child, in either ``(tag yes)`` or bare-atom ``tag`` form."""
    child = find(node, tag)
    if child is not None:
        return len(child) < 2 or child[1] not in ("no", "false")
    return tag in node


def atoms(node: Node) -> list[str]:
    """The string arguments of ``node`` (children that are lists are skipped)."""
    return [x for x in node[1:] if x.__class__ is str]


def xy(node: Node | None, tag: str = "xy") -> tuple[float, float] | None:
    """Read ``(tag x y)`` from a node."""
    if node is None:
        return None
    return float(node[1]), float(node[2])
