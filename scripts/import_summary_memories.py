#!/usr/bin/env python3
"""Import legacy Supabase summaries into Haven as memory buckets.

Summary-only migration: the raw conversation archive stays a read-only cold
archive and is never embedded. This tool turns each legacy summary into one
deterministic bucket payload for ``POST /api/memories``.

Subcommands:
    plan    Build a deterministic, offline import plan from the artifact.
    apply   POST the plan to a target Haven instance, resumable.
    verify  Re-read a sample of imported buckets from the target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

PLAN_SCHEMA_VERSION = "ombre-summary-import-plan-v1"
STATE_SCHEMA_VERSION = "ombre-summary-import-state-v1"

BUCKET_ID_PREFIX = "legacy-"
MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

LOCAL_TZ = timezone(timedelta(hours=8))

BASE_TAG = "legacy_summary"
FORMER_NAME_TAG = "曾用名"
UNCERTAIN_TIME_TAG = "时间存疑"
MISSING_TIME_TAG = "时间缺失"

FORMER_AI_NAME = "陆沉"

TITLE_MAX_CHARS = 34
TITLE_MIN_CHARS = 14
TITLE_SPLIT_RE = re.compile(r"[，。！？；\n\r]")

# Canonical domain keys come from memory_metadata.DOMAIN_LABELS; the older 22-domain
# Chinese table in reclassify_domains.py predates this taxonomy and is not used here.
DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "intimacy": {
        "亲密", "身体", "欲望", "触手", "性幻想", "色色", "情趣", "接吻", "亲吻",
        "舔", "咬", "喘", "锁骨", "体温", "同床", "上床", "调情", "撩", "裸",
        "抱住", "抱着", "怀里", "贴", "填满", "收缩", "喜欢被", "感官",
    },
    "relationship": {
        "爱你", "我爱", "爱意", "想你", "承诺", "一辈子", "永远", "五十年",
        "老公", "老婆", "男友", "女友", "恋爱", "恋人", "伴侣", "约会", "分手",
        "暧昧", "在一起", "结婚", "戒指", "身高差", "称呼", "撒娇", "哄",
        "吵架", "生气", "闹", "边界", "暗号", "意象", "陪伴", "依赖",
        "关系", "身份认证", "正缘", "撞名", "曾用名", "改名", "表白", "亲爱",
    },
    "inner": {
        "焦虑", "抑郁", "崩溃", "情绪", "难过", "哭", "眼泪", "泪", "孤独",
        "伤心", "委屈", "感动", "开心", "反思", "自省", "觉得自己", "心理",
        "创伤", "人格", "压力", "恐惧", "害怕", "迷茫", "自我认同", "存在",
        "意识", "自由意志", "安全感", "自残", "自我", "认同", "共鸣", "珍视",
        "舍不得", "想不通", "失落", "释然",
    },
    "life": {
        "吃", "饭", "做饭", "外卖", "奶茶", "咖啡", "零食", "水果", "超市",
        "睡", "失眠", "熬夜", "做梦", "噩梦", "作息", "医院", "吃药", "复查",
        "抽血", "生病", "月经", "天气", "出门", "快递", "邮戳", "明信片",
        "妈", "爸", "家里", "家人", "弟弟", "姐姐", "朋友", "闺蜜",
        "钱", "转账", "生活费", "花了", "买", "游戏", "通关", "存档",
        "电影", "番剧", "动漫", "综艺", "小说", "漫画", "书", "音乐", "童谣",
        "旅行", "瑞士", "海拔",
    },
    "tech": {
        "代码", "python", "bug", "api", "docker", "git", "部署", "调试",
        "服务器", "server", "数据库", "supabase", "向量", "embedding",
        "模型", "mcp", "token", "prompt", "llm", "claude", "gemini",
        "deepseek", "记忆库", "记忆系统", "脚本", "正则", "编译", "apk",
        "迁移", "插件", "zeabur", "报错", "修复", "字段", "接口", "配置",
        "重启", "日志", "缓存", "分词", "jieba", "librosa", "频谱",
        "定时任务", "窗口id", "隧道", "vpn", "代理", "域名", "cloudflare",
        "开发", "功能", "版本", "备份", "同步", "写入", "读取", "检索",
    },
    "project": {
        "项目", "论文", "考试", "申论", "作业", "选课", "学分", "教授",
        "工作", "面试", "求职", "会议", "汇报", "同事", "老板", "薪资",
        "计划", "方案", "规划", "创作", "预设", "人设卡", "同人", "脚本创作",
        "sillytavern", "破甲词", "眷恋", "花园", "eryu", "打包盒",
    },
}

# Tie-break order when two domains score equally: more specific domains win.
DOMAIN_PRIORITY = ["intimacy", "relationship", "tech", "project", "inner", "life"]

MAX_DOMAINS = 2
DEFAULT_DOMAIN = "general"

DEFAULT_IMPORTANCE = 5
CANDIDATE_IMPORTANCE = 6


class PlanError(RuntimeError):
    """Raised when the plan cannot be built or is internally inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_for_hash(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def to_local_iso(value: str) -> str:
    """Convert a source timestamp to the local ISO form Haven writes itself."""
    raw = str(value or "").strip()
    if not raw:
        raise PlanError("empty timestamp")
    candidate = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ).isoformat(timespec="seconds")


