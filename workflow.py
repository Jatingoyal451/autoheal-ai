"""
workflow.py — AutoHeal AI
Step-based workflow engine.
Each workflow is a list of named steps; the engine can resume from any step (step-level retry).
Includes circuit breaker, anomaly detection, and payload snapshot for rollback.
"""
from __future__ import annotations

import time, random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ══════════════════════════════════════════════════════════════
# Step primitives
# ══════════════════════════════════════════════════════════════

@dataclass
class StepOutcome:
    success: bool
    output:  Optional[dict] = None
    error:   Optional[dict] = None


@dataclass
class StepState:
    step_id:     str
    name:        str
    fn:          Callable
    status:      str      = "pending"   # pending|running|success|failed|skipped
    output:      Optional[dict] = None
    error:       Optional[dict] = None
    duration_ms: float    = 0.0
    attempt:     int      = 0


# ══════════════════════════════════════════════════════════════
# Workflow step implementations — ORDER PROCESSING
# ══════════════════════════════════════════════════════════════

def _op_validate(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.25)
    required = ["order_id", "customer_id", "items", "total_amount"]
    for f in required:
        if f not in payload or payload[f] in (None, "", [], {}):
            return StepOutcome(False, error={
                "type": "MissingFieldError",
                "message": f"Required field '{f}' is missing or empty.",
                "field": f, "traceback": f"KeyError: {f}",
            })
    try:
        total = float(payload["total_amount"])
    except (TypeError, ValueError):
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": f"total_amount must be numeric, got '{payload['total_amount']}'.",
            "field": "total_amount", "traceback": "ValueError: cannot convert to float",
        })
    if total <= 0:
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": f"total_amount must be positive, got {total}.",
            "field": "total_amount", "traceback": f"ValueError: total_amount={total}",
        })
    items = payload.get("items", [])
    if not isinstance(items, list) or len(items) == 0:
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": "Order must contain at least one item.",
            "field": "items", "traceback": "ValueError: items list is empty",
        })
    for item in items:
        try:
            int(item.get("qty", 1))
        except (TypeError, ValueError):
            return StepOutcome(False, error={
                "type": "InvalidDataError",
                "message": f"Item qty must be an integer, got '{item.get('qty')}'.",
                "field": "items.qty", "traceback": f"ValueError: qty={item.get('qty')}",
            })
    # Anomaly detection
    for item in items:
        try:
            if int(item.get("qty", 1)) > 500:
                return StepOutcome(False, error={
                    "type": "AnomalyDetected",
                    "message": f"Suspiciously high quantity ({item['qty']}) for SKU {item.get('sku')}. Flagged for review.",
                    "field": "items.qty", "traceback": "AnomalyError: qty > 500",
                })
        except Exception:
            pass
    return StepOutcome(True, output={"validated": True, "items": len(items), "total": total})


def _op_inventory(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.2)
    if failure_mode == "duplicate_order":
        return StepOutcome(False, error={
            "type": "DuplicateError",
            "message": f"Order '{payload.get('order_id')}' already exists.",
            "field": "order_id", "traceback": "UniqueConstraintError",
        })
    return StepOutcome(True, output={"inventory_ok": True, "reserved": True})


def _op_payment(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.2)
    if failure_mode in ("api_timeout", "network_error"):
        label = "Payment gateway timeout" if failure_mode == "api_timeout" else "Payment service 503"
        return StepOutcome(False, error={
            "type": "TransientFailure" if failure_mode == "api_timeout" else "ExternalServiceError",
            "message": f"{label} — upstream service unreachable.",
            "field": None, "traceback": "ConnectionError: service unreachable",
        })
    txn = f"TXN{random.randint(100000, 999999)}"
    return StepOutcome(True, output={"txn_id": txn, "status": "AUTHORISED"})


def _op_confirm(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.1)
    from datetime import datetime
    return StepOutcome(True, output={
        "order_id": payload["order_id"], "status": "CONFIRMED",
        "timestamp": datetime.now().isoformat(),
    })


