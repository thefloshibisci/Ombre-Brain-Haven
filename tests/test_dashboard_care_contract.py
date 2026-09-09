"""Source contracts for the Dashboard Care and extended config additions.

These tests deliberately inspect the static page and, where useful, evaluate
small extracted JavaScript fragments with Node. They do not need a browser,
server, credentials, or network access.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend" / "dashboard.html"
CONFIG_API = ROOT / "src" / "web" / "config_api.py"
LEGACY_COMPAT = ROOT / "src" / "web" / "legacy_compat.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    finish = source.index(end, begin)
    return source[begin:finish]


def _inline_scripts(html: str) -> list[str]:
    scripts: list[str] = []
    for match in re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        if re.search(r"\bsrc\s*=", match.group("attrs"), flags=re.IGNORECASE):
            continue
        scripts.append(match.group("body"))
    return scripts


def _literal_assignment(source: str, name: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(value)
    raise AssertionError(f"could not find literal assignment {name}")


def test_all_inline_scripts_pass_node_syntax_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")

    scripts = _inline_scripts(_read(DASHBOARD))
    assert len(scripts) >= 3
    with tempfile.TemporaryDirectory(prefix="dashboard-js-") as temp_dir:
        for index, source in enumerate(scripts, start=1):
            script_path = Path(temp_dir) / f"inline-{index}.js"
            script_path.write_text(source, encoding="utf-8")
            completed = subprocess.run(
                [node, "--check", str(script_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert completed.returncode == 0, (
                f"inline script {index} failed node --check:\n"
                f"{completed.stdout}\n{completed.stderr}"
            )


def test_care_markup_has_named_controls_and_mobile_layout_contract():
    html = _read(DASHBOARD)
    markup = _section(
        html,
        "<!-- ====== Care tab",
        "<!-- ====== iter 1.7 §H: About",
    )

    for control_id, field_name in (
        ("care-reminder-title", "title"),
        ("care-reminder-content", "content"),
        ("care-reminder-repeat", "repeat_rule"),
        ("care-reminder-start", "start_at"),
        ("care-reminder-end", "end_at"),
        ("care-reminder-next", "next_due_at"),
        ("care-reminder-rounds", "interval_rounds"),
        ("care-reminder-daily", "daily_limit"),
        ("care-reminder-max", "max_injections"),
    ):
        assert f'id="{control_id}"' in markup
        assert f'name="{field_name}"' in markup
        assert f'<label for="{control_id}">' in markup

    assert 'id="care-reminder-title"' in markup and " required" in markup
    assert 'id="care-reminder-content"' in markup and " required" in markup
    assert 'id="care-refresh-button"' in markup
    assert 'id="care-persona-session"' in markup
    assert 'aria-live="polite"' in markup

    care_css = _section(html, "/* ====== Care /", "/* 更窄的 PC 窗口")
    assert ".care-grid { grid-template-columns: 1fr; }" in care_css
    assert ".care-form-grid, .care-metrics { grid-template-columns: 1fr; }" in care_css
    assert "@media (max-width: 768px)" in care_css
    assert ".upstream-model-row { grid-template-columns:1fr;" in html


def test_care_loads_are_independent_and_keep_current_filters():
    html = _read(DASHBOARD)
    care = _section(html, "// ====== Care /", "async function loadAbout()")
    dashboard = _section(
        html,
        "function activateDashboardTab(tab)",
        "document.querySelectorAll('.tab').forEach(tab =>",
    )

    assert "Promise.allSettled(jobs.map" in care
    assert "renderCareUnavailable(job[3], e.message)" in care
    assert "if (target === 'care') pending.push(loadCareDashboard());" in dashboard
    assert "const requestedReminderStatus = careReminderStatus;" in care
    assert "status=' + encodeURIComponent(requestedReminderStatus)" in care
    assert "/api/moments?limit=200" in care
    assert "/api/daily-chat-memory/pending?status=pending&limit=12" in care
    assert "/api/persona?events_limit=8&sessions_limit=4" in care
    assert "generation !== careLoadGeneration" in care
    assert "generation !== careReminderLoadGeneration" in care
    assert "generation !== carePersonaLoadGeneration" in care
    assert "careMomentsMonthPinned" in care
    assert "careReminderStatus" in care


def test_care_actions_use_one_delegated_binding_and_escaped_data_attributes():
    html = _read(DASHBOARD)
    care = _section(html, "// ====== Care /", "async function loadAbout()")
    wire = _section(html, "function wireCareControls()", "async function loadAbout()")

    assert "careJsString" not in care
    assert "onclick=" not in care
    assert wire.count("root.addEventListener('click'") == 1
    assert "data-care-wired" in wire
    assert "if (!root || root.getAttribute('data-care-wired') === '1') return;" in wire
    assert "toggleCareReminderForm(null)" in wire
    assert "data-care-action=\"edit-reminder\"" in care
    assert "data-care-id=\"' + escAttr(id) + '\"" in care
    assert "data-care-index=\"' + escAttr(index) + '\"" in care
    assert "encodeURIComponent(id)" in care


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_care_renderer_escapes_untrusted_text_and_attribute_values():
    html = _read(DASHBOARD)
    esc = _section(html, "function esc(s)", "// iter 1.9 A:")
    care = _section(html, "// ====== Care /", "function openCareDetail")
    script = r'''
function htmlEscape(value) {
  return String(value).replace(/[&<>"']/g, function(char) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char];
  });
}
const elements = new Map();
function makeElement() {
  return {
    innerHTML: '', textContent: '', value: '',
    classList: {toggle() {}, add() {}, remove() {}},
    setAttribute() {}, getAttribute() { return ''; },
    querySelector() { return null; },
  };
}
const document = {
  createElement() {
    const element = {};
    Object.defineProperty(element, 'textContent', {
      set(value) { this._text = String(value); },
      get() { return this._text || ''; },
    });
    Object.defineProperty(element, 'innerHTML', {
      get() { return htmlEscape(this._text || ''); },
    });
    return element;
  },
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  },
};
''' + esc + care + r'''
renderCareReminders({reminders: [{
  id: 'x" data-x="y',
  title: '<img src=x onerror=alert(1)>',
  content: '</div><script>alert(1)</script>',
  next_due_at: '<svg onload=alert(2)>',
  status: 'active',
}]});
process.stdout.write(elements.get('care-reminders-content').innerHTML);
'''
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rendered = completed.stdout
    assert "<img" not in rendered
    assert "<script" not in rendered
    assert "<svg" not in rendered
    assert 'data-care-id="x&quot; data-x=&quot;y"' in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered


def test_care_form_edit_reset_and_write_safety_contract():
    html = _read(DASHBOARD)
    care = _section(html, "// ====== Care /", "async function loadAbout()")
    toggle = _section(care, "function toggleCareReminderForm", "function closeCareReminderForm")
    close = _section(care, "function closeCareReminderForm", "async function saveCareReminder")
    save = _section(care, "async function saveCareReminder", "async function careReminderPatch")
    patch = _section(care, "async function careReminderPatch", "async function loadCareReminders")

    assert "item.id != null" in toggle
    assert "careReminderEditingId = '';" in close
    assert "careReminderSaveInFlight" in save
    assert "if (submit) submit.disabled = true;" in save
    assert "Number.isInteger(value) || value < 0" in save
    assert "if (repeat === 'every_n_rounds' && payload.interval_rounds < 1)" in save
    assert "method: editingId ? 'PATCH' : 'POST'" in save
    assert "}, false);" in save
    assert "}, false);" in patch
    assert "await loadCareReminders();" in save
    assert "const requestedStatus = careReminderStatus;" in _section(
        care, "async function loadCareReminders", "function renderCareMomentsCalendar"
    )


def test_persona_dream_and_word_map_use_existing_compatibility_fields():
    html = _read(DASHBOARD)
    care = _section(html, "// ====== Care /", "async function loadAbout()")
    legacy = _read(LEGACY_COMPAT)

    persona = _section(care, "function renderCarePersona", "function renderCarePortrait")
    dreams = _section(care, "function renderCareDreams", "function renderCarePersona")
    word_map = _section(care, "function renderCareWordMap", "function openCareDetail")

    for field in ("state", "sessions", "events"):
        assert f"'{field}'" in persona
        assert f'"{field}"' in legacy or f"'{field}'" in legacy
    assert "active_session_id" not in persona
    for field in ("dream_id", "generated_at", "local_date"):
        assert f"'{field}'" in dreams
        assert field in legacy
    for field in ("term_a", "term_b"):
        assert field in word_map
        assert field in legacy


def test_extended_config_payload_fields_are_allowed_by_config_api():
    html = _read(DASHBOARD)
    save = _section(html, "async function saveConfig(persist)", "checkAuth().then")
    backend = _read(CONFIG_API)

    frontend_fields = {
        "gateway": {
            "domain_sentinel_enabled", "domain_sentinel_model", "domain_sentinel_base_url",
            "domain_sentinel_enable_thinking", "domain_sentinel_max_tokens",
            "recent_context_budget", "current_inner_state_interval_rounds", "cooldown_hours",
            "skip_recent_rounds", "recent_context_cooldown_hours", "recent_context_reentry_idle_hours",
            "recalled_memory_budget", "related_memory_budget", "memory_detail_recall_enabled",
            "memory_detail_recall_max_ids", "memory_detail_recall_budget", "direct_render_mode",
            "retrieval_mode", "operit_context_rewrite_enabled", "word_map_hint_enabled",
            "query_planner_enabled", "upstreams",
        },
        "recall": {"query_resurface_enabled"},
        "memory_diffusion": {
            "enabled", "top_k", "min_activation", "chain_walk_enabled", "chain_max_hops",
            "chain_min_confidence", "chain_max_frontier",
        },
        "reranker": {"enabled", "model", "base_url", "candidate_limit", "score_weight"},
        "persona": {"enabled", "event_recording_enabled", "conflict_nudge_enabled", "model", "base_url"},
        "reflection": {
            "enabled", "auto_enabled", "daily_enabled", "model", "base_url", "thinking_mode",
            "daily_chat_memory_summary_enabled", "daily_chat_memory_mode", "daily_chat_memory_hour",
            "daily_chat_memory_turn_limit", "daily_chat_memory_max_per_day", "daily_chat_memory_min_confidence",
            "daily_chat_memory_review_max_per_day", "daily_chat_memory_review_min_confidence",
            "daily_chat_memory_summary_model", "daily_chat_memory_candidate_model", "daily_chat_memory_base_url",
            "daily_chat_memory_api_key_env", "daily_chat_memory_timeout_seconds",
            "daily_chat_memory_summary_max_tokens", "daily_chat_memory_candidate_max_tokens",
            "daily_chat_memory_summary_window_turns", "daily_chat_memory_summary_stride_turns",
        },
        "dream": {"enabled", "auto_enabled", "surface_enabled", "inject_enabled", "model", "base_url", "thinking_mode"},
        "portrait": {"enabled", "auto_enabled", "auto_initial_enabled", "daily_enabled", "model", "base_url"},
        "self_anchor": {"entry_bucket_id"},
    }

    gateway_allowed = (
        _literal_assignment(backend, "_GATEWAY_BOOL_FIELDS")
        | set(_literal_assignment(backend, "_GATEWAY_INT_FIELDS"))
        | set(_literal_assignment(backend, "_GATEWAY_FLOAT_FIELDS"))
        | set(_literal_assignment(backend, "_GATEWAY_STRING_FIELDS"))
        | set(_literal_assignment(backend, "_GATEWAY_CHOICES"))
        | {"upstreams"}
    )
    section_rules = _literal_assignment(backend, "_SECTION_RULES")
    allowed = {"gateway": gateway_allowed}
    allowed.update({name: set(rules) for name, rules in section_rules.items()})

    for section, fields in frontend_fields.items():
        assert fields <= allowed[section], f"frontend fields not accepted for {section}: {fields - allowed[section]}"
        for field in fields - {"upstreams"}:
            assert re.search(rf"\b{re.escape(field)}\s*:", save), field

    assert "api_key_envs" in html and "api_key_env" in html
    assert "api_key_values" in html
    assert "upstream_model" in html
    assert "persist_env" in save
    assert "configStateReady" in save
    assert "configSaveInFlight" in save


def test_config_initialization_avoids_stale_overwrite_and_duplicate_env_fetch():
    html = _read(DASHBOARD)
    load = _section(html, "async function loadConfig()", "async function refreshEnvConfig")
    activation = _section(
        html,
        "function activateDashboardTab(tab)",
        "document.querySelectorAll('.tab').forEach(tab =>",
    )

    assert "var generation = ++configLoadGeneration;" in load
    assert "configStateReady = false;" in load
    assert "if (generation !== configLoadGeneration) return;" in load
    assert "if (generation === configLoadGeneration) configStateReady = true;" in load
    assert "loadConfig({refreshEnv:false})" in activation
    assert activation.count("refreshEnvConfig()") == 1
    assert "if (options.refreshEnv !== false) refreshEnvConfig();" in load
    assert "if (!configStateReady)" in _section(html, "async function saveConfig", "checkAuth().then")
