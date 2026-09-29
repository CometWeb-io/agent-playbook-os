from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from typing import Any

_TEMPLATE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_HYPHEN_ROOT_SEGMENT = re.compile(r"\b(inputs|steps|run|control)\.([A-Za-z0-9_]*-[A-Za-z0-9_-]*)")
_MAX_EXPR_CHARS = 4096
_MAX_AST_NODES = 256


class AttrView:
    def __init__(self, value: Any):
        self._value = value

    def __getattr__(self, key: str) -> Any:
        if isinstance(self._value, Mapping) and key in self._value:
            return wrap(self._value[key])
        raise AttributeError(key)

    def __getitem__(self, key: Any) -> Any:
        return wrap(self._value[key])

    def raw(self):
        return self._value

    def __repr__(self):
        return repr(self._value)



def wrap(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return AttrView(value)
    return value


def unwrap(value: Any) -> Any:
    if isinstance(value, AttrView):
        return value.raw()
    return value


_ALLOWED_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}


def _normalize_expr(expr: str) -> str:
    # Python parses `steps.my-step` as subtraction. Step IDs intentionally allow
    # hyphens, so normalize only the first mapping segment under known roots.
    return _HYPHEN_ROOT_SEGMENT.sub(lambda m: f"{m.group(1)}[{m.group(2)!r}]", expr)


def safe_eval(expr: str, context: dict[str, Any]) -> Any:
    if len(expr) > _MAX_EXPR_CHARS:
        raise ValueError(f"expression too large: {len(expr)} > {_MAX_EXPR_CHARS}")
    tree = ast.parse(_normalize_expr(expr), mode="eval")
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > _MAX_AST_NODES:
        raise ValueError(f"expression AST too large: {node_count} > {_MAX_AST_NODES}")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in context:
                raise KeyError(f"unknown name in expression: {node.id}")
            return wrap(context[node.id])
        if isinstance(node, ast.Attribute):
            base = visit(node.value)
            return getattr(base, node.attr)
        if isinstance(node, ast.Subscript):
            base = visit(node.value)
            key = visit(node.slice)
            return base[key]
        if isinstance(node, ast.List):
            return [unwrap(visit(x)) for x in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(unwrap(visit(x)) for x in node.elts)
        if isinstance(node, ast.Dict):
            return {unwrap(visit(k)): unwrap(visit(v)) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(unwrap(visit(node.operand)))
        if isinstance(node, ast.BoolOp):
            # Preserve normal boolean short-circuit semantics. Besides matching
            # Python expectations, this lets guarded expressions safely inspect
            # optional state such as `x is not None and x.value == ...`.
            if isinstance(node.op, ast.And):
                for value in node.values:
                    if not bool(unwrap(visit(value))):
                        return False
                return True
            for value in node.values:
                if bool(unwrap(visit(value))):
                    return True
            return False
        if isinstance(node, ast.Compare):
            left = unwrap(visit(node.left))
            for op, comp in zip(node.ops, node.comparators):
                right = unwrap(visit(comp))
                fn = _ALLOWED_COMPARE.get(type(op))
                if fn is None:
                    raise ValueError(f"operator not allowed: {type(op).__name__}")
                if not fn(left, right):
                    return False
                left = right
            return True
        raise ValueError(f"expression node not allowed: {type(node).__name__}")

    return unwrap(visit(tree))


def resolve_path(expr: str, context: dict[str, Any]) -> Any:
    return safe_eval(expr.strip(), context)


def render(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if not isinstance(value, str):
        return value
    m = _TEMPLATE.fullmatch(value.strip())
    if m:
        return resolve_path(m.group(1), context)

    def repl(match):
        resolved = resolve_path(match.group(1), context)
        return str(resolved)

    return _TEMPLATE.sub(repl, value)
