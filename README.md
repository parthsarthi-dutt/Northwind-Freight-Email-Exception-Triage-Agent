# Northwind Freight — Email Exception Triage Agent & Reviewer Platform

An autonomous, human-in-the-loop email triage system designed for **Northwind Freight**. The system ingests shipment exception emails, grounds reasoning in real-world tracking and account policy data, classifies urgency and intent, drafts professional customer responses, and hands off proposals to human operations reviewers with one-click sign-off.

---

## 🏛️ System Architecture

The architecture consists of two decoupled microservices communicating through **Redis Streams**:

```text
Incoming Webhook (POST /webhook/email)  OR  Gmail IMAP IDLE Push Listener
                                  │
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        mail-receive-api                                │
│                                                                        │
│  - Parses & Validates: sender, recipient, subject, body, metadata      │
│  - Persists State: SQLite database (status = 'pending')                │
│  - Audits & Logs: Structured logger (console + log files)              │
│  - Publishes Event: Redis Stream 'email:stream' (excludes status)      │
│  - Hosts Command Center: Live status dashboard & Simulation Studio    │
│  - Handles Reviewer Actions: One-click sign-off (Approve / Reject)     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                            Redis Stream (XADD)
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        agentic-workflow                                │
│                                                                        │
│  - Consumer Worker: Pops message via XREADGROUP (status-agnostic)      │
│  - 8-Step Reasoning & Policy Pipeline:                                 │
│      1. Intake & Untrusted Boundary Sanitization (Prompt-injection safe)│
│      2. Sender Verification & Authentication Gating                    │
│      3. Resolution Ladder & Consignee Parcel Authorization Gating      │
│      4. Decision Classification & Threat Grounding (JEV Engine)        │
│      5. Deterministic Facts & SLA Commitment Calculation               │
│      6. Policy Engine Evaluation (Refund limits, manager escalation)   │
│      7. Response Drafting (Grounded draft + separated internal actions) │
│      8. Reviewer Email Egress (Dispatches proposal to human reviewer)  │
│  - Persists 7-Card Audit Trail & Updates Case Status                   │
│  - Acknowledges Stream Message: XACK                                   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 📁 Repository Structure

```text
shipsy/
├── README.md                      # Primary project overview, setup, and operations guide
├── SUBMISSION_WRITEUP.md          # 1-2 page design choices & reachability documentation
├── requirements.txt               # Top-level dependencies for local setup
├── .env.example                   # Master environment variables template
├── .gitignore                     # Production ignore rules (excludes secrets, caches, db, archives)
│
├── mail-receive-api/              # Service 1: Ingestion API, Dashboard & Reviewer Egress
│   ├── app/
│   │   ├── __init__.py            # Flask application factory & database initialization
│   │   ├── routes.py              # Webhook, dashboard, audit, and reviewer action routes
│   │   ├── models.py              # Email and audit database models
│   │   ├── email_parser.py        # Email sanitization, header normalization, regex parser
│   │   ├── redis_stream.py        # Redis stream publisher with connection pooling
│   │   ├── logger.py              # Rotating file and console structured logger
│   │   ├── templates/             # Command Center UI (index.html, reviewer action states)
│   │   └── static/                # UI styling and dashboard visual assets
│   ├── run.py                     # Microservice 1 entrypoint (http://127.0.0.1:5000)
│   ├── run_gmail_push_agent.py    # Standalone IMAP IDLE push listener
│   ├── requirements.txt           # Ingestion service dependencies
│   ├── .env.example               # Service 1 configuration template
│   └── tests/                     # 27 passing unit & integration tests
│
└── agentic-workflow/              # Service 2: Agent Decision Worker & Policy Engine
    ├── worker.py                  # Redis consumer daemon
    ├── agent.py                   # 8-step pipeline orchestrator
    ├── jev_classifier.py          # Dual-layer JEV intent, urgency & safety classifier
    ├── response_drafter.py        # Grounded LLM response drafter with strict guardrails
    ├── reviewer_mailer.py         # SMTP email dispatcher with one-click action buttons
    ├── northwind_db.py            # SQLite data-access layer for Northwind operations catalog
    ├── northwind.db               # Pre-seeded SQLite database (shipments, tracking, accounts)
    ├── northwind_data.xlsx        # Ground-truth logistics data workbook
    ├── load_northwind_to_sqlite.py# ETL script to rebuild SQLite from the data workbook
    ├── config.py                  # Service configuration
    ├── requirements.txt           # Worker service dependencies
    ├── .env.example               # Service 2 configuration template
    └── tests/                     # 49 passing unit tests
