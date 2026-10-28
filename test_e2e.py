"""
test_e2e.py - Full end-to-end test for AutoHeal AI
Run: python test_e2e.py
"""
import sys, warnings
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")

import traceback
from workflow  import WORKFLOW_REGISTRY, make_steps, run_step
from failures  import FAILURE_MODES, inject
from diagnose  import diagnose
from fixes     import build_fixed_payload, dry_run_validate, compute_diff
from models    import DiagnosisResult, WorkflowError, ErrorType, FixAction
import db

results = []

def test(name, fn):
    try:
        fn()
        results.append(("PASS", name))
        print(f"  PASS  {name}")
    except AssertionError as e:
        results.append(("FAIL", name))
        print(f"  FAIL  {name}: {e}")
    except Exception as e:
        results.append(("FAIL", name))
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")

# =============================================================
# 1. Database
# =============================================================
print("\n--- Database ---")

def t_db_init():
    db.init_db()
    m = db.fetch_metrics()
    assert isinstance(m["total"], int)

def t_db_fix_memory():
    db.store_fix("sig_test_001", "WF", "data", "f1", "fill_default", {"f1": "v"})
    hit = db.lookup_fix("sig_test_001")
    assert hit is not None
    assert hit["fix_action"] == "fill_default"
    db.mark_fix_success("sig_test_001")

def t_db_dlq():
    db.push_dlq("run_t1", "WF", "none", "data", "root", {"k": "v"}, "reason")
    dlq = db.fetch_dlq()
    assert len(dlq) >= 1

def t_db_audit():
    db.audit("run_t1", "EVT", "system", "description", {"k": 1})
    rows = db.fetch_audit(limit=5)
    assert len(rows) >= 1

test("DB: init + metrics",           t_db_init)
test("DB: fix_memory store/lookup",  t_db_fix_memory)
test("DB: dead-letter queue push",   t_db_dlq)
test("DB: audit log write",          t_db_audit)

# =============================================================
# 2. Failure injection
# =============================================================
print("\n--- Failure Injection ---")

OP  = "Order Processing"
EO  = "Employee Onboarding"
INV = "Invoice Generation"

def t_inject_missing():
    r = inject(WORKFLOW_REGISTRY[OP]["default_payload"], "missing_field", OP)
    assert "customer_id" not in r

def t_inject_price():
    r = inject(WORKFLOW_REGISTRY[OP]["default_payload"], "invalid_price", OP)
    assert r["total_amount"] < 0

def t_inject_type():
    r = inject(WORKFLOW_REGISTRY[OP]["default_payload"], "wrong_data_type", OP)
    assert r["items"][0]["qty"] == "not-a-number"

def t_inject_none():
    orig = WORKFLOW_REGISTRY[OP]["default_payload"]
    r    = inject(orig, "none", OP)
    assert r == orig

def t_inject_null():
    r = inject(WORKFLOW_REGISTRY[OP]["default_payload"], "null_value", OP)
    assert r["customer_id"] == ""

def t_inject_dup():
    r = inject(WORKFLOW_REGISTRY[OP]["default_payload"], "duplicate_order", OP)
    assert "DUPLICATE" in r["order_id"]

test("Inject: missing_field",    t_inject_missing)
test("Inject: invalid_price",    t_inject_price)
test("Inject: wrong_data_type",  t_inject_type)
test("Inject: none is identity", t_inject_none)
test("Inject: null_value",       t_inject_null)
test("Inject: duplicate_order",  t_inject_dup)

# =============================================================
# 3. Happy paths
# =============================================================
print("\n--- Happy Paths ---")

def happy(wf_name):
    p = WORKFLOW_REGISTRY[wf_name]["default_payload"]
    steps = make_steps(wf_name)
    for s in steps:
        o = run_step(s, p, "none")
        assert o.success, f"{s.name} failed: {o.error}"

test("Happy: Order Processing",    lambda: happy(OP))
test("Happy: Employee Onboarding", lambda: happy(EO))
test("Happy: Invoice Generation",  lambda: happy(INV))

