import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from mcp.server.fastmcp import FastMCP


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = (REPO_ROOT / "server.py").read_text(encoding="utf-8-sig")
DASHBOARD_SOURCE = (REPO_ROOT / "dashboard.html").read_text(encoding="utf-8")

RELATION_TYPES = [
    "caused_by",
    "causes",
    "continuation_of",
    "continues",
    "related_to",
    "same_event",
    "custom",
]


def _server_tree() -> ast.Module:
    return ast.parse(SERVER_SOURCE, filename=str(REPO_ROOT / "server.py"))


def _function_source(name: str) -> str:
    tree = _server_tree()
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name
    ]
    assert functions, f"server.py must define {name}"
    return ast.get_source_segment(SERVER_SOURCE, functions[0]) or ""


def _assignment_values(target_name: str) -> list[str]:
    tree = _server_tree()
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        if targets[0].id != target_name:
            continue
        if isinstance(value, ast.Subscript) and isinstance(value.slice, (ast.Tuple, ast.List)):
            elements = value.slice.elts
        elif isinstance(value, (ast.Tuple, ast.List)):
            elements = value.elts
        else:
            raise AssertionError(f"{target_name} must use literal values")
        for element in elements:
            assert isinstance(element, ast.Constant)
            values.append(element.value)
    return values


def test_relation_attach_uses_the_p0luz_literal_schema():
    assert _assignment_values("RelationType") == RELATION_TYPES
    source = _function_source("relation_attach")
    assert "relation_type: RelationType" in source
    assert "RelationType = Literal[" in SERVER_SOURCE


def test_relation_tools_publish_stable_slot_and_legacy_semantics():
    attach = _function_source("relation_attach")
    assert "bucket_id -> target_bucket_id" in attach
    assert all(name in attach for name in RELATION_TYPES)
    assert "reverse_label" in attach
    assert "自动" in attach and "反向" in attach
    assert "custom" in attach and "label" in attach

    read = _function_source("relation_read")
    assert "include_detached=True" in read
    assert "include_titles=True" in read
    assert "不读取目标正文" in read
    assert "relation_slot" in read

    detach = _function_source("relation_detach")
    assert "不删除关系历史" in detach
    assert "relation_id" in detach
    assert "relation_slot" in detach

    restore = _function_source("relation_restore")
    assert "relation_id" in restore
    assert "archived" in restore


@pytest.mark.parametrize(
    "tool_name",
    [
        "source_read",
        "source_attach",
        "source_detach",
        "source_restore",
        "relation_read",
        "relation_attach",
        "relation_detach",
        "relation_restore",
        "letter_write",
        "letter_lock_update",
        "letter_read",
        "profile_fact",
        "reminder_create",
        "reminder_list",
        "reminder_update",
        "darkroom_enter",
        "darkroom_rooms",
        "darkroom_delete",
        "darkroom_view",
        "entity_edge_backfill",
        "introspection",
    ],
)
def test_haven_specific_tools_remain_registered(tool_name):
    decorator = f"@mcp.tool()\nasync def {tool_name}("
    assert decorator in SERVER_SOURCE


def test_relation_tool_parameters_remain_backward_compatible():
    expected = {
        "relation_read": ["bucket_id", "expected_title", "include_titles", "include_detached"],
        "relation_attach": ["bucket_id", "target_bucket_id", "relation_type", "expected_title", "label", "reverse_label"],
        "relation_detach": ["bucket_id", "relation_slot", "expected_title"],
        "relation_restore": ["bucket_id", "relation_slot", "expected_title"],
    }
    for name, parameter_names in expected.items():
        tree = _server_tree()
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
        assert [argument.arg for argument in function.args.args] == parameter_names


def test_bucket_read_payload_adds_display_and_raw_content_without_changing_content():
    namespace = {
        "strip_wikilinks": lambda value: value.replace("[[原链接]]", "原链接"),
        "normalize_memory_metadata": lambda bucket: {},
        "decay_engine": SimpleNamespace(calculate_score=lambda metadata: 1.25),
    }
    exec(_function_source("_bucket_read_payload"), namespace)
    payload = namespace["_bucket_read_payload"]({"id": "bucket", "content": "[[原链接]] 正文"})
    assert payload["content"] == "原链接 正文"
    assert payload["display_content"] == payload["content"]
    assert payload["raw_content"] == "[[原链接]] 正文"


def test_dashboard_displays_sanitized_content_and_edits_raw_content():
    assert "b.display_content || b.content" in DASHBOARD_SOURCE
    detail_view = "esc(b.display_content || b.content || '')"
    assert detail_view in DASHBOARD_SOURCE
    assert "b.raw_content || b.content" in DASHBOARD_SOURCE
    editor = "esc(b.raw_content || b.content || '')"
    assert editor in DASHBOARD_SOURCE
    assert "<textarea" not in editor


def test_literal_annotation_generates_a_fixed_enum_schema():
    mcp = FastMCP("schema contract")

    @mcp.tool()
    async def relation_attach(relation_type: Literal[*RELATION_TYPES]) -> str:
        """probe"""

    validated = mcp._tool_manager._tools["relation_attach"]
    assert validated.parameters["properties"]["relation_type"]["enum"] == RELATION_TYPES
