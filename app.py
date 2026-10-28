"""
app.py — AutoHeal AI  🛡️
Self-Healing Automation System
CODE E FIESTA 2026 — Problem Statement 4
"""
from __future__ import annotations
import json, os, time, uuid, random
import streamlit as st

from failures  import FAILURE_MODES, inject, random_failure
from workflow  import WORKFLOW_REGISTRY, make_steps, run_step, StepState
from diagnose  import diagnose
from fixes     import build_fixed_payload, dry_run_validate, compute_diff, backoff_delay
from models    import DiagnosisResult, FixAction, WorkflowError
import db

# ── Load API key from Streamlit secrets (cloud) or .env (local) ──────────────
try:
    _secret_key = st.secrets.get("GEMINI_API_KEY", "")
    if _secret_key:
        os.environ["GEMINI_API_KEY"] = _secret_key
except Exception:
    pass  # running locally without secrets.toml — .env will be used instead

# ══════════════════════════════════════════════════════════════
# Page config & CSS
# ══════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="AutoHeal AI", page_icon="🛡️",
    layout="wide", initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif;}

.hero{background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
      padding:1.6rem 2rem;border-radius:14px;margin-bottom:1.2rem;color:#fff;}
.hero h1{font-size:2.2rem;font-weight:700;margin:0;letter-spacing:-.5px;}
.hero p{font-size:1rem;margin:.3rem 0 0;opacity:.75;}

.badge{display:inline-block;padding:3px 12px;border-radius:20px;
       font-size:.78rem;font-weight:600;letter-spacing:.4px;}
.bg-green{background:#d1fae5;color:#065f46;}
.bg-red  {background:#fee2e2;color:#991b1b;}
.bg-amber{background:#fef3c7;color:#92400e;}
.bg-blue {background:#dbeafe;color:#1e40af;}
.bg-purp {background:#ede9fe;color:#5b21b6;}
.bg-gray {background:#f1f5f9;color:#475569;}
.bg-orange{background:#ffedd5;color:#9a3412;}

.step-log{font-family:'Courier New',monospace;font-size:.80rem;
          background:#0f172a;color:#94a3b8;padding:.9rem 1.1rem;
          border-radius:10px;max-height:200px;overflow-y:auto;line-height:1.7;}
.card{background:#f8fafc;border:1px solid #e2e8f0;
      border-radius:12px;padding:1.1rem 1.3rem;margin-bottom:.8rem;}
.metric-box{text-align:center;padding:.9rem;border-radius:12px;background:#f1f5f9;}
.metric-box .val{font-size:1.9rem;font-weight:700;}
.metric-box .lbl{font-size:.74rem;color:#64748b;margin-top:2px;}
.diff-add{color:#16a34a;font-weight:600;}
.diff-del{color:#dc2626;text-decoration:line-through;}
.diff-risky{background:#fef3c7;padding:1px 4px;border-radius:4px;}
.mem-hit{background:linear-gradient(90deg,#d1fae5,#a7f3d0);
         border-left:4px solid #059669;padding:.7rem 1rem;
         border-radius:8px;margin:.5rem 0;}
section[data-testid="stSidebar"]{background:#0f172a;}
section[data-testid="stSidebar"] *{color:#e2e8f0!important;}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
# Session state initialisation
# ══════════════════════════════════════════════════════════════
_DEFAULTS = dict(
    phase="idle",          # idle|running|failed|diagnosing|fixing|awaiting_approval|retrying|done|escalated
    run_id=None,
    steps=[],              # list of StepState (serialised as dicts)
    original_payload=None,
    current_payload=None,
    failed_step_idx=None,
    attempt=0,
    diagnosis=None,        # DiagnosisResult dict
    fix_source=None,       # memory|llm|fallback
    patched_payload=None,
    error_signature=None,
    dry_run_ok=None,
    dry_run_msg=None,
    start_time=None,
    failure_time=None,
    last_workflow=None,
    last_failure=None,
    chaos_active=False,
    approval_pending=False,
)
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

ss = st.session_state


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _step_icon(status: str) -> str:
    return {"pending":"⬜","running":"🔄","success":"✅","failed":"❌","skipped":"⏭️"}.get(status,"⬜")

def _badge(text, cls="bg-gray"):
    return f'<span class="badge {cls}">{text}</span>'

def _metric(val, lbl):
    return f'<div class="metric-box"><div class="val">{val}</div><div class="lbl">{lbl}</div></div>'

def _reset():
    for k, v in _DEFAULTS.items():
        ss[k] = v
    st.rerun()

def _steps_as_dicts(steps: list[StepState]) -> list[dict]:
    return [
        dict(step_id=s.step_id, name=s.name, status=s.status,
             output=s.output, error=s.error,
             duration_ms=s.duration_ms, attempt=s.attempt)
        for s in steps
    ]

def _steps_from_dicts(dicts: list[dict], workflow_name: str) -> list[StepState]:
    from workflow import WORKFLOW_REGISTRY
    step_fns = {sid: fn for sid, _, fn in WORKFLOW_REGISTRY[workflow_name]["step_defs"]}
    result = []
    for d in dicts:
        s = StepState(step_id=d["step_id"], name=d["name"], fn=step_fns[d["step_id"]])
        s.status = d["status"]; s.output = d["output"]; s.error = d["error"]
        s.duration_ms = d["duration_ms"]; s.attempt = d["attempt"]
        result.append(s)
    return result


# ══════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 🛡️ AutoHeal AI")
    st.markdown("---")

    _env_key = os.environ.get("GEMINI_API_KEY", "")
    _key_from_secrets = bool(_env_key and not _env_key.startswith("your_"))

    if _key_from_secrets:
        st.success("🔑 API key loaded from secrets ✓")
        api_key = _env_key
    else:
        api_key = st.text_input("🔑 Gemini API Key", type="password",
                                 placeholder="AIza…",
                                 help="Get free at aistudio.google.com")
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key
            st.success("API key set ✓")
        else:
            st.info("No key → rule-based fallback")

    st.markdown("---")

    workflow_name = st.selectbox(
        "📋 Workflow",
        list(WORKFLOW_REGISTRY.keys()),
        format_func=lambda k: f"{WORKFLOW_REGISTRY[k]['icon']} {k}",
    )
    wf_meta = WORKFLOW_REGISTRY[workflow_name]

    failure_mode = st.selectbox(
        "💥 Inject Failure",
        list(FAILURE_MODES.keys()),
        format_func=lambda k: f"{FAILURE_MODES[k].icon} {FAILURE_MODES[k].label}",
    )
    fm = FAILURE_MODES[failure_mode]

    max_retries = st.slider("🔁 Max Retries", 1, 5, 3)
    use_llm     = st.toggle("🤖 Use Gemini AI", value=True)

    st.markdown("---")
    st.markdown("**⚡ Chaos Mode**")
    if st.button("🎲 Random Failure Injection", use_container_width=True):
        ss.chaos_active = True
        st.rerun()

    st.markdown("---")
    st.caption(wf_meta["description"])


# ══════════════════════════════════════════════════════════════
# Hero
# ══════════════════════════════════════════════════════════════
st.markdown("""
<div class="hero">
  <h1>🛡️ AutoHeal AI</h1>
  <p>Self-Healing Automation · CODE E FIESTA 2026 · Problem Statement 4 &nbsp;|&nbsp;
     Monitor → Detect → Diagnose → Fix → Recover</p>
</div>""", unsafe_allow_html=True)

# Chaos mode override
if ss.chaos_active:
    failure_mode = random_failure()
    ss.chaos_active = False
    st.toast(f"🎲 Chaos mode! Injecting: {FAILURE_MODES[failure_mode].label}", icon="🎲")

# ══════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════
tab_run, tab_metrics, tab_history, tab_dlq = st.tabs(
    ["🚀 Run Workflow", "📊 Metrics Dashboard", "📜 Run History", "💀 Dead-Letter Queue"]
)


# ══════════════════════════════════════════════════════════════
# TAB 1 — RUN WORKFLOW
# ══════════════════════════════════════════════════════════════
with tab_run:

    # ── Payload editor ────────────────────────────────────────
    st.subheader("📝 Input Payload")
    col_pay, col_info = st.columns([3, 1])
    with col_pay:
        payload_str = st.text_area(
            "JSON payload (editable)",
            value=json.dumps(wf_meta["default_payload"], indent=2),
            height=180, label_visibility="collapsed",
        )
        try:
            user_payload = json.loads(payload_str)
            st.caption("✅ Valid JSON")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            user_payload = wf_meta["default_payload"]

    with col_info:
        st.markdown(f"**Failure being injected:**")
        st.markdown(f"{fm.icon} **{fm.label}**")
        st.caption(fm.description)
        cat_cls = {"data":"bg-red","transient":"bg-amber","logic":"bg-purp",
                   "external":"bg-orange","none":"bg-green"}.get(fm.category,"bg-gray")
        st.markdown(_badge(fm.category.upper(), cat_cls), unsafe_allow_html=True)

    st.markdown("---")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        run_btn = st.button("▶ Run Workflow", type="primary", use_container_width=True,
                            disabled=ss.phase not in ("idle","done","escalated"))
    with c2:
        if st.button("🔄 Reset", use_container_width=True):
            _reset()
    with c3:
        if st.button("🎲 Chaos Mode", use_container_width=True):
            ss.chaos_active = True
            st.rerun()

    if run_btn:
        chosen_failure = failure_mode if not ss.chaos_active else random_failure()
        injected = inject(user_payload, chosen_failure, workflow_name)
        steps    = make_steps(workflow_name)
        ss.update(dict(
            phase="running", run_id=str(uuid.uuid4())[:8],
            steps=_steps_as_dicts(steps),
            original_payload=injected, current_payload=injected,
            patched_payload=None, failed_step_idx=None,
            attempt=1, diagnosis=None, fix_source=None,
            error_signature=None, dry_run_ok=None, dry_run_msg=None,
            start_time=time.time(), failure_time=None,
            last_workflow=workflow_name, last_failure=chosen_failure,
        ))
        st.rerun()

    # ═══════════════════════════════════════════════════════════
    # STATE MACHINE
    # ═══════════════════════════════════════════════════════════
    phase = ss.phase

    # ── IDLE ──────────────────────────────────────────────────
    if phase == "idle":
        st.info("👆 Configure your workflow above and click **Run Workflow** to begin.")

    # ── RUNNING ───────────────────────────────────────────────
    elif phase == "running":
        wf_name  = ss.last_workflow
        fm_key   = ss.last_failure
        payload  = ss.current_payload
        steps    = _steps_from_dicts(ss.steps, wf_name)
        failed   = False

        with st.status(f"⚙️ Executing **{wf_name}** — Attempt #{ss.attempt}", expanded=True) as status:
            if fm_key != "none":
                st.write(f"💥 Failure injected: **{FAILURE_MODES[fm_key].label}**")

            for i, step in enumerate(steps):
                st.write(f"→ Step {i+1}/{len(steps)}: **{step.name}** …")
                outcome = run_step(step, payload, fm_key)
                if outcome.success:
                    st.write(f"✅ {step.name} — {round(step.duration_ms)}ms")
                else:
                    err = outcome.error
                    st.write(f"❌ **{step.name}** failed: {err['message']}")
                    ss.failed_step_idx = i
                    ss.failure_time    = time.time()
                    failed = True

                    we = WorkflowError(
                        error_type=err["type"], message=err["message"],
                        field=err.get("field"), step_id=step.step_id,
                        step_name=step.name, traceback=err.get("traceback",""),
                    )
                    ss.error_signature = we.signature(wf_name)
                    db.audit(ss.run_id, "FAILURE_DETECTED", "system",
                             f"Step '{step.name}' failed: {err['message']}",
                             metadata=err)
                    break

            ss.steps = _steps_as_dicts(steps)

            if failed:
                status.update(label=f"❌ Failed at step: **{steps[ss.failed_step_idx].name}**",
                               state="error")
                db.insert_run(
                    ss.run_id, wf_name, fm_key, ss.attempt, "FAILED",
                    steps[ss.failed_step_idx].step_id,
                    steps[ss.failed_step_idx].error["type"], ss.error_signature,
                    None, None, None, None,
                    ss.original_payload, None, None,
                    (time.time() - ss.start_time)*1000, None,
                )
                ss.phase = "diagnosing"
            else:
                status.update(label="✅ Workflow completed successfully!", state="complete")
                final_out = steps[-1].output
                duration  = (time.time() - ss.start_time)*1000
                db.insert_run(
                    ss.run_id, wf_name, fm_key, ss.attempt, "SUCCESS",
                    None, None, None, None, None, "none", None,
                    ss.original_payload, None, final_out, duration, None,
                )
                db.audit(ss.run_id, "WORKFLOW_SUCCESS", "system",
                         f"Completed in {round(duration)}ms on attempt #{ss.attempt}")
                ss.phase = "done"

        st.rerun()

    # ── DIAGNOSING ────────────────────────────────────────────
    elif phase == "diagnosing":
        wf_name  = ss.last_workflow
        steps    = _steps_from_dicts(ss.steps, wf_name)
        fi       = ss.failed_step_idx
        err      = steps[fi].error
        wf_meta2 = WORKFLOW_REGISTRY[wf_name]

        with st.status("🔍 Checking fix memory…", expanded=True) as status:
            cached = db.lookup_fix(ss.error_signature)
            if cached:
                st.write(f"⚡ **Memory HIT** — seen {cached['use_count']} time(s) before! Skipping LLM.")
                status.update(label="⚡ Instant fix from memory!", state="complete")
            else:
                st.write("💭 Cache miss — calling AI…")
                status.update(label="🤖 AI diagnosing…")

            diag_result, fix_src = diagnose(
                wf_name, wf_meta2["description"],
                ss.current_payload, err, steps[fi].name,
                wf_meta2["field_defaults"], ss.error_signature,
                use_llm=use_llm,
            )

            if fix_src == "memory":
                status.update(label="⚡ Fix memory hit — instant recovery!", state="complete")
            elif fix_src == "llm":
                status.update(label="🤖 AI diagnosis complete", state="complete")
            else:
                status.update(label="📏 Rule-based fallback applied", state="complete")

        ss.diagnosis  = diag_result.model_dump()
        ss.fix_source = fix_src
        db.audit(ss.run_id, "DIAGNOSIS_COMPLETE", "ai" if fix_src != "memory" else "system",
                 f"Source={fix_src} | action={diag_result.fix_action} | confidence={diag_result.confidence:.0%}",
                 metadata=ss.diagnosis)

        # Build patched payload
        patched = build_fixed_payload(ss.current_payload, diag_result, wf_meta2["field_defaults"])
        ss.patched_payload = patched

        # Dry-run validation
        ok, msg = dry_run_validate(wf_name, patched)
        ss.dry_run_ok  = ok
        ss.dry_run_msg = msg
        db.audit(ss.run_id, "DRY_RUN", "system",
                 f"{'PASSED' if ok else 'FAILED'}: {msg or 'OK'}")

        ss.phase = "fixing"
        st.rerun()

    # ── FIXING ────────────────────────────────────────────────
    elif phase == "fixing":
        wf_name    = ss.last_workflow
        steps      = _steps_from_dicts(ss.steps, wf_name)
        fi         = ss.failed_step_idx
        err        = steps[fi].error
        diag       = DiagnosisResult(**ss.diagnosis)
        fix_src    = ss.fix_source

        # ── Step Timeline ─────────────────────────────────────
        st.subheader("📍 Execution Timeline")
        cols = st.columns(len(steps))
        for i, (col, step) in enumerate(zip(cols, steps)):
            with col:
                icon  = _step_icon(step.status)
                color = "#16a34a" if step.status=="success" else \
                        "#dc2626" if step.status=="failed"  else "#94a3b8"
                st.markdown(
                    f'<div style="text-align:center;padding:.5rem;border-radius:8px;'
                    f'border:2px solid {color};background:#f8fafc">'
                    f'<div style="font-size:1.4rem">{icon}</div>'
                    f'<div style="font-size:.72rem;font-weight:600;color:{color}">{step.name}</div>'
                    f'<div style="font-size:.68rem;color:#94a3b8">'
                    f'{round(step.duration_ms)}ms | #{step.attempt}</div></div>',
                    unsafe_allow_html=True,
                )
        st.markdown("---")

        # ── Error box ──────────────────────────────────────────
        failed_step = steps[fi]
        st.markdown(f"""
<div class="card" style="border-left:4px solid #dc2626">
  <b>❌ Failed at:</b> {failed_step.name} &nbsp;
  {_badge(err['type'], 'bg-red')} &nbsp;
  {_badge(FAILURE_MODES[ss.last_failure].category.upper(),
          {'data':'bg-red','transient':'bg-amber','logic':'bg-purp','external':'bg-orange'}.get(
           FAILURE_MODES[ss.last_failure].category,'bg-gray'))}<br><br>
  <b>Message:</b> {err['message']}<br>
  <code style="font-size:.8rem">{err.get('traceback','')}</code>
</div>""", unsafe_allow_html=True)

        # ── Memory hit banner ──────────────────────────────────
        if fix_src == "memory":
            st.markdown("""
<div class="mem-hit">
  ⚡ <b>Fix Memory Hit!</b> — This error pattern was seen before.
  Applying known fix <b>instantly</b> — no LLM call needed.
  The system is getting smarter! 🧠
</div>""", unsafe_allow_html=True)

        # ── Diagnosis card ─────────────────────────────────────
        st.subheader("🤖 AI Diagnosis")
        src_cls = {"memory":"bg-green","llm":"bg-blue","fallback":"bg-amber"}.get(fix_src,"bg-gray")
        src_lbl = {"memory":"⚡ Memory","llm":"🤖 Gemini AI","fallback":"📏 Rule-Based"}.get(fix_src,"?")

        c1, c2, c3, c4 = st.columns(4)
        mets = [
            (diag.severity(), "Severity"),
            (f"{diag.confidence:.0%}", "AI Confidence"),
            (diag.fix_action.value.replace("_"," ").title(), "Fix Action"),
            (src_lbl, "Source"),
        ]
        cls_map = {"LOW":"bg-green","MEDIUM":"bg-amber","HIGH":"bg-red","CRITICAL":"bg-red"}
        for col, (val, lbl) in zip([c1,c2,c3,c4], mets):
            with col:
                st.markdown(_metric(val, lbl), unsafe_allow_html=True)

        st.markdown(f"""
<div class="card" style="margin-top:.8rem">
  <b>🔍 Root Cause</b><br>{diag.root_cause}<br><br>
  <b>🔧 Proposed Fix</b><br>{diag.explanation or diag.fix_action.value}<br><br>
  <b>💡 Prevention Tip</b> — <i>{diag.prevention_tip}</i>
</div>""", unsafe_allow_html=True)

        # ── Before / After diff ────────────────────────────────
        if ss.patched_payload and ss.patched_payload != ss.current_payload:
            with st.expander("🔀 Before / After Payload Diff", expanded=True):
                diffs = compute_diff(ss.current_payload, ss.patched_payload)
                changed = [d for d in diffs if d["changed"]]
                if changed:
                    rows = ""
                    for d in diffs:
                        if d["changed"]:
                            risky_cls = "diff-risky" if d["is_risky"] else ""
                            rows += (
                                f'<tr class="{risky_cls}">'
                                f'<td><b>{d["field"]}</b>'
                                f'{"  ⚠️" if d["is_risky"] else ""}</td>'
                                f'<td class="diff-del">{json.dumps(d["old_value"])}</td>'
                                f'<td class="diff-add">{json.dumps(d["new_value"])}</td>'
                                f'</tr>'
                            )
                        else:
                            rows += (
                                f'<tr style="color:#94a3b8"><td>{d["field"]}</td>'
                                f'<td colspan="2">{json.dumps(d["old_value"])}</td></tr>'
                            )
                    st.markdown(
                        f'<table style="width:100%;font-size:.82rem;border-collapse:collapse">'
                        f'<thead><tr><th>Field</th><th>Before</th><th>After</th></tr></thead>'
                        f'<tbody>{rows}</tbody></table>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("No structural changes (retry only).")

        # ── Dry-run result ─────────────────────────────────────
        if ss.dry_run_ok:
            st.success(f"✅ Dry-run validation passed — safe to retry.")
        else:
            st.error(f"❌ Dry-run failed: {ss.dry_run_msg}")
            st.warning("Auto-fix could not produce a valid payload. Escalating.")
            if st.button("🚨 Escalate to Human", type="primary"):
                ss.phase = "escalated"
                st.rerun()

        if ss.dry_run_ok:
            # ── Approval gate ──────────────────────────────────
            needs_approval = diag.needs_human_approval()
            reason         = diag.approval_reason()

            if needs_approval:
                st.warning(f"⚠️ **Human Approval Required** — {reason}")
                ca, cr = st.columns(2)
                with ca:
                    if st.button("✅ Approve & Retry", type="primary", use_container_width=True):
                        db.audit(ss.run_id, "HUMAN_APPROVED", "human",
                                 f"Human approved fix: {diag.fix_action.value}")
                        ss.phase   = "retrying"
                        ss.attempt += 1
                        st.rerun()
                with cr:
                    if st.button("🚫 Reject — Escalate", use_container_width=True):
                        db.audit(ss.run_id, "HUMAN_REJECTED", "human", "Fix rejected by human")
                        ss.phase = "escalated"
                        st.rerun()
            else:
                if fix_src == "memory":
                    # Auto-apply instantly
                    ss.phase   = "retrying"
                    ss.attempt += 1
                    st.rerun()
                else:
                    if st.button("🔁 Apply Fix & Retry", type="primary", use_container_width=True):
                        ss.phase   = "retrying"
                        ss.attempt += 1
                        st.rerun()

    # ── RETRYING ──────────────────────────────────────────────
    elif phase == "retrying":
        wf_name  = ss.last_workflow
        fm_key   = ss.last_failure
        diag     = DiagnosisResult(**ss.diagnosis)
        payload  = ss.patched_payload or ss.current_payload
        wf_meta2 = WORKFLOW_REGISTRY[wf_name]

        # Determine start step (step-level retry)
        start_idx = ss.failed_step_idx if ss.failed_step_idx is not None else 0
        steps     = _steps_from_dicts(ss.steps, wf_name)

        # For transient failures, clear the failure mode so retry succeeds
        retry_fm = "none" if fm_key in ("api_timeout","network_error","transient") else "none"

        # Exponential backoff for transient errors
        delay = 0.0
        if diag.fix_action == FixAction.retry_with_backoff:
            delay = backoff_delay(ss.attempt - 1, base=0.5, max_delay=4.0)

        with st.status(
            f"🔁 Retrying **{wf_name}** from step {start_idx+1} "
            f"(Attempt #{ss.attempt}/{max_retries}"
            f"{f', backoff {delay:.1f}s' if delay else ''})…",
            expanded=True
        ) as status:
            if delay:
                st.write(f"⏳ Exponential backoff: waiting {delay:.1f}s…")
                time.sleep(delay)

            failed = False
            for i in range(start_idx, len(steps)):
                step = steps[i]
                if i < start_idx:
                    step.status = "skipped"; continue

                st.write(f"→ Step {i+1}/{len(steps)}: **{step.name}** …")
                step.status = "pending"
                outcome = run_step(step, payload, retry_fm)

                if outcome.success:
                    st.write(f"✅ {step.name} — {round(step.duration_ms)}ms")
                else:
                    err = outcome.error
                    st.write(f"❌ **{step.name}** failed again: {err['message']}")
                    ss.failed_step_idx = i
                    ss.failure_time    = time.time()
                    failed = True

                    we = WorkflowError(
                        error_type=err["type"], message=err["message"],
                        field=err.get("field"), step_id=step.step_id,
                        step_name=step.name, traceback=err.get("traceback",""),
                    )
                    ss.error_signature = we.signature(wf_name)
                    break

            ss.steps           = _steps_as_dicts(steps)
            ss.current_payload = payload

            if failed:
                status.update(label=f"❌ Retry failed at: {steps[ss.failed_step_idx].name}",
                               state="error")
                if ss.attempt >= max_retries:
                    db.audit(ss.run_id, "MAX_RETRIES_EXCEEDED", "system",
                             f"Exceeded {max_retries} retries — escalating")
                    ss.phase = "escalated"
                else:
                    ss.diagnosis = None
                    ss.phase     = "diagnosing"
            else:
                status.update(label="✅ Recovery successful!", state="complete")
                duration = (time.time() - ss.start_time) * 1000
                mttr     = (time.time() - ss.failure_time) * 1000 if ss.failure_time else None

                # Store to fix memory
                if ss.fix_source != "memory":
                    db.store_fix(
                        ss.error_signature, wf_name,
                        diag.error_type.value, None,
                        diag.fix_action.value, diag.patch,
                    )
                db.mark_fix_success(ss.error_signature)

                db.insert_run(
                    ss.run_id, wf_name, fm_key, ss.attempt, "SUCCESS",
                    None, diag.error_type.value, ss.error_signature,
                    diag.root_cause, diag.fix_action.value, ss.fix_source,
                    diag.confidence, ss.original_payload, payload,
                    steps[-1].output, duration, mttr,
                )
                db.audit(ss.run_id, "RECOVERY_SUCCESS", "system",
                         f"Recovered in {round(mttr or 0)}ms after {ss.attempt} attempt(s)")
                ss.phase = "done"

        st.rerun()

    # ── DONE ──────────────────────────────────────────────────
    elif phase == "done":
        wf_name   = ss.last_workflow
        steps     = _steps_from_dicts(ss.steps, wf_name)
        attempt   = ss.attempt
        fix_src   = ss.fix_source
        duration  = (time.time() - ss.start_time) * 1000
        mttr_ms   = (time.time() - ss.failure_time)*1000 if ss.failure_time else None

        if attempt == 1 and not ss.diagnosis:
            st.success("🎉 **Workflow completed successfully on the first attempt!**")
        elif fix_src == "memory":
            st.success(f"⚡ **Instant Recovery via Fix Memory** — healed in {round(mttr_ms or 0)}ms without LLM! 🧠")
        else:
            st.success(f"✅ **Recovered Successfully** — healed in {attempt} attempt(s) ({round(mttr_ms or 0)}ms MTTR)")

        # Metrics row
        c1,c2,c3,c4 = st.columns(4)
        src_label = {"memory":"⚡ Memory","llm":"🤖 Gemini","fallback":"📏 Rules","none":"—"}.get(fix_src or "none","—")
        for col, (val, lbl) in zip([c1,c2,c3,c4], [
            ("✅", "Status"),
            (attempt, "Attempts"),
            (f"{round(mttr_ms or 0)}ms", "MTTR"),
            (src_label, "Fix Source"),
        ]):
            with col:
                st.markdown(_metric(val, lbl), unsafe_allow_html=True)

        st.markdown("---")

        # Step timeline
        st.subheader("📍 Final Execution Timeline")
        step_cols = st.columns(len(steps))
        for col, step in zip(step_cols, steps):
            with col:
                icon  = _step_icon(step.status)
                color = "#16a34a" if step.status == "success" else "#94a3b8"
                st.markdown(
                    f'<div style="text-align:center;padding:.5rem;border-radius:8px;'
                    f'border:2px solid {color}">'
                    f'<div style="font-size:1.3rem">{icon}</div>'
                    f'<div style="font-size:.72rem;font-weight:600">{step.name}</div>'
                    f'<div style="font-size:.68rem;color:#94a3b8">{round(step.duration_ms)}ms</div>'
                    f'</div>', unsafe_allow_html=True,
                )

        # Output
        final_out = next((s.output for s in reversed(steps) if s.output), None)
        if final_out:
            with st.expander("📦 Workflow Output", expanded=True):
                st.json(final_out)

        # Diagnosis summary
        if ss.diagnosis:
            diag = DiagnosisResult(**ss.diagnosis)
            with st.expander("🤖 Explainability Card — Why did the AI choose this fix?"):
                st.markdown(f"**Root Cause:** {diag.root_cause}")
                st.markdown(f"**Fix Action:** `{diag.fix_action.value}` ({diag.explanation})")
                st.markdown(f"**Confidence:** {diag.confidence:.0%}")
                st.markdown(f"**Prevention Tip:** _{diag.prevention_tip}_")
                st.markdown(f"**Fix Source:** {fix_src}")

    # ── ESCALATED ─────────────────────────────────────────────
    elif phase == "escalated":
        wf_name = ss.last_workflow
        st.error("🚨 **Human Intervention Required**")
        st.markdown("""
<div class="card" style="border-left:4px solid #dc2626">
  <b>AutoHeal exhausted all automated recovery options.</b><br><br>
  This workflow has been pushed to the <b>Dead-Letter Queue</b> for human review.
  All context (error, diagnosis, attempted fixes) is preserved in the audit log.
</div>""", unsafe_allow_html=True)

        diag = DiagnosisResult(**ss.diagnosis) if ss.diagnosis else None
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Error Details**")
            steps = _steps_from_dicts(ss.steps, wf_name)
            fi    = ss.failed_step_idx
            if fi is not None:
                st.json(steps[fi].error)
        with col2:
            st.markdown("**Last AI Diagnosis**")
            if diag:
                st.markdown(f"- **Root Cause:** {diag.root_cause}")
                st.markdown(f"- **Severity:** {diag.severity()}")
                st.markdown(f"- **Confidence:** {diag.confidence:.0%}")
                st.markdown(f"- **Reason:** {diag.approval_reason() or 'Max retries exceeded'}")

        # Push to DLQ (only once)
        if not ss.get("dlq_pushed"):
            db.push_dlq(
                ss.run_id or "unknown", wf_name, ss.last_failure or "unknown",
                diag.error_type.value if diag else None,
                diag.root_cause if diag else None,
                ss.current_payload,
                diag.approval_reason() if diag else "Max retries exceeded",
            )
            db.audit(ss.run_id, "DLQ_PUSH", "system",
                     f"Pushed to dead-letter queue after {ss.attempt} attempts")
            ss.dlq_pushed = True

        # Simulated Slack alert
        st.markdown("""
<div class="card" style="background:#f0f9ff;border-left:4px solid #0284c7;margin-top:.8rem">
  📣 <b>Simulated Slack Alert</b> sent to <code>#incident-alerts</code>:<br>
  <i>"🚨 AutoHeal escalation: <b>{wf}</b> failed after {n} attempts.
  Error: {err}. Human review required → Dead-Letter Queue."</i>
</div>""".format(
            wf=wf_name, n=ss.attempt,
            err=(diag.error_type.value if diag else "unknown"),
        ), unsafe_allow_html=True)

        st.info("📋 Check the **💀 Dead-Letter Queue** tab to review and resolve.")


# ══════════════════════════════════════════════════════════════
# TAB 2 — METRICS DASHBOARD
# ══════════════════════════════════════════════════════════════
with tab_metrics:
    st.subheader("📊 Metrics Dashboard")
    metrics = db.fetch_metrics()

    if metrics["total"] == 0:
        st.info("No runs recorded yet. Run a workflow to see metrics.")
    else:
        c1,c2,c3,c4,c5 = st.columns(5)
        for col, (val, lbl) in zip([c1,c2,c3,c4,c5], [
            (metrics["total"],               "Total Runs"),
            (metrics["success"],             "Successful"),
            (f"{metrics['auto_heal_rate']}%","Auto-Heal Rate"),
            (f"{metrics['avg_mttr_s']}s",    "Avg MTTR"),
            (metrics["llm_calls_saved"],     "LLM Calls Saved 💰"),
        ]):
            with col:
                st.markdown(_metric(val, lbl), unsafe_allow_html=True)

        st.markdown("---")
        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("**Failures by Error Type**")
            if metrics["by_type"]:
                for etype, cnt in metrics["by_type"].items():
                    pct = cnt / max(metrics["total"], 1)
                    st.markdown(f"`{etype}` — {cnt} ({pct:.0%})")
                    st.progress(pct)
            else:
                st.caption("No failures recorded.")

        with col_b:
            st.markdown("**Fix Source Breakdown**")
            src_icons = {"memory":"⚡","llm":"🤖","fallback":"📏","none":"✅"}
            if metrics["by_source"]:
                for src, cnt in metrics["by_source"].items():
                    pct = cnt / max(metrics["total"], 1)
                    icon = src_icons.get(src, "❓")
                    st.markdown(f"{icon} `{src}` — {cnt} ({pct:.0%})")
                    st.progress(pct)
            else:
                st.caption("No fix data yet.")

        st.markdown("---")
        st.markdown("**🧠 Fix Memory Status**")
        cm1, cm2 = st.columns(2)
        with cm1:
            st.markdown(_metric(metrics["mem_patterns"], "Learned Patterns"), unsafe_allow_html=True)
        with cm2:
            st.markdown(_metric(metrics["mem_hits"], "Cache Hits (LLM calls saved)"), unsafe_allow_html=True)

        mem_rows = db.fetch_fix_memory()
        if mem_rows:
            with st.expander("📚 Fix Memory Contents"):
                for row in mem_rows:
                    st.markdown(
                        f"**{row['workflow']}** — `{row['error_type']}` "
                        f"→ `{row['fix_action']}` | "
                        f"Used {row['use_count']}× | "
                        f"✅ {row['success_count']} successes"
                    )


# ══════════════════════════════════════════════════════════════
# TAB 3 — RUN HISTORY
# ══════════════════════════════════════════════════════════════
with tab_history:
    st.subheader("📜 Run History")
    ch, cc = st.columns([4, 1])
    with cc:
        if st.button("🗑️ Clear All Data", use_container_width=True):
            db.clear_all()
            st.rerun()

    rows = db.fetch_history(60)
    if not rows:
        st.info("No runs yet.")
    else:
        status_icon = {"SUCCESS":"✅","FAILED":"❌","ESCALATED":"🚨"}
        for row in rows:
            icon = status_icon.get(row["status"], "❓")
            fix_src_lbl = {"memory":"⚡","llm":"🤖","fallback":"📏","none":"—"}.get(
                row.get("fix_source") or "none", "—")

            with st.expander(
                f"{icon} [{row['run_at']}]  **{row['workflow']}**  —  "
                f"Attempt #{row['attempt']}  |  {row['status']}  |  {fix_src_lbl}"
            ):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Failure Mode:** `{row['failure_mode']}`")
                    st.markdown(f"**Error Type:** `{row['error_type'] or 'None'}`")
                    if row["root_cause"]:
                        st.markdown(f"**Root Cause:** {row['root_cause']}")
                    if row["fix_action"]:
                        st.markdown(f"**Fix Action:** `{row['fix_action']}`")
                    if row.get("mttr_ms"):
                        st.markdown(f"**MTTR:** {round(row['mttr_ms'])}ms")
                with col2:
                    if row["output"]:
                        st.markdown("**Output:**")
                        try:
                            st.json(json.loads(row["output"]))
                        except Exception:
                            st.code(row["output"])

                # Audit trail for this run
                audit_rows = db.fetch_audit(run_id=row["run_id"], limit=20)
                if audit_rows:
                    with st.expander("📋 Audit Trail"):
                        for ar in reversed(audit_rows):
                            actor_icon = {"system":"⚙️","ai":"🤖","human":"👤"}.get(ar["actor"],"❓")
                            st.caption(
                                f"{actor_icon} `{ar['logged_at']}` "
                                f"**[{ar['event_type']}]** {ar['description']}"
                            )


# ══════════════════════════════════════════════════════════════
# TAB 4 — DEAD-LETTER QUEUE
# ══════════════════════════════════════════════════════════════
with tab_dlq:
    st.subheader("💀 Dead-Letter Queue")
    st.caption("Workflows that AutoHeal could not automatically recover. Pending human review.")

    dlq = db.fetch_dlq()
    pending = [d for d in dlq if d["status"] == "pending"]
    resolved= [d for d in dlq if d["status"] != "pending"]

    st.markdown(f"**{len(pending)} pending** · {len(resolved)} resolved")

    if not dlq:
        st.success("✅ Dead-letter queue is empty — all workflows recovered automatically!")
    else:
        for item in pending:
            with st.expander(
                f"🔴 [{item['queued_at']}]  **{item['workflow']}**  —  `{item['error_type'] or 'unknown'}`"
            ):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Failure:** `{item['failure_mode']}`")
                    st.markdown(f"**Reason:** {item['reason']}")
                    if item["root_cause"]:
                        st.markdown(f"**Root Cause:** {item['root_cause']}")
                with col2:
                    if item.get("payload"):
                        st.markdown("**Payload at failure:**")
                        st.json(item["payload"])

                c1, c2 = st.columns(2)
                with c1:
                    if st.button(f"✅ Mark Resolved #{item['id']}", key=f"res_{item['id']}"):
                        db.resolve_dlq(item["id"], "human")
                        st.rerun()
                with c2:
                    if st.button(f"🗑️ Dismiss #{item['id']}", key=f"dis_{item['id']}"):
                        db.resolve_dlq(item["id"], "dismissed")
                        st.rerun()

        if resolved:
            with st.expander(f"📁 {len(resolved)} Resolved Items"):
                for item in resolved:
                    st.markdown(
                        f"✅ [{item['resolved_at']}] **{item['workflow']}** — "
                        f"`{item['error_type']}` resolved by `{item['resolved_by']}`"
                    )