# =============================================================
# 4. Failure detection
# =============================================================
print("\n--- Failure Detection ---")

def detect(wf_name, failure_mode, expected_type):
    p     = inject(WORKFLOW_REGISTRY[wf_name]["default_payload"], failure_mode, wf_name)
    steps = make_steps(wf_name)
    for s in steps:
        o = run_step(s, p, failure_mode)
        if not o.success:
            got = o.error["type"]
            assert got == expected_type, f"Expected {expected_type}, got {got}"
            return
    assert False, "Expected failure but workflow succeeded"

test("Detect: missing_field -> MissingFieldError",     lambda: detect(OP, "missing_field",    "MissingFieldError"))
test("Detect: invalid_price -> InvalidDataError",      lambda: detect(OP, "invalid_price",     "InvalidDataError"))
test("Detect: wrong_data_type -> InvalidDataError",    lambda: detect(OP, "wrong_data_type",   "InvalidDataError"))
test("Detect: api_timeout -> TransientFailure",        lambda: detect(OP, "api_timeout",       "TransientFailure"))
test("Detect: network_error -> ExternalServiceError",  lambda: detect(OP, "network_error",     "ExternalServiceError"))
test("Detect: duplicate_order -> DuplicateError",      lambda: detect(OP, "duplicate_order",   "DuplicateError"))
test("Detect: null_value -> MissingFieldError",        lambda: detect(OP, "null_value",        "MissingFieldError"))
test("Detect: onboarding missing -> MissingFieldError",lambda: detect(EO, "missing_field",     "MissingFieldError"))
test("Detect: onboarding api_timeout -> TransientFailure", lambda: detect(EO, "api_timeout",   "TransientFailure"))
test("Detect: invoice missing -> MissingFieldError",   lambda: detect(INV,"missing_field",     "MissingFieldError"))
test("Detect: invoice invalid_price -> InvalidDataError", lambda: detect(INV,"invalid_price",  "InvalidDataError"))

# =============================================================
# 5. Dry-run validation
# =============================================================
print("\n--- Dry-Run Validation ---")

def t_dry_valid():
    p = WORKFLOW_REGISTRY[OP]["default_payload"]
    ok, msg = dry_run_validate(OP, p)
    assert ok, msg

def t_dry_missing_field():
    p = dict(WORKFLOW_REGISTRY[OP]["default_payload"])
    del p["customer_id"]
    ok, _ = dry_run_validate(OP, p)
    assert not ok

def t_dry_negative_amount():
    p = dict(WORKFLOW_REGISTRY[OP]["default_payload"])
    p["total_amount"] = -999
    ok, _ = dry_run_validate(OP, p)
    assert not ok

def t_dry_non_numeric():
    p = dict(WORKFLOW_REGISTRY[OP]["default_payload"])
    p["total_amount"] = "abc"
    ok, _ = dry_run_validate(OP, p)
    assert not ok

def t_dry_invoice_valid():
    p = WORKFLOW_REGISTRY[INV]["default_payload"]
    ok, msg = dry_run_validate(INV, p)
    assert ok, msg

def t_dry_onboarding_bad_email():
    p = dict(WORKFLOW_REGISTRY[EO]["default_payload"])
    p["email"] = "not-an-email"
    ok, _ = dry_run_validate(EO, p)
    assert not ok

test("Dry-run: valid OP payload passes",       t_dry_valid)
test("Dry-run: missing field fails",           t_dry_missing_field)
test("Dry-run: negative amount fails",         t_dry_negative_amount)
test("Dry-run: non-numeric amount fails",      t_dry_non_numeric)
test("Dry-run: valid Invoice payload passes",  t_dry_invoice_valid)
test("Dry-run: bad email fails Onboarding",    t_dry_onboarding_bad_email)

# =============================================================
# 6. Fix building
# =============================================================
print("\n--- Fix Building ---")

