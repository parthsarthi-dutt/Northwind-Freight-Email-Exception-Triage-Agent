# Agentic Workflow Worker Microservice

An independent, scalable worker microservice designed to run in parallel with the **Mail Receive API**.

It listens to the Redis Stream (`email:stream`), pops incoming email messages (which contain **everything except the `status` column**), and executes an agentic workflow (intent analysis, tracking entity extraction, decision making, tool calls).

---

## 🏗️ Architecture

```text
Redis Stream ("email:stream")
          │
          ▼  (XREADGROUP - Consumer Group)
┌───────────────────────────────────────────────┐
│              agentic-workflow                 │
│                                               │
│  1. Deserializes message:                     │
│     - id, from, to, subject, body_text,       │
│       received_at, headers, attachments       │
│     - (Guaranteed NO 'status' field)          │
│                                               │
│  2. Calls EmailAgent.process():               │
│     - Updates status to 'processing' (API)    │
│     - Classifies Intent                       │
│     - Extracts tracking numbers (NWF-20101)   │
│     - Executes resolution / LLM tool call     │
│     - Updates status to 'completed' (API)     │
│                                               │
│  3. Acknowledges stream (XACK)                │
└───────────────────────────────────────────────┘
```

---

## 📁 Folder Structure

```text
agentic-workflow/
├── northwind.db        # Permanent SQLite database containing full Northwind Freight context
├── northwind_db.py     # High-speed contextual query helpers (shipments, events, accounts, policy)
├── load_northwind_to_sqlite.py # Excel to SQLite conversion script
├── agent.py            # Agentic decision engine, entity extraction, status callbacks
├── jev_classifier.py   # Jev-1.14/1.13 decision model with Groq models, tracking grounding, premature checks & harm prevention
├── worker.py           # Redis Stream consumer daemon (XREADGROUP + XACK)
├── config.py           # Configuration (Redis URL, stream name, API URL, Groq models)
├── requirements.txt    # Worker dependencies (redis, requests, python-dotenv, openpyxl, pytest)
├── pytest.ini          # Pytest configuration
├── .env.example        # Environment variables template
└── tests/
    ├── test_agent_worker.py    # Unit & integration tests for agent & stream processing
    ├── test_jev_classifier.py  # Unit tests for prompt injection, medical urgency, premature checks, refund limits
    └── test_northwind_db.py    # Unit tests for Northwind context lookups
```

---

## 🚀 Running the Worker

### 1. Configure `.env`
Copy `.env.example` to `.env`:
```ini
REDIS_URL=redis://localhost:6379/0
REDIS_STREAM_NAME=email:stream
REDIS_CONSUMER_GROUP=agentic_workflow_group
REDIS_CONSUMER_NAME=worker_1
INGESTION_API_URL=http://127.0.0.1:5000
```

### 2. Start the Worker Daemon
```powershell
..\.venv\Scripts\python.exe worker.py
```

### 3. Run Automated Tests
```powershell
..\.venv\Scripts\python.exe -m pytest -v
```