```

---

## 🛡️ Repository Hygiene: What Is Included vs. What Is Ignored

To maintain security, privacy, and clean version control, this repository follows strict boundaries:

### What Is Included in This Repository:
- **Clean Source Code**: All microservices, routes, agents, classifiers, and models.
- **Architectural & Design Documentation**: High-level overview in `README.md` and detailed evaluation rationale in `SUBMISSION_WRITEUP.md`.
- **Pre-Seeded Reference Catalog**: `agentic-workflow/northwind.db` (read-only SQLite catalog of fictional shipments, tracking events, and account tiers for immediate execution).
- **Comprehensive Automated Test Suite**: 76 unit and integration tests across both microservices.
- **Configuration Templates**: Fully documented `.env.example` templates with no active credentials.

### What Is Excluded & Ignored (via `.gitignore`):
- **Secrets & Credentials**: `.env` files containing real API keys (Groq, Resend) or email passwords.
- **Runtime Databases & Logs**: Ephemeral database instances (`instance/*.db`) and application logs (`logs/*.log`).
- **Virtual Environments & Caches**: `.venv/`, `__pycache__/`, `.pytest_cache/`, and OS temp files.
- **Confidential Assignment Prompts & Raw Scratch**: Placed in local `archive/` (e.g. `assignment-brief.txt`, raw `sample_emails.json`, scratch notes).

---

## 🚀 Quickstart & Setup

### Prerequisites
- Python 3.12+
- Redis server running on `localhost:6379` (or accessible via `REDIS_HOST` / `REDIS_PORT`)

### 1. Environment Configuration
Copy the configuration template and populate your local credentials:
```bash
cp .env.example .env
```
Key configuration values:
- `GROQ_API_KEY`: Groq API key for Llama 3 / Mixtral inference (classification and drafting).
- `REVIEWER_EMAIL`: The operations reviewer's destination email address.
- `SMTP_USER` / `SMTP_PASS`: Credentials used to dispatch proposals to the reviewer.
- `REDIS_HOST` / `REDIS_PORT`: Redis broker location (default `localhost:6379`).

### 2. Install Dependencies
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Launch Service 1 — Ingestion API & Dashboard
In your first terminal:
```bash
cd mail-receive-api
python run.py
```
*The service starts on `http://127.0.0.1:5000`. The Command Center UI is accessible immediately in your browser.*

### 4. Launch Service 2 — Agentic Workflow Worker
In your second terminal:
```bash
cd agentic-workflow
python worker.py
```
*The worker connects to Redis Stream `email:stream` and begins processing incoming emails through the 8-step decision pipeline.*

---

## 📡 Live Demonstration & Reaching the Agent

During the live evaluation session, emails can be submitted directly to the agent via the ingestion webhook.

### Webhook Endpoint
```http
POST http://127.0.0.1:5000/webhook/email
Content-Type: application/json
```

### Sample Payload
```json
{
  "from": {
    "name": "Marcus Webb",
    "email": "marcus.webb@mailbox.example"
  },
  "to": "support@northwind-freight.example",
  "subject": "Parcel NWF-20101 is late",
  "body_text": "Hi, my parcel NWF-20101 was due two days ago and tracking hasn't moved. Where is it?",
  "headers": {},
  "attachments": []
}
```

### Triggering with cURL
```bash
curl -X POST http://127.0.0.1:5000/webhook/email \
  -H "Content-Type: application/json" \
  -d '{
    "from": {"name": "Marcus Webb", "email": "marcus.webb@mailbox.example"},
    "to": "support@northwind-freight.example",
    "subject": "Parcel NWF-20101 is late",
    "body_text": "Hi, my parcel NWF-20101 was due two days ago and tracking hasn'\''t moved. Where is it?"
  }'
```

### Remote Access (ngrok Tunnel)
If the evaluation panel requires remote webhook access, expose the local port:
```bash
ngrok http 5000
```
Use the provided `https://<subdomain>.ngrok.io/webhook/email` as the endpoint.

---

## 🖥️ Live Command Center & Observability

Open **`http://127.0.0.1:5000/`** to view the real-time operational dashboard:
- **Interactive Simulation Studio**: Send custom test emails with arbitrary subjects and bodies to observe the agent's real-time reasoning.
- **Step-by-Step Live Pipeline Tracking**: Watch intake sanitization, sender verification, database queries, JEV classification, and drafting update live.
- **7-Card Audit Inspection**: Inspect exact evidence looked up, facts derived, policy rules evaluated, and the full reviewer egress message.
- **Urgency SLAs & Automatic Escalation**:
  - **P1 Critical (Urgency ≥ 4) or Premium Tier**: Automatically escalates to `sent_to_manager` if under review for > 15 minutes.
  - **Standard Tier**: Automatically escalates to `sent_to_manager` if under review for > 30 minutes.
  - **Unauthorized Inquiries**: Gated at P3 Low (Urgency 1/5) with manager auto-escalation suppressed.

---

## 📬 Human-in-the-Loop Reviewer Sign-Off

1. **Email Dispatched to Reviewer**: An email proposal is sent to `REVIEWER_EMAIL` containing the original email, extracted shipment evidence, derived facts, policy justification, and the draft response.
2. **One-Click Action Buttons**:
   - `[Approve & Send]`: Confirms proposal; marks case as `completed`.
   - `[Reject / Escalate]`: Hands case over for manual intervention; marks case as `escalated`.
3. **Idempotency & Duplicate Protection**: Once clicked, the reviewer receives an immediate confirmation page. Subsequent clicks display a clear warning: *"You have already responded to this review request"*, preventing race conditions or contradictory state changes.

---

## 🧪 Test Suite & Verification

The system includes **76 automated tests** providing complete coverage of safety rules, SLA escalations, database queries, and API endpoints.

### Run All Tests:
```bash
# Service 1: Ingestion API (27 tests)
cd mail-receive-api
python -m pytest -v

# Service 2: Agentic Workflow Worker (49 tests)
cd ../agentic-workflow
python -m pytest -v
```

---

## 📖 Additional Documentation

For detailed analysis of design decisions, safety boundaries, and the evaluation framework, see:
- [`SUBMISSION_WRITEUP.md`](SUBMISSION_WRITEUP.md) — The 1–2 page candidate submission document covering architecture rationale, trade-offs, and reachability.