# ══════════════════════════════════════════════════════════════
# EMPLOYEE ONBOARDING
# ══════════════════════════════════════════════════════════════

def _eo_validate(payload: dict, failure_mode: str) -> StepOutcome:
    import re; time.sleep(0.2)
    required = ["employee_id", "name", "email", "department", "role"]
    for f in required:
        if f not in payload or payload[f] in (None, "", [], {}):
            return StepOutcome(False, error={
                "type": "MissingFieldError",
                "message": f"Required field '{f}' is missing or empty.",
                "field": f, "traceback": f"KeyError: {f}",
            })
    if isinstance(payload.get("start_date"), int):
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": f"start_date must be a date string (YYYY-MM-DD), got integer {payload['start_date']}.",
            "field": "start_date", "traceback": "TypeError: expected str, got int",
        })
    if not re.match(r"[^@]+@[^@]+\.[^@]+", str(payload.get("email", ""))):
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": f"Invalid email format: '{payload.get('email')}'.",
            "field": "email", "traceback": "ValueError: invalid email address",
        })
    return StepOutcome(True, output={"validated": True, "name": payload["name"]})


def _eo_account(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.2)
    if failure_mode in ("api_timeout", "network_error"):
        return StepOutcome(False, error={
            "type": "TransientFailure",
            "message": "LDAP server unreachable — account creation failed.",
            "field": None, "traceback": "ConnectionError: LDAP timeout",
        })
    if failure_mode == "duplicate_order":
        return StepOutcome(False, error={
            "type": "DuplicateError",
            "message": f"Employee '{payload.get('employee_id')}' already exists in the directory.",
            "field": "employee_id", "traceback": "UniqueConstraintError",
        })
    return StepOutcome(True, output={"account": payload["email"], "uid": payload["employee_id"]})


def _eo_role(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.15)
    return StepOutcome(True, output={"role": payload.get("role"), "dept": payload.get("department")})


def _eo_email(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.1)
    return StepOutcome(True, output={"welcome_email": "sent", "to": payload.get("email")})


# ══════════════════════════════════════════════════════════════
# INVOICE GENERATION
# ══════════════════════════════════════════════════════════════

def _inv_validate(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.2)
    required = ["invoice_id", "client_name", "client_email", "amount", "due_date"]
    for f in required:
        if f not in payload or payload[f] in (None, "", [], {}):
            return StepOutcome(False, error={
                "type": "MissingFieldError",
                "message": f"Required field '{f}' is missing from the invoice.",
                "field": f, "traceback": f"KeyError: {f}",
            })
    return StepOutcome(True, output={"validated": True, "invoice_id": payload["invoice_id"]})


def _inv_tax(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.2)
    try:
        amount = float(payload["amount"])
    except (TypeError, ValueError):
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": f"amount must be numeric, got '{payload.get('amount')}'.",
            "field": "amount", "traceback": "ValueError: cannot convert to float",
        })
    if amount <= 0:
        return StepOutcome(False, error={
            "type": "InvalidDataError",
            "message": f"Invoice amount must be positive, got {amount}.",
            "field": "amount", "traceback": f"ValueError: amount={amount}",
        })
    gst   = round(amount * 0.18, 2)
    total = round(amount + gst, 2)
    return StepOutcome(True, output={"subtotal": amount, "gst_18pct": gst, "grand_total": total})


def _inv_pdf(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.2)
    if failure_mode in ("api_timeout", "network_error"):
        return StepOutcome(False, error={
            "type": "ExternalServiceError",
            "message": "PDF rendering service unavailable (HTTP 503).",
            "field": None, "traceback": "ConnectionError: PDF service 503",
        })
    return StepOutcome(True, output={"pdf": f"INV-{payload['invoice_id']}.pdf", "pages": 1})