def t_fill_default():
    diag = DiagnosisResult(
        root_cause="missing field", error_type=ErrorType.data,
        fix_action=FixAction.fill_default, patch={"customer_id": "CUST-555"},
        confidence=0.92, prevention_tip="", explanation=""
    )
    broken = dict(WORKFLOW_REGISTRY[OP]["default_payload"])
    del broken["customer_id"]
    fixed = build_fixed_payload(broken, diag, WORKFLOW_REGISTRY[OP]["field_defaults"])
    assert "customer_id" in fixed
    assert fixed["customer_id"] == "CUST-555"

def t_cast_type():
    diag = DiagnosisResult(
        root_cause="wrong type", error_type=ErrorType.data,
        fix_action=FixAction.cast_type, patch={"total_amount": "2297.0"},
        confidence=0.85, prevention_tip="", explanation=""
    )
    broken = dict(WORKFLOW_REGISTRY[OP]["default_payload"])
    broken["total_amount"] = "bad-string"
    fixed = build_fixed_payload(broken, diag, WORKFLOW_REGISTRY[OP]["field_defaults"])
    assert isinstance(fixed["total_amount"], float)

def t_retry_no_change():
    diag = DiagnosisResult(
        root_cause="transient", error_type=ErrorType.transient,
        fix_action=FixAction.retry_with_backoff, patch={},
        confidence=0.92, prevention_tip="", explanation=""
    )
    p = WORKFLOW_REGISTRY[OP]["default_payload"]
    fixed = build_fixed_payload(p, diag, WORKFLOW_REGISTRY[OP]["field_defaults"])
    assert fixed == p

test("Fix: fill_default restores field",           t_fill_default)
test("Fix: cast_type converts string to float",    t_cast_type)
test("Fix: retry_with_backoff makes no changes",   t_retry_no_change)

# =============================================================
# 7. Pydantic model validators
# =============================================================
print("\n--- Pydantic Validators ---")

def t_confidence_pct():
    d = DiagnosisResult(root_cause="x", error_type="data", fix_action="fill_default",
                        patch={}, confidence=87, prevention_tip="", explanation="")
    assert d.confidence == 0.87, f"Got {d.confidence}"

def t_bad_fix_action():
    try:
        DiagnosisResult(root_cause="x", error_type="data", fix_action="hack_system",
                        patch={}, confidence=0.8, prevention_tip="", explanation="")
        assert False, "Should have raised"
    except Exception:
        pass

def t_approval_low_conf():
    d = DiagnosisResult(root_cause="x", error_type="data", fix_action="fill_default",
                        patch={}, confidence=0.5, prevention_tip="", explanation="")
    assert d.needs_human_approval()

def t_approval_risky_field():
    d = DiagnosisResult(root_cause="x", error_type="data", fix_action="apply_patch",
                        patch={"amount": 5000}, confidence=0.95, prevention_tip="", explanation="")
    assert d.needs_human_approval()

def t_no_approval_safe():
    d = DiagnosisResult(root_cause="x", error_type="data", fix_action="fill_default",
                        patch={"name": "test"}, confidence=0.90, prevention_tip="", explanation="")
    assert not d.needs_human_approval()

def t_error_signature():
    we = WorkflowError(error_type="MissingFieldError", message="m",
                       field="customer_id", step_id="v", step_name="Validate")
    s1 = we.signature(OP)
    s2 = we.signature(OP)
    s3 = we.signature(EO)
    assert s1 == s2, "Same inputs must give same signature"
    assert s1 != s3, "Different workflow must give different signature"
    assert len(s1) == 16

test("Pydantic: 87 normalised to 0.87",         t_confidence_pct)
test("Pydantic: bad fix_action rejected",        t_bad_fix_action)
test("Pydantic: low confidence -> approval",     t_approval_low_conf)
test("Pydantic: risky field patch -> approval",  t_approval_risky_field)
test("Pydantic: safe fix -> no approval",        t_no_approval_safe)
test("Pydantic: error signature deterministic",  t_error_signature)