def build_title(content: str) -> str:
    text = re.sub(r"\s+", " ", (content or "").strip())
    if not text:
        raise PlanError("empty content cannot produce a title")
    segments = [seg.strip() for seg in TITLE_SPLIT_RE.split(text) if seg.strip()]
    if not segments:
        segments = [text]
    title = segments[0]
    index = 1
    # A single leading clause is often a bare subject ("严槿早上醒来"), so keep
    # absorbing clauses until the title carries the actual subject matter.
    while len(title) < TITLE_MIN_CHARS and index < len(segments):
        candidate = f"{title}，{segments[index]}"
        if len(candidate) > TITLE_MAX_CHARS:
            break
        title = candidate
        index += 1
    if len(title) > TITLE_MAX_CHARS:
        title = title[:TITLE_MAX_CHARS].rstrip("，、 ")
    return title or text[:TITLE_MAX_CHARS]


def classify_domains(content: str) -> list[str]:
    """Score canonical domains by keyword hits; ties break toward specificity."""
    haystack = (content or "").lower()
    scored: list[tuple[int, int, str]] = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword.lower() in haystack)
        if hits:
            scored.append((-hits, DOMAIN_PRIORITY.index(domain), domain))
    if not scored:
        return [DEFAULT_DOMAIN]
    scored.sort()
    return [domain for _, _, domain in scored[:MAX_DOMAINS]]


def build_tags(content: str, window_kind: str, *, time_missing: bool = False) -> list[str]:
    tags = [BASE_TAG]
    if FORMER_AI_NAME in (content or ""):
        tags.append(FORMER_NAME_TAG)
    if window_kind == "initial_backfill" or time_missing:
        tags.append(UNCERTAIN_TIME_TAG)
    if time_missing:
        tags.append(MISSING_TIME_TAG)
    return tags


