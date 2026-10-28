"""
diagnose.py — AutoHeal AI
Diagnosis pipeline:
  1. Fix Memory lookup (instant — skip LLM)
  2. Gemini LLM (structured JSON → Pydantic)
  3. Rule-based fallback (if LLM unavailable)

Returns (DiagnosisResult, source: 'memory'|'llm'|'fallback')
"""
from __future__ import annotations
import json, os, re
from typing import Optional
from dotenv import load_dotenv

from models import DiagnosisResult, ErrorType, FixAction
import db

load_dotenv()


# ══════════════════════════════════════════════════════════════
# Gemini LLM call
# ══════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """You are AutoHeal, an expert AI system diagnosing business workflow failures.

Respond with ONLY valid JSON — no markdown, no code fences, no commentary:
{
  "root_cause":     "<1-2 sentences: WHY did this error happen?>",
  "error_type":     "transient" | "data" | "logic" | "external" | "unknown",
  "fix_action":     "fill_default" | "cast_type" | "apply_patch" | "retry_with_backoff" | "escalate",
  "patch":          {<ONLY the fields that need to change, with corrected values>},
  "confidence":     <float 0.0–1.0>,
  "prevention_tip": "<1 sentence>",
  "explanation":    "<why you chose this fix_action>"
}

Strict rules:
- MissingFieldError           → fill_default;       patch the missing field with a sensible default
- InvalidDataError (type)     → cast_type;          patch with correct-type value
- InvalidDataError (value)    → apply_patch;        patch with a valid positive value
- TransientFailure            → retry_with_backoff; patch = {}
- ExternalServiceError        → retry_with_backoff; patch = {}
- DuplicateError / AnomalyDetected → escalate;     patch = {}
- confidence: 0.0–1.0 (NOT a percentage like 87)
- If genuinely unsure → escalate with confidence < 0.6
"""


def _call_llm(
    workflow_name: str, workflow_desc: str,
    payload: dict, error: dict, step_name: str,
) -> DiagnosisResult:
    from google import genai
    from google.genai import types as gtypes

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key or api_key.startswith("your_"):
        raise ValueError("GEMINI_API_KEY not configured")

    client = genai.Client(api_key=api_key)

    user_msg = f"""
Workflow : {workflow_name} — {workflow_desc}
Step     : {step_name}
Payload  :
{json.dumps(payload, indent=2)}

Error Type    : {error.get('type')}
Error Message : {error.get('message')}
Affected Field: {error.get('field') or 'N/A'}
Traceback     : {error.get('traceback', '')}
"""
    full_prompt = f"{_SYSTEM_PROMPT}\n\n{user_msg}"
    resp = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=full_prompt,
        config=gtypes.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=512,
        ),
    )
    raw = resp.text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$",          "", raw, flags=re.MULTILINE)

    data = json.loads(raw)
    return DiagnosisResult(**data)


# ══════════════════════════════════════════════════════════════
# Rule-based fallback
# ══════════════════════════════════════════════════════════════

_RULE_TABLE: dict[str, tuple[str, str, float]] = {
    "MissingFieldError":    ("data",      "fill_default",        0.90),
    "InvalidDataError":     ("data",      "cast_type",           0.82),
    "TransientFailure":     ("transient", "retry_with_backoff",  0.92),
    "ExternalServiceError": ("external",  "retry_with_backoff",  0.88),
    "DuplicateError":       ("logic",     "escalate",            0.95),
    "AnomalyDetected":      ("logic",     "escalate",            0.90),
    "UnknownError":         ("unknown",   "escalate",            0.40),
}


def _rule_based(
    error: dict, workflow_name: str, field_defaults: dict,
) -> DiagnosisResult:
    err_type = error.get("type", "UnknownError")
    field    = error.get("field")
    category, action, confidence = _RULE_TABLE.get(
        err_type, ("unknown", "escalate", 0.40)
    )

    patch: dict = {}
    base_field = (field or "").split(".")[0]

    if action in ("fill_default", "cast_type") and base_field in field_defaults:
        patch[base_field] = field_defaults[base_field]

    return DiagnosisResult(
        root_cause=f"[Rule] {error.get('message', 'Unknown error')}",
        error_type=ErrorType(category),
        fix_action=FixAction(action),
        patch=patch,
        confidence=confidence,
        prevention_tip="Add input validation before executing the workflow.",
        explanation="Deterministic rule-based diagnosis applied (LLM unavailable).",
    )


# ══════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════

def diagnose(
    workflow_name: str,
    workflow_desc: str,
    payload: dict,
    error: dict,
    step_name: str,
    field_defaults: dict,
    error_signature: str,
    use_llm: bool = True,
) -> tuple[DiagnosisResult, str]:
    """
    Returns (DiagnosisResult, source) where source ∈ {'memory', 'llm', 'fallback'}.

    Pipeline:
      1. Fix Memory hit  → instant, no LLM
      2. Gemini LLM      → structured JSON → Pydantic
      3. Rule fallback   → deterministic rules
    """
    # ── 1. Fix Memory ────────────────────────────────────────
    cached = db.lookup_fix(error_signature)
    if cached:
        try:
            result = DiagnosisResult(
                root_cause=f"[Memory] Known fix applied — same error pattern seen {cached['use_count']} time(s).",
                error_type=ErrorType(cached["error_type"]),
                fix_action=FixAction(cached["fix_action"]),
                patch=cached["patch"],
                confidence=0.99,
                prevention_tip="This fix was learned from a previous successful recovery.",
                explanation=f"Fix memory hit (signature={error_signature[:8]}…). Skipped LLM call.",
            )
            return result, "memory"
        except Exception:
            pass  # corrupted entry — fall through to LLM

    # ── 2. Gemini LLM ────────────────────────────────────────
    if use_llm:
        try:
            result = _call_llm(workflow_name, workflow_desc, payload, error, step_name)
            return result, "llm"
        except json.JSONDecodeError:
            pass   # fall through to rule-based
        except Exception:
            pass   # API key missing / network error

    # ── 3. Rule-based fallback ────────────────────────────────
    result = _rule_based(error, workflow_name, field_defaults)
    return result, "fallback"