def _inv_email(payload: dict, failure_mode: str) -> StepOutcome:
    time.sleep(0.1)
    if failure_mode == "duplicate_order":
        return StepOutcome(False, error={
            "type": "DuplicateError",
            "message": f"Invoice '{payload.get('invoice_id')}' was already sent to this client.",
            "field": "invoice_id", "traceback": "DuplicateError: already sent",
        })
    return StepOutcome(True, output={"emailed_to": payload.get("client_email"), "sent": True})


# ══════════════════════════════════════════════════════════════
# Workflow Registry
# ══════════════════════════════════════════════════════════════

WORKFLOW_REGISTRY: dict[str, dict] = {
    "Order Processing": {
        "description": "E-commerce: validate → inventory → payment → confirmation",
        "icon": "🛒",
        "default_payload": {
            "order_id":     "ORD-20261028-001",
            "customer_id":  "CUST-555",
            "items": [
                {"sku": "PROD-A", "qty": 2,  "price": 499.0},
                {"sku": "PROD-B", "qty": 1,  "price": 1299.0},
            ],
            "total_amount": 2297.0,
        },
        "field_defaults": {
            "customer_id":  "CUST-555",
            "total_amount": 2297.0,
            "items":        [{"sku": "PROD-A", "qty": 2, "price": 499.0}],
        },
        "step_defs": [
            ("validate_order",    "Validate Order",       _op_validate),
            ("check_inventory",   "Check Inventory",      _op_inventory),
            ("process_payment",   "Process Payment",      _op_payment),
            ("send_confirmation", "Send Confirmation",    _op_confirm),
        ],
    },
    "Employee Onboarding": {
        "description": "HR: validate → create account → assign role → welcome email",
        "icon": "👤",
        "default_payload": {
            "employee_id": "EMP-2026-042",
            "name":        "Priya Sharma",
            "email":       "priya.sharma@company.com",
            "department":  "Engineering",
            "role":        "Backend Developer",
            "start_date":  "2026-11-01",
        },
        "field_defaults": {
            "email":      "employee@company.com",
            "name":       "New Employee",
            "start_date": "2026-11-01",
            "salary":     60000,
        },
        "step_defs": [
            ("validate_employee", "Validate Employee",        _eo_validate),
            ("create_account",    "Create System Account",    _eo_account),
            ("assign_role",       "Assign Role & Permissions",_eo_role),
            ("send_welcome",      "Send Welcome Email",       _eo_email),
        ],
    },
    "Invoice Generation": {
        "description": "Billing: validate → compute GST → generate PDF → email client",
        "icon": "🧾",
        "default_payload": {
            "invoice_id":   "INV-2026-007",
            "client_name":  "Acme Corp",
            "client_email": "billing@acme.com",
            "amount":       5000.0,
            "due_date":     "2026-11-15",
        },
        "field_defaults": {
            "client_name":  "Client Corp",
            "amount":       5000.0,
            "client_email": "client@example.com",
        },
        "step_defs": [
            ("validate_invoice", "Validate Invoice",   _inv_validate),
            ("compute_tax",      "Compute GST",        _inv_tax),
            ("generate_pdf",     "Generate PDF",       _inv_pdf),
            ("send_email",       "Email to Client",    _inv_email),
        ],
    },
}


def make_steps(workflow_name: str) -> list[StepState]:
    defs = WORKFLOW_REGISTRY[workflow_name]["step_defs"]
    return [StepState(step_id=sid, name=name, fn=fn) for sid, name, fn in defs]


def run_step(step: StepState, payload: dict, failure_mode: str) -> StepOutcome:
    """Execute one step, recording duration and attempt count."""
    step.status  = "running"
    step.attempt += 1
    t0 = time.time()
    outcome = step.fn(payload, failure_mode)
    step.duration_ms = (time.time() - t0) * 1000
    if outcome.success:
        step.status = "success"
        step.output = outcome.output
    else:
        step.status = "failed"
        step.error  = outcome.error
    return outcome
