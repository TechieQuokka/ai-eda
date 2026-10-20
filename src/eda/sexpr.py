"""KiCad's S-expression files (`.kicad_sym`, `.kicad_sch`): a small reader and writer.

Expressions are lists; bare words are `Atom`s; quoted strings are plain `str`, so a file
read and written again keeps its quoting.
"""

import re
from collections.abc import Iterator

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_UNESCAPE = re.compile(r"\\(.)", re.S)


class Atom(str):
    """A bare (unquoted) word such as a keyword, number, or `yes`."""

    __slots__ = ()


type SExpr = list["SExpr"] | Atom | str


def parse(text: str) -> list[SExpr]:
    """Parse one top-level expression."""
    stack: list[list[SExpr]] = [[]]
    pos = 0
    while True:
        match = _TOKEN.match(text, pos)
        if match is None:
            if text[pos:].strip():
                raise ValueError(f"cannot parse S-expression at offset {pos}")
            break
        pos = match.end()
        if match.group(1):
            stack.append([])
        elif match.group(2):
            if len(stack) < 2:
                raise ValueError(f"unbalanced ')' at offset {pos}")
            done = stack.pop()
            stack[-1].append(done)
        elif match.group(3) is not None:
            stack[-1].append(_UNESCAPE.sub(lambda m: "\n" if m[1] == "n" else m[1], match[3]))
        else:
            stack[-1].append(Atom(match.group(4)))
    if len(stack) != 1 or len(stack[0]) != 1 or not isinstance(stack[0][0], list):
        raise ValueError("expected exactly one top-level expression")
    return stack[0][0]


def dumps(expr: SExpr, indent: int = 0) -> str:
    """Write an expression, one sub-list per line (KiCad's tab-indented layout)."""
    if not isinstance(expr, list):
        return _atom(expr)
    if not any(isinstance(item, list) for item in expr):
        return "(" + " ".join(_atom(item) for item in expr if not isinstance(item, list)) + ")"
    pad = "\t" * (indent + 1)
    parts: list[str] = []
    head: list[str] = []
    for item in expr:
        if isinstance(item, list):
            parts.append("\n" + pad + dumps(item, indent + 1))
        elif parts:
            parts.append(" " + _atom(item))
        else:
            head.append(_atom(item))
    return "(" + " ".join(head) + "".join(parts) + "\n" + "\t" * indent + ")"


def _atom(item: Atom | str) -> str:
    if isinstance(item, Atom):
        return str(item)
    escaped = item.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def head(expr: SExpr) -> str | None:
    """The keyword of a list such as `(pin ...)`."""
    if isinstance(expr, list) and expr and isinstance(expr[0], Atom):
        return str(expr[0])
    return None


def children(expr: SExpr, keyword: str) -> Iterator[list[SExpr]]:
    """Direct sub-lists of `expr` that start with `keyword`."""
    if isinstance(expr, list):
        for item in expr:
            if isinstance(item, list) and head(item) == keyword:
                yield item


def child(expr: SExpr, keyword: str) -> list[SExpr] | None:
    return next(children(expr, keyword), None)


def number(expr: SExpr) -> float:
    if not isinstance(expr, str):
        raise ValueError(f"expected a number, got {expr!r}")
    return float(expr)
