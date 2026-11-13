from __future__ import annotations

import copy
import re
from typing import Any

_WEIGHT = re.compile(r"[0-9]+(?:\.[0-9]{1,3})?\Z", re.ASCII)
_MAX_MG = 2**63 - 1


def grams_to_mg(value: str) -> int:
    if not isinstance(value, str) or not _WEIGHT.fullmatch(value):
        raise ValueError("invalid weight")
    whole, dot, fraction = value.partition(".")
    # Bound before integer conversion, including strings with huge digit counts.
    whole = whole.lstrip("0") or "0"
    if len(whole) > 16:
        raise ValueError("weight overflow")
    mg = int(whole) * 1000 + int((fraction + "000")[:3])
    if mg > _MAX_MG:
        raise ValueError("weight overflow")
    return mg


def mg_to_grams(value: int | None) -> str | None:
    if value is None:
        return None
    whole, fraction = divmod(value, 1000)
    return str(whole) if not fraction else f"{whole}.{fraction:03d}".rstrip("0")


def parse_pointer(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("invalid pointer")
    tokens = []
    for token in pointer[1:].split("/"):
        decoded = re.sub(r"~[01]", lambda m: "/" if m[0] == "~1" else "~", token)
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid escape")
        tokens.append(decoded)
    return tuple(tokens)


def json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            json_equal(left[k], right[k]) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def validate_override_paths(paths: list[tuple[str, ...]]) -> None:
    ordered = sorted(paths)
    for first, second in zip(ordered, ordered[1:], strict=False):
        if len(first) < len(second) and second[: len(first)] == first:
            raise ValueError("overlapping pointers")


def apply_overrides(
    source: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(source)
    parsed = [(parse_pointer(path), value) for path, value in overrides.items()]
    validate_override_paths([tokens for tokens, _ in parsed])
    for tokens, value in parsed:
        node: Any = result
        for token in tokens[:-1]:
            if token not in node or not isinstance(node[token], dict):
                node[token] = {}
            node = node[token]
        node[tokens[-1]] = copy.deepcopy(value)
    return result


def validate_new_pointer(source: dict[str, Any], pointer: str) -> tuple[str, ...]:
    tokens = parse_pointer(pointer)
    node: Any = source
    for token in tokens[:-1]:
        if (
            not isinstance(node, dict)
            or token not in node
            or not isinstance(node[token], dict)
        ):
            raise ValueError("pointer parent missing")
        node = node[token]
    if not isinstance(node, dict):
        raise ValueError("pointer parent missing")
    return tokens