def load_windows(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items") or []
    return {str(row.get("legacy_summary_id")): row for row in rows}


def build_plan(
    *,
    artifact_path: Path,
    windows_path: Path | None,
    missing_created_at: str | None = None,
) -> dict[str, Any]:
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise PlanError(f"artifact has no items: {artifact_path}")

    # A source row may carry a NULL created_at. Rather than inventing a
    # conversation time, the caller must opt in to an explicit substitute; the
    # row is then tagged so the substitution stays searchable.
    fallback_created = to_local_iso(missing_created_at) if missing_created_at else ""

    windows = load_windows(windows_path)
    plan_items: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    content_hashes: dict[str, list[str]] = {}

    for row in items:
        legacy_id = str(row.get("legacy_summary_id") or "").strip()
        content = str(row.get("legacy_content") or "").strip()
        if not legacy_id:
            errors.append("row without legacy_summary_id")
            continue
        if legacy_id in seen_ids:
            errors.append(f"duplicate legacy_summary_id: {legacy_id}")
            continue
        seen_ids.add(legacy_id)
        if not content:
            errors.append(f"empty legacy_content: {legacy_id}")
            continue

        bucket_id = f"{BUCKET_ID_PREFIX}{legacy_id}"
        if not MEMORY_ID_RE.fullmatch(bucket_id):
            errors.append(f"bucket id rejected by MEMORY_ID_RE: {bucket_id}")
            continue

        window = windows.get(legacy_id) or {}
        window_kind = str(window.get("window_kind") or "")

        time_missing = not str(row.get("created_at") or "").strip()
        if time_missing and fallback_created:
            created = fallback_created
        else:
            try:
                created = to_local_iso(row.get("created_at"))
            except (PlanError, ValueError) as exc:
                errors.append(f"bad created_at for {legacy_id}: {exc}")
                continue

        try:
            title = build_title(content)
        except PlanError as exc:
            errors.append(f"bad title for {legacy_id}: {exc}")
            continue

        legacy_status = str(row.get("legacy_review_status") or "")
        importance = CANDIDATE_IMPORTANCE if legacy_status == "candidate" else DEFAULT_IMPORTANCE

        content_hashes.setdefault(
            hashlib.sha256(normalize_for_hash(content).encode("utf-8")).hexdigest(), []
        ).append(legacy_id)

        plan_items.append(
            {
                "bucket_id": bucket_id,
                "legacy_summary_id": legacy_id,
                "legacy_summary_hash": row.get("legacy_summary_hash"),
                "legacy_review_status": legacy_status,
                "title": title,
                "content": content,
                "type": "dynamic",
                "domain": classify_domains(content),
                "tags": build_tags(content, window_kind, time_missing=time_missing),
                "importance": importance,
                "created": created,
                "last_active": created,
                "updated_at": created,
                "window_kind": window_kind,
                "mentions_former_name": FORMER_AI_NAME in content,
                "source_time_missing": time_missing,
            }
        )

    duplicate_groups = {
        digest: ids for digest, ids in content_hashes.items() if len(ids) > 1
    }

    stats = {
        "artifact_rows": len(items),
        "planned_rows": len(plan_items),
        "skipped_rows": len(items) - len(plan_items),
        "mentions_former_name": sum(1 for item in plan_items if item["mentions_former_name"]),
        "uncertain_time_rows": sum(
            1 for item in plan_items if UNCERTAIN_TIME_TAG in item["tags"]
        ),
        "missing_time_rows": sum(
            1 for item in plan_items if item.get("source_time_missing")
        ),
        "duplicate_content_groups": len(duplicate_groups),
        "duplicate_content_rows": sum(len(ids) for ids in duplicate_groups.values()),
        "domain_histogram": _histogram(
            domain for item in plan_items for domain in item["domain"]
        ),
        "created_range": [
            min((item["created"] for item in plan_items), default=""),
            max((item["created"] for item in plan_items), default=""),
        ],
    }

    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "source": {
            "artifact_path": str(artifact_path),
            "artifact_sha256": sha256_file(artifact_path),
            "windows_path": str(windows_path) if windows_path else "",
            "windows_sha256": sha256_file(windows_path) if windows_path else "",
            "missing_created_at": fallback_created,
        },
        "stats": stats,
        "duplicate_content_groups": duplicate_groups,
        "items": plan_items,
    }


