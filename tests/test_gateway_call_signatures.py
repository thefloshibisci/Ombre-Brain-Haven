"""Guard the gateway debug-payload call site against signature drift.

``_build_injection_debug_payload`` gained a required keyword-only
``xinchao_context`` parameter during the xinchao adapter work, but its single
call site in ``prepare_payload`` was left unchanged. ``handle_chat`` always
requests the debug payload, so every ``/v1/chat/completions`` request raised
``TypeError`` and Starlette returned an opaque 500.
"""

from __future__ import annotations

import ast
import pathlib

GATEWAY = pathlib.Path(__file__).resolve().parent.parent / "gateway.py"


def _tree() -> ast.Module:
    return ast.parse(GATEWAY.read_text(encoding="utf-8"))


def _find_def(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is no longer defined in gateway.py")


def _find_calls(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def test_injection_debug_payload_call_sites_supply_every_required_argument() -> None:
    tree = _tree()
    fdef = _find_def(tree, "_build_injection_debug_payload")
    required = [
        arg.arg
        for arg, default in zip(fdef.args.kwonlyargs, fdef.args.kw_defaults)
        if default is None
    ]
    assert "xinchao_context" in required

    calls = _find_calls(tree, "_build_injection_debug_payload")
    assert calls, "expected at least one _build_injection_debug_payload call site"

    for call in calls:
        passed = {keyword.arg for keyword in call.keywords if keyword.arg}
        missing = sorted(name for name in required if name not in passed)
        assert not missing, (
            f"gateway.py line {call.lineno} omits {missing}; a debug-enabled chat "
            "request would raise TypeError and return 500"
        )


def test_prepare_payload_defines_the_xinchao_context_text_it_forwards() -> None:
    """The value passed at the call site must actually be bound in scope."""
    tree = _tree()
    prepare = _find_def(tree, "prepare_payload")
    assigned = {
        target.id
        for node in ast.walk(prepare)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "xinchao_context_text" in assigned
