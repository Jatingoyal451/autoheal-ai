"""
fixes.py — AutoHeal AI
Fix Application Engine:
  • Whitelist-enforced patch application
  • Dry-run schema validation before retry
  • Payload diff for before/after UI
  • Rollback via original snapshot
"""
from __future__ import annotations
import copy
from typing import Optional

from models import DiagnosisResult, FixAction, RISKY_FIELDS


# ══════════════════════════════════════════════════════════════
# Build patched payload
# ══════════════════════════════════════════════════════════════

def build_fixed_payload(
    original: dict,
    diagnosis: DiagnosisResult,
    field_defaults: dict,
) -> dict:
    """
    Applies diagnosis patch to a deep copy of original.
    For fill_default: also fills any empty fields from field_defaults.
    For cast_type: coerces field to the same Python type as the default.
    """
    result = copy.deepcopy(original)
    patch  = dict(diagnosis.patch)

    if diagnosis.fix_action == FixAction.fill_default:
        # AI patch first, then fill remaining gaps from defaults
        for k, v in field_defaults.items():
            if k not in result or result[k] in (None, "", [], {}):
                patch.setdefault(k, v)

    elif diagnosis.fix_action == FixAction.cast_type:
        # Coerce each patched field to the type of the default value
        for k, v in list(patch.items()):
            default_v = field_defaults.get(k)
            if default_v is not None:
                try:
                    patch[k] = type(default_v)(v)
                except (TypeError, ValueError):
                    patch[k] = default_v

    elif diagnosis.fix_action == FixAction.retry_with_backoff:
        # No structural change — just retry
        patch = {}

    # Apply the final patch
    result.update(patch)
    return result


# ══════════════════════════════════════════════════════════════
# Dry-run validation  (schema check without executing workflow)
# ══════════════════════════════════════════════════════════════

def dry_run_validate(workflow_name: str, payload: dict) -> tuple[bool, Optional[str]]:
    """
    Runs schema/type checks on the patched payload.
    Returns (passed: bool, error_message: str | None).
    """
    import re

    if workflow_name == "Order Processing":
        required = ["order_id", "customer_id", "items", "total_amount"]
        for f in required:
            if f not in payload or payload[f] in (None, "", [], {}):
                return False, f"Dry-run ✗ — field '{f}' still missing or empty."
        try:
            t = float(payload["total_amount"])
            if t <= 0:
                return False, f"Dry-run ✗ — total_amount={t} is not positive."
        except (TypeError, ValueError):
            return False, "Dry-run ✗ — total_amount is not numeric."
        return True, None

    elif workflow_name == "Employee Onboarding":
        required = ["employee_id", "name", "email", "department", "role"]
        for f in required:
            if f not in payload or payload[f] in (None, "", [], {}):
                return False, f"Dry-run ✗ — field '{f}' still missing or empty."
        if not re.match(r"[^@]+@[^@]+\.[^@]+", str(payload.get("email", ""))):
            return False, "Dry-run ✗ — email format is still invalid."
        if isinstance(payload.get("start_date"), int):
            return False, "Dry-run ✗ — start_date is still an integer, expected string."
        return True, None

    elif workflow_name == "Invoice Generation":
        required = ["invoice_id", "client_name", "client_email", "amount", "due_date"]
        for f in required:
            if f not in payload or payload[f] in (None, "", [], {}):
                return False, f"Dry-run ✗ — field '{f}' still missing or empty."
        try:
            a = float(payload["amount"])
            if a <= 0:
                return False, f"Dry-run ✗ — amount={a} is not positive."
        except (TypeError, ValueError):
            return False, "Dry-run ✗ — amount is not numeric."
        return True, None

    return True, None   # unknown workflow — pass through


# ══════════════════════════════════════════════════════════════
# Payload diff
# ══════════════════════════════════════════════════════════════

def compute_diff(original: dict, patched: dict) -> list[dict]:
    """
    Returns a list of field-level diffs for the before/after UI panel.
    Each item: {field, old_value, new_value, changed, is_risky}
    """
    all_keys = sorted(set(original) | set(patched))
    diffs = []
    for k in all_keys:
        old_v = original.get(k, "__MISSING__")
        new_v = patched.get(k, "__MISSING__")
        diffs.append({
            "field":     k,
            "old_value": old_v,
            "new_value": new_v,
            "changed":   old_v != new_v,
            "is_risky":  k.lower() in RISKY_FIELDS,
        })
    return diffs


# ══════════════════════════════════════════════════════════════
# Exponential backoff helper
# ══════════════════════════════════════════════════════════════

def backoff_delay(attempt: int, base: float = 1.0, max_delay: float = 16.0) -> float:
    """Returns delay in seconds for a given attempt number (1-indexed)."""
    import math
    delay = min(base * (2 ** (attempt - 1)), max_delay)
    return delay