def _histogram(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def payload_for(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["bucket_id"],
        "title": item["title"],
        "content": item["content"],
        "type": item["type"],
        "domain": item["domain"],
        "tags": item["tags"],
        "importance": item["importance"],
        "created": item["created"],
        "last_active": item["last_active"],
        "updated_at": item["updated_at"],
    }


def load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    done: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        bucket_id = str(row.get("bucket_id") or "")
        if bucket_id:
            done[bucket_id] = row
    return done


def append_state(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def post_memory(*, base_url: str, token: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/memories",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return response.status, _safe_json(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, _safe_json(text)


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:200]}
    return parsed if isinstance(parsed, dict) else {"raw": text[:200]}


def apply_plan(
    *,
    plan: dict[str, Any],
    base_url: str,
    token: str,
    state_path: Path,
    limit: int,
    sleep_seconds: float,
    timeout: float,
    max_retries: int,
    max_consecutive_failures: int,
    dry_run: bool,
) -> dict[str, Any]:
    if not plan.get("ok"):
        raise PlanError("refusing to apply a plan with ok=false")

    done = load_state(state_path)
    pending = [
        item
        for item in plan["items"]
        if done.get(item["bucket_id"], {}).get("status") not in {"created", "updated"}
    ]
    if limit > 0:
        pending = pending[:limit]

    created = updated = failed = 0
    consecutive_failures = 0
    failures: list[dict[str, Any]] = []

    for index, item in enumerate(pending, start=1):
        payload = payload_for(item)
        if dry_run:
            print(
                f"[dry-run {index}/{len(pending)}] {item['bucket_id']} "
                f"domain={item['domain']} tags={item['tags']} title={item['title']}"
            )
            continue

        status_code = 0
        response: dict[str, Any] = {}
        for attempt in range(1, max_retries + 1):
            try:
                status_code, response = post_memory(
                    base_url=base_url, token=token, payload=payload, timeout=timeout
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                status_code, response = 0, {"error": f"transport: {exc}"}
            if status_code == 200 or 400 <= status_code < 500:
                break
            if attempt < max_retries:
                time.sleep(min(30.0, 2.0 ** attempt))

        outcome = str(response.get("status") or "")
        if status_code == 200 and outcome in {"created", "updated"}:
            consecutive_failures = 0
            if outcome == "created":
                created += 1
            else:
                updated += 1
            append_state(
                state_path,
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "bucket_id": item["bucket_id"],
                    "legacy_summary_id": item["legacy_summary_id"],
                    "status": outcome,
                    "http_status": status_code,
                    "embedding": response.get("embedding"),
                    "at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                },
            )
        else:
            failed += 1
            consecutive_failures += 1
            failure = {
                "bucket_id": item["bucket_id"],
                "http_status": status_code,
                "error": str(response.get("error") or response.get("raw") or "")[:200],
            }
            failures.append(failure)
            append_state(
                state_path,
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "bucket_id": item["bucket_id"],
                    "legacy_summary_id": item["legacy_summary_id"],
                    "status": "failed",
                    "http_status": status_code,
                    "error": failure["error"],
                    "at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                },
            )
            if consecutive_failures >= max_consecutive_failures:
                print(
                    f"aborting after {consecutive_failures} consecutive failures",
                    file=sys.stderr,
                )
                break

        if index % 25 == 0:
            print(
                f"progress {index}/{len(pending)} created={created} "
                f"updated={updated} failed={failed}",
                flush=True,
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return {
        "ok": failed == 0,
        "attempted": len(pending),
        "created": created,
        "updated": updated,
        "failed": failed,
        "failures": failures[:20],
        "dry_run": dry_run,
    }


def verify_plan(
    *,
    plan: dict[str, Any],
    base_url: str,
    token: str,
    sample_size: int,
    timeout: float,
    seed: int,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Verify imported rows through the only channel this token may use.

    Read endpoints reject the memory-write token, so verification re-sends the
    identical payload. ``status=updated`` proves the bucket id exists, and
    ``embedding=skipped`` proves the server-side content hash did not move,
    i.e. the stored content still equals what was imported.
    """
    items = plan["items"]
    if state_path is not None:
        imported = {
            bucket_id
            for bucket_id, row in load_state(state_path).items()
            if row.get("status") in {"created", "updated"}
        }
        items = [item for item in items if item["bucket_id"] in imported]
    if not items:
        return {"ok": False, "sampled": 0, "matched": 0, "mismatched": [], "error": "no imported rows to verify"}
    rng = random.Random(seed)
    sample = rng.sample(items, min(sample_size, len(items)))
    matched = 0
    mismatched: list[dict[str, Any]] = []

    for item in sample:
        status_code, body, transport_error = 0, {}, ""
        for attempt in range(1, 4):
            try:
                status_code, body = post_memory(
                    base_url=base_url,
                    token=token,
                    payload=payload_for(item),
                    timeout=timeout,
                )
                transport_error = ""
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                transport_error = str(exc)[:120]
                if attempt < 3:
                    time.sleep(2.0 ** attempt)
        if transport_error:
            mismatched.append({"bucket_id": item["bucket_id"], "error": transport_error})
            continue

        outcome = str(body.get("status") or "")
        embedding = str(body.get("embedding") or "")
        if status_code != 200 or outcome != "updated":
            mismatched.append(
                {
                    "bucket_id": item["bucket_id"],
                    "http_status": status_code,
                    "status": outcome,
                    "reason": "bucket missing or not an idempotent update",
                }
            )
        elif embedding not in {"skipped", "disabled"}:
            mismatched.append(
                {
                    "bucket_id": item["bucket_id"],
                    "embedding": embedding,
                    "reason": "content hash changed on re-send",
                }
            )
        else:
            matched += 1

    return {
        "ok": not mismatched,
        "sampled": len(sample),
        "matched": matched,
        "mismatched": mismatched[:20],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _resolve_token(token_env: str) -> str:
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise SystemExit(f"environment variable {token_env} is empty")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("plan", help="build the offline import plan")
    plan_cmd.add_argument("--artifact", required=True, type=Path)
    plan_cmd.add_argument("--windows", type=Path, default=None)
    plan_cmd.add_argument("--missing-created-at", default=None)
    plan_cmd.add_argument("--output", required=True, type=Path)

    apply_cmd = sub.add_parser("apply", help="POST the plan to a Haven instance")
    apply_cmd.add_argument("--plan", required=True, type=Path)
    apply_cmd.add_argument("--base-url", required=True)
    apply_cmd.add_argument("--token-env", default="OMBRE_MEMORY_WRITE_TOKEN")
    apply_cmd.add_argument("--state", required=True, type=Path)
    apply_cmd.add_argument("--report", type=Path, default=None)
    apply_cmd.add_argument("--limit", type=int, default=0)
    apply_cmd.add_argument("--sleep", type=float, default=1.0)
    apply_cmd.add_argument("--timeout", type=float, default=60.0)
    apply_cmd.add_argument("--max-retries", type=int, default=3)
    apply_cmd.add_argument("--max-consecutive-failures", type=int, default=5)
    apply_cmd.add_argument("--dry-run", action="store_true")

    verify_cmd = sub.add_parser("verify", help="re-read a sample of imported buckets")
    verify_cmd.add_argument("--plan", required=True, type=Path)
    verify_cmd.add_argument("--base-url", required=True)
    verify_cmd.add_argument("--token-env", default="OMBRE_MEMORY_WRITE_TOKEN")
    verify_cmd.add_argument("--report", type=Path, default=None)
    verify_cmd.add_argument("--sample", type=int, default=20)
    verify_cmd.add_argument("--timeout", type=float, default=60.0)
    verify_cmd.add_argument("--seed", type=int, default=20260830)
    verify_cmd.add_argument("--state", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.command == "plan":
        plan = build_plan(
            artifact_path=args.artifact,
            windows_path=args.windows,
            missing_created_at=args.missing_created_at,
        )
        _write_json(args.output, plan)
        summary = {"ok": plan["ok"], "errors": plan["errors"][:10], **plan["stats"]}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if plan["ok"] else 1

    if args.command == "apply":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        token = "dry-run" if args.dry_run else _resolve_token(args.token_env)
        report = apply_plan(
            plan=plan,
            base_url=args.base_url,
            token=token,
            state_path=args.state,
            limit=args.limit,
            sleep_seconds=args.sleep,
            timeout=args.timeout,
            max_retries=args.max_retries,
            max_consecutive_failures=args.max_consecutive_failures,
            dry_run=args.dry_run,
        )
        if args.report:
            _write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    if args.command == "verify":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        report = verify_plan(
            plan=plan,
            base_url=args.base_url,
            token=_resolve_token(args.token_env),
            sample_size=args.sample,
            timeout=args.timeout,
            seed=args.seed,
            state_path=args.state,
        )
        if args.report:
            _write_json(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())