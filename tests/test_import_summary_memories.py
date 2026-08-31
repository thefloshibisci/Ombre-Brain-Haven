"""Contract tests for scripts/import_summary_memories.py (summary-only import)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import import_summary_memories as importer  # noqa: E402
from memory_metadata import CANONICAL_DOMAINS  # noqa: E402


def _artifact(tmp_path: Path, items: list[dict]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps({"ok": True, "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _row(**overrides) -> dict:
    row = {
        "legacy_summary_id": "11111111-2222-3333-4444-555555555555",
        "legacy_content": "严槿和南枳一起把记忆库部署到服务器上，调试了接口报错。",
        "legacy_summary_hash": "deadbeef",
        "legacy_review_status": "backlog",
        "created_at": "2026-08-01T04:00:00+00:00",
    }
    row.update(overrides)
    return row


class TestDomainClassification:
    def test_only_canonical_domain_keys_are_emitted(self):
        contents = [
            "严槿和南枳讨论部署 docker 和 api 报错",
            "严槿说爱你，两人约会并承诺一辈子",
            "严槿焦虑崩溃，哭了很久后开始反思",
            "严槿吃了外卖又熬夜打游戏通关",
            "两人讨论论文和申论考试的备考计划",
            "亲密的身体接触与欲望的具象描述",
            "完全无关的一句普通陈述",
        ]
        for content in contents:
            for domain in importer.classify_domains(content):
                assert domain in CANONICAL_DOMAINS

    def test_unmatched_content_falls_back_to_general(self):
        assert importer.classify_domains("嗯。") == [importer.DEFAULT_DOMAIN]

    def test_at_most_two_domains(self):
        content = "严槿爱南枳，两人讨论 docker 部署、论文计划、失眠和焦虑"
        assert len(importer.classify_domains(content)) <= importer.MAX_DOMAINS

    def test_specific_domain_wins_tie_over_broad_domain(self):
        # One intimacy hit and one life hit: intimacy is more specific.
        domains = importer.classify_domains("触手，吃")
        assert domains[0] == "intimacy"


class TestTitle:
    def test_title_absorbs_clauses_until_meaningful(self):
        title = importer.build_title("严槿早上醒来，询问南枳是否能调用记忆库，南枳确认可以。")
        assert len(title) >= importer.TITLE_MIN_CHARS
        assert "记忆库" in title

    def test_title_respects_max_length(self):
        title = importer.build_title("啊" * 200)
        assert len(title) <= importer.TITLE_MAX_CHARS

    def test_title_never_exceeds_max_when_absorbing(self):
        content = "、".join(["短句"] * 40)
        assert len(importer.build_title(content)) <= importer.TITLE_MAX_CHARS

    def test_empty_content_is_rejected(self):
        with pytest.raises(importer.PlanError):
            importer.build_title("   ")


class TestTimestamps:
    def test_utc_is_converted_to_local_offset(self):
        assert importer.to_local_iso("2026-08-01T04:00:00+00:00") == "2026-08-01T12:00:00+08:00"

    def test_naive_timestamp_is_treated_as_utc(self):
        assert importer.to_local_iso("2026-08-01T04:00:00") == "2026-08-01T12:00:00+08:00"

    def test_empty_timestamp_is_rejected(self):
        with pytest.raises(importer.PlanError):
            importer.to_local_iso("")


class TestPlan:
    def test_plan_is_deterministic_and_idempotent(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        first = importer.build_plan(artifact_path=artifact, windows_path=None)
        second = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert first["items"] == second["items"]

    def test_bucket_id_is_derived_from_legacy_id(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        item = plan["items"][0]
        assert item["bucket_id"] == f"legacy-{item['legacy_summary_id']}"
        assert importer.MEMORY_ID_RE.fullmatch(item["bucket_id"])

    def test_content_is_preserved_verbatim(self, tmp_path):
        content = "严槿和南枳一起修好了部署脚本。"
        artifact = _artifact(tmp_path, [_row(legacy_content=content)])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert plan["items"][0]["content"] == content

    def test_duplicate_legacy_ids_are_reported_not_imported(self, tmp_path):
        artifact = _artifact(tmp_path, [_row(), _row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert plan["ok"] is False
        assert len(plan["items"]) == 1
        assert any("duplicate" in error for error in plan["errors"])

    def test_empty_content_row_is_skipped_with_error(self, tmp_path):
        artifact = _artifact(tmp_path, [_row(legacy_content="  ")])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert plan["ok"] is False
        assert plan["items"] == []

    def test_former_name_mentions_are_tagged(self, tmp_path):
        artifact = _artifact(
            tmp_path, [_row(legacy_content="陆沉向严槿保证永远不会真的生气。")]
        )
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        item = plan["items"][0]
        assert item["mentions_former_name"] is True
        assert importer.FORMER_NAME_TAG in item["tags"]

    def test_current_name_only_rows_are_not_tagged_with_former_name(self, tmp_path):
        artifact = _artifact(tmp_path, [_row(legacy_content="南枳和严槿一起看电影。")])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert importer.FORMER_NAME_TAG not in plan["items"][0]["tags"]

    def test_initial_backfill_window_marks_time_as_uncertain(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        windows = tmp_path / "windows.json"
        windows.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "legacy_summary_id": _row()["legacy_summary_id"],
                            "window_kind": "initial_backfill",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        plan = importer.build_plan(artifact_path=artifact, windows_path=windows)
        assert importer.UNCERTAIN_TIME_TAG in plan["items"][0]["tags"]

    def test_cron_window_does_not_mark_time_as_uncertain(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        windows = tmp_path / "windows.json"
        windows.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "legacy_summary_id": _row()["legacy_summary_id"],
                            "window_kind": "cron_interval",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        plan = importer.build_plan(artifact_path=artifact, windows_path=windows)
        assert importer.UNCERTAIN_TIME_TAG not in plan["items"][0]["tags"]

    def test_candidate_rows_get_higher_importance(self, tmp_path):
        artifact = _artifact(tmp_path, [_row(legacy_review_status="candidate")])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert plan["items"][0]["importance"] == importer.CANDIDATE_IMPORTANCE

    def test_every_row_carries_the_base_legacy_tag(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert importer.BASE_TAG in plan["items"][0]["tags"]

    def test_all_timestamps_use_local_offset(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        item = importer.build_plan(artifact_path=artifact, windows_path=None)["items"][0]
        assert item["created"] == item["last_active"] == item["updated_at"]
        assert item["created"].endswith("+08:00")


class TestPayload:
    def test_payload_only_contains_fields_the_api_accepts(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        item = importer.build_plan(artifact_path=artifact, windows_path=None)["items"][0]
        payload = importer.payload_for(item)
        assert set(payload) == {
            "id",
            "title",
            "content",
            "type",
            "domain",
            "tags",
            "importance",
            "created",
            "last_active",
            "updated_at",
        }
        assert payload["type"] == "dynamic"


class TestApplyGuards:
    def test_apply_refuses_a_plan_that_is_not_ok(self, tmp_path):
        with pytest.raises(importer.PlanError):
            importer.apply_plan(
                plan={"ok": False, "items": []},
                base_url="http://localhost",
                token="t",
                state_path=tmp_path / "state.jsonl",
                limit=0,
                sleep_seconds=0,
                timeout=1,
                max_retries=1,
                max_consecutive_failures=1,
                dry_run=True,
            )

    def test_dry_run_performs_no_writes(self, tmp_path, monkeypatch):
        def explode(**kwargs):
            raise AssertionError("dry run must not POST")

        monkeypatch.setattr(importer, "post_memory", explode)
        state = tmp_path / "state.jsonl"
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        report = importer.apply_plan(
            plan=plan,
            base_url="http://localhost",
            token="t",
            state_path=state,
            limit=0,
            sleep_seconds=0,
            timeout=1,
            max_retries=1,
            max_consecutive_failures=1,
            dry_run=True,
        )
        assert report["dry_run"] is True
        assert not state.exists()

    def test_already_imported_rows_are_skipped_on_resume(self, tmp_path, monkeypatch):
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        state = tmp_path / "state.jsonl"
        importer.append_state(
            state, {"bucket_id": plan["items"][0]["bucket_id"], "status": "created"}
        )

        def explode(**kwargs):
            raise AssertionError("must not re-POST an imported row")

        monkeypatch.setattr(importer, "post_memory", explode)
        report = importer.apply_plan(
            plan=plan,
            base_url="http://localhost",
            token="t",
            state_path=state,
            limit=0,
            sleep_seconds=0,
            timeout=1,
            max_retries=1,
            max_consecutive_failures=1,
            dry_run=False,
        )
        assert report["attempted"] == 0

    def test_failed_rows_are_retried_on_resume(self, tmp_path, monkeypatch):
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        state = tmp_path / "state.jsonl"
        importer.append_state(
            state, {"bucket_id": plan["items"][0]["bucket_id"], "status": "failed"}
        )
        calls: list[dict] = []

        def fake_post(*, base_url, token, payload, timeout):
            calls.append(payload)
            return 200, {"status": "created", "id": payload["id"]}

        monkeypatch.setattr(importer, "post_memory", fake_post)
        report = importer.apply_plan(
            plan=plan,
            base_url="http://localhost",
            token="t",
            state_path=state,
            limit=0,
            sleep_seconds=0,
            timeout=1,
            max_retries=1,
            max_consecutive_failures=1,
            dry_run=False,
        )
        assert len(calls) == 1
        assert report["created"] == 1

    def test_apply_aborts_after_consecutive_failures(self, tmp_path, monkeypatch):
        rows = [
            _row(legacy_summary_id=f"1111{index:04d}-2222-3333-4444-555555555555")
            for index in range(6)
        ]
        artifact = _artifact(tmp_path, rows)
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert plan["ok"] is True
        attempts: list[str] = []

        def fake_post(*, base_url, token, payload, timeout):
            attempts.append(payload["id"])
            return 500, {"error": "boom"}

        monkeypatch.setattr(importer, "post_memory", fake_post)
        report = importer.apply_plan(
            plan=plan,
            base_url="http://localhost",
            token="t",
            state_path=tmp_path / "state.jsonl",
            limit=0,
            sleep_seconds=0,
            timeout=1,
            max_retries=1,
            max_consecutive_failures=2,
            dry_run=False,
        )
        assert report["ok"] is False
        assert len(attempts) == 2

    def test_http_400_is_not_retried(self, tmp_path, monkeypatch):
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        attempts: list[str] = []

        def fake_post(*, base_url, token, payload, timeout):
            attempts.append(payload["id"])
            return 400, {"error": "missing title"}

        monkeypatch.setattr(importer, "post_memory", fake_post)
        importer.apply_plan(
            plan=plan,
            base_url="http://localhost",
            token="t",
            state_path=tmp_path / "state.jsonl",
            limit=0,
            sleep_seconds=0,
            timeout=1,
            max_retries=3,
            max_consecutive_failures=5,
            dry_run=False,
        )
        assert len(attempts) == 1

    def test_growing_plan_only_writes_the_new_rows(self, tmp_path, monkeypatch):
        """The legacy cron keeps producing summaries, so plans grow between runs.

        Re-planning a superset and re-running apply against the same state file
        must POST only the rows that were not imported before.
        """
        old_rows = [
            _row(legacy_summary_id=f"3333{index:04d}-2222-3333-4444-555555555555")
            for index in range(4)
        ]
        old_plan = importer.build_plan(
            artifact_path=_artifact(tmp_path / "old", old_rows), windows_path=None
        )
        state = tmp_path / "state.jsonl"
        for item in old_plan["items"]:
            importer.append_state(
                state, {"bucket_id": item["bucket_id"], "status": "created"}
            )

        new_rows = old_rows + [
            _row(legacy_summary_id=f"4444{index:04d}-2222-3333-4444-555555555555")
            for index in range(3)
        ]
        new_plan = importer.build_plan(
            artifact_path=_artifact(tmp_path / "new", new_rows), windows_path=None
        )
        assert new_plan["ok"] is True
        assert len(new_plan["items"]) == 7

        posted: list[str] = []

        def fake_post(*, base_url, token, payload, timeout):
            posted.append(payload["id"])
            return 200, {"status": "created", "id": payload["id"]}

        monkeypatch.setattr(importer, "post_memory", fake_post)
        report = importer.apply_plan(
            plan=new_plan,
            base_url="http://localhost",
            token="t",
            state_path=state,
            limit=0,
            sleep_seconds=0,
            timeout=1,
            max_retries=1,
            max_consecutive_failures=5,
            dry_run=False,
        )

        assert report["attempted"] == 3
        assert all(bucket_id.startswith("legacy-4444") for bucket_id in posted)

    def test_bucket_ids_are_stable_across_replanning(self, tmp_path):
        """A row's bucket id must not depend on its position in the export."""
        rows = [
            _row(legacy_summary_id=f"5555{index:04d}-2222-3333-4444-555555555555")
            for index in range(3)
        ]
        first = importer.build_plan(
            artifact_path=_artifact(tmp_path / "a", rows), windows_path=None
        )
        reordered = importer.build_plan(
            artifact_path=_artifact(tmp_path / "b", list(reversed(rows))),
            windows_path=None,
        )
        by_legacy = {item["legacy_summary_id"]: item["bucket_id"] for item in first["items"]}
        for item in reordered["items"]:
            assert by_legacy[item["legacy_summary_id"]] == item["bucket_id"]

    def test_limit_bounds_the_batch(self, tmp_path, monkeypatch):
        rows = [
            _row(legacy_summary_id=f"2222{index:04d}-2222-3333-4444-555555555555")
            for index in range(5)
        ]
        artifact = _artifact(tmp_path, rows)
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        calls: list[str] = []

        def fake_post(*, base_url, token, payload, timeout):
            calls.append(payload["id"])
            return 200, {"status": "created", "id": payload["id"]}

        monkeypatch.setattr(importer, "post_memory", fake_post)
        importer.apply_plan(
            plan=plan,
            base_url="http://localhost",
            token="t",
            state_path=tmp_path / "state.jsonl",
            limit=2,
            sleep_seconds=0,
            timeout=1,
            max_retries=1,
            max_consecutive_failures=5,
            dry_run=False,
        )
        assert len(calls) == 2
