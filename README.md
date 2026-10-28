# 🛡️ AutoHeal AI — Self-Healing Automation System
> **CODE E FIESTA 2026 · Problem Statement 4**  
> Organized by Medicaps University × SlowBros Labs

---

## ✨ What It Does

AutoHeal AI monitors business workflows, detects failures in real time, uses **Google Gemini** to diagnose the root cause, applies a corrected payload patch, and retries — all automatically. Unresolvable issues are escalated to a human with full context.

```
Streamlit UI → Workflow Execution → Error Detection
    → AI Diagnosis (Gemini) → Safe Auto-Fix → Retry
        → Verification → Dashboard / Human Escalation
```

---

## 🚀 Quick Start

### 1 · Install dependencies
```bash
pip install -r requirements.txt
```

### 2 · Set your Gemini API key
```bash
# Windows PowerShell
$env:GEMINI_API_KEY = "AIza..."

# Or create a .env file
echo GEMINI_API_KEY=AIza... > .env
```
> Get a free key at [aistudio.google.com](https://aistudio.google.com)

### 3 · Run the app
```bash
streamlit run app.py
```

---

## 🎬 Demo Scenarios

| Workflow | Failure Mode | Expected Behaviour |
|---|---|---|
| Order Processing | Missing Field | Detects missing `customer_id`, AI patches it, retries successfully |
| Employee Onboarding | Invalid Data | Detects malformed email, AI corrects format, retries |
| Invoice Generation | Transient Failure | Detects PDF service down, AI recommends retry, succeeds on 2nd attempt |
| Any | No Failure | Happy path — completes in a single attempt |

---

## 🗂️ Project Structure

```
self heal/
├── app.py           # Streamlit UI & state machine
├── workflows.py     # 3 simulated workflows + auto-fix engine
├── ai_engine.py     # Gemini diagnosis module
├── database.py      # SQLite persistence (run history)
├── requirements.txt
└── .env.example
```

---

## 🧠 Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| AI | Google Gemini 1.5 Flash |
| Workflows | Pure Python (simulated) |
| Storage | SQLite (via Python stdlib) |
| Env management | python-dotenv |

---

## 🔄 Recovery State Machine

```
IDLE → RUNNING → (success) → DONE
                ↓ (fail)
           DIAGNOSING (Gemini)
                ↓
            FIXING (patch payload)
                ↓
          RETRYING (attempt N)
         ↙           ↘
       DONE       APPROVAL (escalate)
```

---

## ⚙️ Key Features

- ✅ **3 real workflows** with genuine validation logic
- ✅ **3 failure injection modes** — missing field, invalid data, transient
- ✅ **Gemini AI** diagnosis with structured JSON output
- ✅ **Rule-based fallback** if no API key is set
- ✅ **Human approval gate** for high-risk fixes
- ✅ **Configurable max retries** (1–5)
- ✅ **Persistent run history** in SQLite
- ✅ **Recovery metrics dashboard**