# =============================================================
# 8. Full self-heal loop (rule-based, no LLM)
# =============================================================
print("\n--- Full Self-Heal Loop ---")

def full_heal(wf_name, failure_mode):
    """Run a complete detect -> diagnose -> fix -> retry loop."""
    import uuid
    payload  = WORKFLOW_REGISTRY[wf_name]["default_payload"]
    injected = inject(payload, failure_mode, wf_name)

    # Run until failure
    steps, failed_step, err = make_steps(wf_name), None, None
    for s in steps:
        o = run_step(s, injected, failure_mode)
        if not o.success:
            failed_step, err = s, o.error
            break
    assert failed_step, f"Expected failure for {failure_mode} but workflow passed"

    # Build signature
    we  = WorkflowError(error_type=err["type"], message=err["message"],
                        field=err.get("field"), step_id=failed_step.step_id,
                        step_name=failed_step.name)
    sig = we.signature(wf_name)

    # Diagnose (fallback, no LLM)
    diag, src = diagnose(
        wf_name, WORKFLOW_REGISTRY[wf_name]["description"],
        injected, err, failed_step.name,
        WORKFLOW_REGISTRY[wf_name]["field_defaults"],
        sig, use_llm=False,
    )
    assert src == "fallback"

    # Build fixed payload
    fixed  = build_fixed_payload(injected, diag, WORKFLOW_REGISTRY[wf_name]["field_defaults"])
    ok, msg = dry_run_validate(wf_name, fixed)

    if diag.fix_action != FixAction.escalate:
        assert ok, f"Dry-run failed after fix: {msg}"

        # Retry with no failure
        retry_steps = make_steps(wf_name)
        for s in retry_steps:
            o = run_step(s, fixed, "none")
            assert o.success, f"Retry failed at {s.name}: {o.error}"

    # Store in memory
    db.store_fix(sig, wf_name, diag.error_type.value,
                 err.get("field"), diag.fix_action.value, diag.patch)
    db.mark_fix_success(sig)

    # Second run -> memory hit
    _, src2 = diagnose(
        wf_name, WORKFLOW_REGISTRY[wf_name]["description"],
        injected, err, failed_step.name,
        WORKFLOW_REGISTRY[wf_name]["field_defaults"],
        sig, use_llm=False,
    )
    assert src2 == "memory", f"Expected memory hit on 2nd run, got {src2}"

test("Full heal: OP missing_field",   lambda: full_heal(OP,  "missing_field"))
test("Full heal: OP invalid_price",   lambda: full_heal(OP,  "invalid_price"))
test("Full heal: OP api_timeout",     lambda: full_heal(OP,  "api_timeout"))
test("Full heal: OP wrong_data_type", lambda: full_heal(OP,  "wrong_data_type"))
test("Full heal: OP null_value",      lambda: full_heal(OP,  "null_value"))
test("Full heal: EO missing_field",   lambda: full_heal(EO,  "missing_field"))
test("Full heal: EO api_timeout",     lambda: full_heal(EO,  "api_timeout"))
test("Full heal: INV missing_field",  lambda: full_heal(INV, "missing_field"))
test("Full heal: INV invalid_price",  lambda: full_heal(INV, "invalid_price"))

# Diff
def t_diff():
    diffs = compute_diff({"a": 1, "b": "x"}, {"a": 2, "b": "x", "c": "new"})
    changed = [d for d in diffs if d["changed"]]
    assert len(changed) == 2

test("Diff: detects changed and added fields", t_diff)

# =============================================================
# Summary
# =============================================================
total    = len(results)
passed   = sum(1 for r, _ in results if r == "PASS")
failed_n = total - passed

print()
print("=" * 60)
print(f"  Results: {passed}/{total} passed  |  {failed_n} failed")
print("=" * 60)
if failed_n == 0:
    print("  ALL TESTS PASSED - AutoHeal AI is fully functional!")
else:
    fails = [n for r, n in results if r == "FAIL"]
    print("  FAILED TESTS:")
    for f in fails:
        print(f"    - {f}")