class TestMissingCreatedAt:
    def test_null_created_at_is_rejected_by_default(self, tmp_path):
        artifact = _artifact(tmp_path, [_row(created_at="")])
        plan = importer.build_plan(artifact_path=artifact, windows_path=None)
        assert plan["ok"] is False
        assert plan["items"] == []
        assert any("bad created_at" in err for err in plan["errors"])

    def test_explicit_substitute_admits_the_row_and_tags_it(self, tmp_path):
        artifact = _artifact(tmp_path, [_row(created_at="")])
        plan = importer.build_plan(
            artifact_path=artifact,
            windows_path=None,
            missing_created_at="2026-08-27T18:01:05+00:00",
        )
        assert plan["ok"] is True
        item = plan["items"][0]
        assert item["created"] == "2026-08-28T02:01:05+08:00"
        assert item["last_active"] == item["created"]
        assert item["source_time_missing"] is True
        assert importer.UNCERTAIN_TIME_TAG in item["tags"]
        assert importer.MISSING_TIME_TAG in item["tags"]
        assert plan["stats"]["missing_time_rows"] == 1
        assert plan["source"]["missing_created_at"] == "2026-08-28T02:01:05+08:00"

    def test_substitute_does_not_touch_rows_that_have_a_time(self, tmp_path):
        artifact = _artifact(tmp_path, [_row()])
        plan = importer.build_plan(
            artifact_path=artifact,
            windows_path=None,
            missing_created_at="2026-08-27T18:01:05+00:00",
        )
        item = plan["items"][0]
        assert item["created"] == "2026-08-01T12:00:00+08:00"
        assert item["source_time_missing"] is False
        assert importer.MISSING_TIME_TAG not in item["tags"]
        assert plan["stats"]["missing_time_rows"] == 0
