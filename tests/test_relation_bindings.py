"""Relation ledger contract tests, adapted from P0luz test_relation_v1.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from bucket_manager import BucketManager
from relation_store import (
    normalize_relation_label,
    normalize_relation_links,
    normalize_relation_type,
    relation_display_label,
    relation_hint,
    reverse_relation_type,
)
from relation_bindings import attach, detach, restore
from relation_read import dispatch as relation_read


@pytest.fixture
async def bucket_mgr(tmp_path: Path):
    return BucketManager({"buckets_dir": str(tmp_path / "vault")})


@pytest.mark.parametrize(
    ("relation_type", "expected", "reverse"),
    [
        ("caused_by", "原因", "causes"),
        ("causes", "结果", "caused_by"),
        ("continuation_of", "前段", "continues"),
        ("continues", "后续", "continuation_of"),
        ("related_to", "相关", "related_to"),
        ("same_event", "同一事件", "same_event"),
    ],
)
def test_relation_fixed_display_labels_and_reverse_mapping(relation_type, expected, reverse):
    assert relation_display_label(relation_type, "") == expected
    assert reverse_relation_type(relation_type) == reverse


def test_relation_custom_display_and_reverse_are_symmetric():
    assert normalize_relation_type(" custom ") == "custom"
    assert reverse_relation_type("custom") == "custom"
    assert relation_display_label("custom", "同一次切换") == "同一次切换"


def test_relation_normalizers_fail_closed_for_malformed_metadata():
    assert normalize_relation_label(None) == ""
    assert normalize_relation_label("x" * 20) == "x" * 20
    assert normalize_relation_type(" Causes ") == "causes"
    with pytest.raises(ValueError):
        normalize_relation_type("custom.type-1")
    for value in (True, 1, [], {}):
        with pytest.raises(ValueError):
            normalize_relation_label(value)
        with pytest.raises(ValueError):
            normalize_relation_type(value)
    for malformed in (
        {"target_bucket_id": [], "type": "causes", "label": "", "status": "active"},
        {"target_bucket_id": "b", "type": {}, "label": "", "status": "active"},
        {"target_bucket_id": "b", "type": "causes", "label": True, "status": "active"},
        {"target_bucket_id": "b", "type": "causes", "label": "", "status": True},
        {"target_bucket_id": "b", "type": "causes", "label": "x\ny", "status": "active"},
        {"target_bucket_id": "b", "type": "causes", "label": "x" * 21, "status": "active"},
        {"target_bucket_id": "b", "type": "custom", "label": "", "status": "active"},
        {"target_bucket_id": "b", "type": "causes", "label": "", "status": "active", "relation_id": []},
    ):
        with pytest.raises(ValueError):
            normalize_relation_links([malformed])


def test_relation_hint_is_metadata_only_limited_and_reports_hidden_count():
    bucket = {"metadata": {"relation_links": [
        {"target_bucket_id": "b", "type": "caused_by", "label": "", "status": "active"},
        {"target_bucket_id": "c", "type": "custom", "label": "同一次切换", "status": "active"},
        {"target_bucket_id": "d", "type": "same_event", "label": "", "status": "active"},
        {"target_bucket_id": "e", "type": "causes", "label": "", "status": "detached"},
    ]}}
    hint = relation_hint(bucket)
    assert "原因" in hint and "同一次切换" in hint
    assert "同一事件" not in hint and hint.count("→") == 2
    assert "另有 1 条 relation" in hint


async def test_fixed_relation_attach_is_id_only_and_naturally_bidirectional(bucket_mgr):
    cause = await bucket_mgr.create("cause body", extra_metadata={"title": "Cause title"})
    effect = await bucket_mgr.create("effect body", extra_metadata={"title": "Effect title"})
    before_cause = await bucket_mgr.get(cause)
    before_effect = await bucket_mgr.get(effect)

    result = await attach(bucket_mgr, cause, effect, "causes")
    assert "slot=1" in result and "target_slot=1" in result and "relation_id=rel_" in result

    cause_links = (await bucket_mgr.get(cause))["metadata"]["relation_links"]
    effect_links = (await bucket_mgr.get(effect))["metadata"]["relation_links"]
    assert cause_links[0]["type"] == "causes"
    assert effect_links[0]["type"] == "caused_by"
    assert cause_links[0]["target_bucket_id"] == effect
    assert effect_links[0]["target_bucket_id"] == cause
    assert cause_links[0]["relation_id"] == effect_links[0]["relation_id"]
    assert cause_links[0]["label"] == effect_links[0]["label"] == ""

    manifest = await relation_read(bucket_mgr, cause)
    assert "active=1" in manifest and "type=causes" in manifest
    assert "Effect title" not in manifest and "effect body" not in manifest

    expanded = await relation_read(bucket_mgr, cause, include_titles=True)
    assert "title=Effect title" in expanded and "effect body" not in expanded

    after_cause = await bucket_mgr.get(cause)
    after_effect = await bucket_mgr.get(effect)
    for key in ("last_active", "activation_count", "importance", "tags", "domain", "created"):
        assert after_cause["metadata"].get(key) == before_cause["metadata"].get(key)
        assert after_effect["metadata"].get(key) == before_effect["metadata"].get(key)


async def test_expected_title_is_optional_guard_not_identity_key(bucket_mgr):
    a = await bucket_mgr.create("A", extra_metadata={"title": "A title"})
    b = await bucket_mgr.create("B", extra_metadata={"title": "B title"})

    assert "status=active" in await attach(bucket_mgr, a, b, "related_to")
    assert "active=1" in await relation_read(bucket_mgr, a)
    assert "标题不匹配" in await relation_read(bucket_mgr, a, expected_title="wrong")

    c = await bucket_mgr.create("C", extra_metadata={"title": "C title"})
    assert "标题不匹配" in await attach(bucket_mgr, a, c, "same_event", expected_title="wrong")
    assert "relation_links" not in (await bucket_mgr.get(c))["metadata"]


async def test_custom_relation_uses_forward_and_reverse_labels(bucket_mgr):
    a = await bucket_mgr.create("A", extra_metadata={"title": "A"})
    b = await bucket_mgr.create("B", extra_metadata={"title": "B"})

    result = await attach(bucket_mgr, a, b, "custom", label="启发了", reverse_label="受启发于")
    assert "status=active" in result
    a_link = (await bucket_mgr.get(a))["metadata"]["relation_links"][0]
    b_link = (await bucket_mgr.get(b))["metadata"]["relation_links"][0]
    assert a_link["type"] == b_link["type"] == "custom"
    assert a_link["label"] == "启发了"
    assert b_link["label"] == "受启发于"

    c = await bucket_mgr.create("C", extra_metadata={"title": "C"})
    assert "status=active" in await attach(bucket_mgr, a, c, "custom", label="同一组实验")
    c_link = (await bucket_mgr.get(c))["metadata"]["relation_links"][0]
    assert c_link["label"] == "同一组实验"


async def test_fixed_types_reject_labels_and_custom_requires_label(bucket_mgr):
    a = await bucket_mgr.create("A", extra_metadata={"title": "A"})
    b = await bucket_mgr.create("B", extra_metadata={"title": "B"})

    assert "固定六种" in await attach(bucket_mgr, a, b, "causes", label="extra")
    assert "固定六种" in await attach(bucket_mgr, a, b, "causes", reverse_label="extra")
    assert "必须填 label" in await attach(bucket_mgr, a, b, "custom")
    assert "relation_links" not in (await bucket_mgr.get(a))["metadata"]
    assert "relation_links" not in (await bucket_mgr.get(b))["metadata"]


async def test_detach_restore_updates_both_mirrors_and_read_hides_detached_by_default(bucket_mgr):
    a = await bucket_mgr.create("A", extra_metadata={"title": "A"})
    b = await bucket_mgr.create("B", extra_metadata={"title": "B"})

    await attach(bucket_mgr, a, b, "continuation_of")
    assert "status=detached" in await detach(bucket_mgr, a, 1)
    a_link = (await bucket_mgr.get(a))["metadata"]["relation_links"][0]
    b_link = (await bucket_mgr.get(b))["metadata"]["relation_links"][0]
    assert a_link["status"] == b_link["status"] == "detached"

    compact = await relation_read(bucket_mgr, a)
    assert "active=0 | detached=1" in compact and "slot=1" not in compact
    history = await relation_read(bucket_mgr, a, include_detached=True)
    assert "slot=1" in history and "detached" in history

    assert "status=active" in await restore(bucket_mgr, b, 1)
    a_link = (await bucket_mgr.get(a))["metadata"]["relation_links"][0]
    b_link = (await bucket_mgr.get(b))["metadata"]["relation_links"][0]
    assert a_link["status"] == b_link["status"] == "active"


async def test_legacy_one_way_relation_remains_readable_and_locally_reversible(bucket_mgr):
    source = await bucket_mgr.create("source", extra_metadata={"title": "source"})
    target = await bucket_mgr.create("target", extra_metadata={"title": "target"})
    await bucket_mgr.mutate_relation_links(
        source,
        lambda post: (
            True,
            post.__setitem__("relation_links", [{
                "target_bucket_id": target,
                "type": "related_to",
                "label": "",
                "status": "active",
            }]) or "seed",
        ),
    )

    assert "slot=1" in await relation_read(bucket_mgr, source)
    assert "legacy=true" in await detach(bucket_mgr, source, 1)
    assert (await bucket_mgr.get(source))["metadata"]["relation_links"][0]["status"] == "detached"
    assert "relation_links" not in (await bucket_mgr.get(target))["metadata"]
    assert "legacy=true" in await restore(bucket_mgr, source, 1)


async def test_relation_rejects_self_and_special_buckets_but_allows_archived_ordinary(bucket_mgr):
    ordinary = await bucket_mgr.create("ordinary", extra_metadata={"title": "ordinary"})
    target = await bucket_mgr.create("target", extra_metadata={"title": "target"})
    feel = await bucket_mgr.create("feel", bucket_type="feel", extra_metadata={"title": "feel"})

    assert "必须是字符串" in await attach(bucket_mgr, ordinary, [], "causes")
    assert "自环" in await attach(bucket_mgr, ordinary, ordinary, "causes")
    result = await attach(bucket_mgr, ordinary, feel, "related_to")
    assert "Relation" in result
    assert "slot=" not in result and "status=" not in result

    await bucket_mgr.archive(ordinary)
    archived = await bucket_mgr.get(ordinary)
    assert archived["metadata"].get("type") == "archived"
    assert "slot=1" in await attach(bucket_mgr, ordinary, target, "related_to")
    assert "slot=1" in await relation_read(bucket_mgr, ordinary)
    assert "slot=1" in await relation_read(bucket_mgr, target)


@pytest.mark.parametrize("value", ["custom.type", "next", "related-to", "causes!", "same_event_1"])
def test_relation_type_rejects_safe_but_unsupported_values(value):
    with pytest.raises(ValueError):
        normalize_relation_type(value)


@pytest.mark.parametrize("value", ["\n", " \r\n ", "x" * 21])
def test_relation_label_rejects_raw_newlines_and_overlong_values(value):
    with pytest.raises(ValueError):
        normalize_relation_label(value)


async def test_archived_feel_bucket_becomes_relation_eligible_haven_limitation(bucket_mgr):
    # Haven's archive() overwrites type to "archived" without preserving the
    # original kind (no footprint_snapshot).  This differs from P0luz, where
    # archived special buckets stay special.  The safe Haven adaptation is to
    # treat all archived buckets as relation-eligible after archiving.
    source = await bucket_mgr.create("source", extra_metadata={"title": "source"})
    feel = await bucket_mgr.create("feel", bucket_type="feel", extra_metadata={"title": "feel"})
    assert "只允许普通记忆桶" in await attach(bucket_mgr, feel, source, "causes")

    await bucket_mgr.archive(feel)
    # After archiving, Haven no longer knows this was a feel bucket.
    assert "slot=1" in await attach(bucket_mgr, feel, source, "related_to")


async def test_source_and_target_active_limits_are_checked_before_pair_write(bucket_mgr):
    source = await bucket_mgr.create("source", extra_metadata={"title": "source"})
    targets = [await bucket_mgr.create(f"target-{i}", extra_metadata={"title": f"target-{i}"}) for i in range(17)]
    for target in targets[:16]:
        assert "status=active" in await attach(bucket_mgr, source, target, "related_to")
    rejected = await attach(bucket_mgr, source, targets[16], "related_to")
    assert "16" in rejected and "拒绝" in rejected
    assert "relation_links" not in (await bucket_mgr.get(targets[16]))["metadata"]
