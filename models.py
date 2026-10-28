"""
models.py — AutoHeal AI
Pydantic v2 models for structured AI diagnosis output.
The AI proposes; the code decides — all fix_action values are whitelisted.
"""

from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, field_validator, model_validator


# ── Enums ─────────────────────────────────────────────────────────────────────

class ErrorType(str, Enum):
    transient = "transient"   # network / timeout — retry cures it
    data      = "data"        # bad payload field — patch cures it
    logic     = "logic"       # business rule violation — escalate
    external  = "external"    # third-party service down — retry
    unknown   = "unknown"     # no idea — escalate


class FixAction(str, Enum):
    fill_default       = "fill_default"        # restore missing field from defaults
    cast_type          = "cast_type"           # coerce field to correct type
    apply_patch        = "apply_patch"         # overwrite field(s) with AI-proposed values
    retry_with_backoff = "retry_with_backoff"  # transient — just retry
    escalate           = "escalate"            # can't auto-fix, needs human


# ── Safety constants ───────────────────────────────────────────────────────────

CONFIDENCE_THRESHOLD: float = 0.75   # below → human approval required

# Fix actions that are ALWAYS safe to auto-apply (no approval needed)
AUTO_APPROVED_ACTIONS: set[FixAction] = {
    FixAction.fill_default,
    FixAction.cast_type,
    FixAction.retry_with_backoff,
}

# Fields that touch money — patching them always requires approval
RISKY_FIELDS: set[str] = {
    "price", "amount", "total", "total_amount",
    "cost", "fee", "discount", "grand_total", "salary",
}


# ── Core diagnosis model ───────────────────────────────────────────────────────

class DiagnosisResult(BaseModel):
    root_cause:    str
    error_type:    ErrorType
    fix_action:    FixAction
    patch:         dict[str, Any] = {}
    confidence:    float
    prevention_tip: str = ""
    explanation:   str  = ""

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("confidence", mode="before")
    @classmethod
    def normalise_confidence(cls, v: Any) -> float:
        """Accept 0.87 or 87 — both mean 87 %."""
        v = float(v)
        if v > 1.0:
            v = v / 100.0
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return round(v, 2)

    @field_validator("fix_action", mode="before")
    @classmethod
    def validate_fix_action(cls, v: Any) -> str:
        # Accept both FixAction enum instances and raw strings
        raw = v.value if isinstance(v, FixAction) else str(v)
        allowed = {fa.value for fa in FixAction}
        if raw not in allowed:
            raise ValueError(
                f"fix_action '{v}' not in whitelist {allowed}. "
                "Defaulting to escalate."
            )
        return raw

    @field_validator("error_type", mode="before")
    @classmethod
    def validate_error_type(cls, v: Any) -> str:
        raw = v.value if isinstance(v, ErrorType) else str(v)
        allowed = {et.value for et in ErrorType}
        if raw not in allowed:
            return "unknown"
        return raw

    # ── Derived properties ────────────────────────────────────────────────────

    def needs_human_approval(self) -> bool:
        """True if this fix should be gated by a human Approve/Reject button."""
        if self.fix_action == FixAction.escalate:
            return True
        if self.confidence < CONFIDENCE_THRESHOLD:
            return True
        if self.fix_action == FixAction.apply_patch:
            patched_keys = {k.lower() for k in self.patch}
            if patched_keys & RISKY_FIELDS:
                return True
        return False

    def approval_reason(self) -> str:
        reasons: list[str] = []
        if self.confidence < CONFIDENCE_THRESHOLD:
            reasons.append(
                f"Low AI confidence ({self.confidence:.0%} < {CONFIDENCE_THRESHOLD:.0%} threshold)"
            )
        if self.fix_action == FixAction.escalate:
            reasons.append("Fix action is 'escalate' — requires human decision")
        patched_keys = {k.lower() for k in self.patch}
        risky = patched_keys & RISKY_FIELDS
        if risky:
            reasons.append(f"Patch modifies financial field(s): {', '.join(sorted(risky))}")
        return " · ".join(reasons)

    def is_retry_strategy(self) -> bool:
        return self.fix_action in (FixAction.retry_with_backoff,)

    def severity(self) -> str:
        if self.error_type in (ErrorType.unknown,):
            return "CRITICAL"
        if self.fix_action == FixAction.escalate:
            return "HIGH"
        if self.confidence < 0.6:
            return "HIGH"
        if self.confidence < CONFIDENCE_THRESHOLD:
            return "MEDIUM"
        return "LOW"

    def severity_color(self) -> str:
        return {
            "CRITICAL": "#dc2626",
            "HIGH":     "#ea580c",
            "MEDIUM":   "#d97706",
            "LOW":      "#16a34a",
        }.get(self.severity(), "#6b7280")


# ── Workflow-level error ───────────────────────────────────────────────────────

class WorkflowError(BaseModel):
    error_type: str
    message:    str
    field:      Optional[str] = None
    step_id:    str
    step_name:  str
    traceback:  str = ""

    def signature(self, workflow_name: str) -> str:
        import hashlib
        # Deterministic key: same error pattern always → same signature
        key = f"{workflow_name}:{self.error_type}:{self.field or 'none'}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]
