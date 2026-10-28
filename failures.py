"""
failures.py — AutoHeal AI
Failure Injection Engine — all injectable failure modes with metadata.
"""
from __future__ import annotations
import random
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FailureMode:
    key:          str
    label:        str
    icon:         str
    description:  str
    category:     str           # data | transient | logic | external
    affects_field: Optional[str] = None
    badge_color:  str = "#6b7280"


FAILURE_MODES: dict[str, FailureMode] = {
    "none": FailureMode(
        key="none", label="No Failure (Happy Path)", icon="✅",
        description="Workflow runs through all steps successfully.",
        category="none", badge_color="#16a34a",
    ),
    "missing_field": FailureMode(
        key="missing_field", label="Missing Required Field", icon="🔴",
        description="A required field (e.g. customer_id / email / client_name) is stripped before execution.",
        category="data", affects_field="key_id_field", badge_color="#dc2626",
    ),
    "invalid_price": FailureMode(
        key="invalid_price", label="Invalid Price / Negative Amount", icon="💸",
        description="A financial field is set to a negative value (-999), triggering a validation error.",
        category="data", affects_field="amount", badge_color="#dc2626",
    ),
    "api_timeout": FailureMode(
        key="api_timeout", label="API / Service Timeout", icon="⏱️",
        description="Downstream payment gateway / LDAP / PDF service is unreachable (HTTP 503).",
        category="transient", badge_color="#d97706",
    ),
    "wrong_data_type": FailureMode(
        key="wrong_data_type", label="Wrong Data Type", icon="🔢",
        description="A numeric field receives a string value (e.g. qty='abc', amount='five-thousand').",
        category="data", affects_field="quantity", badge_color="#dc2626",
    ),
    "duplicate_order": FailureMode(
        key="duplicate_order", label="Duplicate Record / Idempotency Violation", icon="♊",
        description="A record with this ID already exists — unique-constraint violation.",
        category="logic", badge_color="#7c3aed",
    ),
    "null_value": FailureMode(
        key="null_value", label="Null / Empty Required Field", icon="∅",
        description="A required field is present but contains null or an empty string.",
        category="data", badge_color="#dc2626",
    ),
    "network_error": FailureMode(
        key="network_error", label="External Service 503", icon="🌐",
        description="External third-party service returns HTTP 503 Service Unavailable.",
        category="external", badge_color="#ea580c",
    ),
}

CHAOS_POOL = [k for k in FAILURE_MODES if k != "none"]


def random_failure() -> str:
    return random.choice(CHAOS_POOL)


def inject(payload: dict, failure_mode: str, workflow_name: str) -> dict:
    """
    Returns a (possibly mutated) COPY of the payload with the failure injected.
    Keeps original untouched so rollback is always possible.
    """
    import copy
    p = copy.deepcopy(payload)

    if failure_mode == "none":
        return p

    # ── Order Processing ──────────────────────────────────────
    if workflow_name == "Order Processing":
        if failure_mode == "missing_field":
            p.pop("customer_id", None)
        elif failure_mode == "invalid_price":
            p["total_amount"] = -999.0
        elif failure_mode == "wrong_data_type":
            if isinstance(p.get("items"), list) and p["items"]:
                p["items"][0]["qty"] = "not-a-number"
        elif failure_mode == "null_value":
            p["customer_id"] = ""
        elif failure_mode == "duplicate_order":
            p["order_id"] = "DUPLICATE-ORD-001"
        elif failure_mode in ("api_timeout", "network_error"):
            pass  # handled at step level

    # ── Employee Onboarding ───────────────────────────────────
    elif workflow_name == "Employee Onboarding":
        if failure_mode == "missing_field":
            p.pop("email", None)
        elif failure_mode == "invalid_price":
            p["salary"] = -50000
        elif failure_mode == "wrong_data_type":
            p["start_date"] = 99999
        elif failure_mode == "null_value":
            p["name"] = ""
        elif failure_mode == "duplicate_order":
            p["employee_id"] = "DUPLICATE-EMP-001"

    # ── Invoice Generation ────────────────────────────────────
    elif workflow_name == "Invoice Generation":
        if failure_mode == "missing_field":
            p.pop("client_name", None)
        elif failure_mode == "invalid_price":
            p["amount"] = -5000.0
        elif failure_mode == "wrong_data_type":
            p["amount"] = "five-thousand"
        elif failure_mode == "null_value":
            p["client_email"] = ""
        elif failure_mode == "duplicate_order":
            p["invoice_id"] = "DUPLICATE-INV-001"

    return p
