# Northwind Freight — Email Exception Triage Agent: Write-Up

---

## What Was Built

At its core, this is a system that sits between Northwind Freight's operations inbox and the human coordinators who staff it. Instead of a coordinator opening each email, reading it, alt-tabbing into the shipment database, deciding what to do, composing a reply, and sending it — the agent does every step up to the actual sending, then hands the finished work to a human reviewer for a single approve-or-reject decision.

The system is split into two independent services that talk to each other through a Redis message queue:

**The first service** (`mail-receive-api`) is a Flask API that accepts emails as JSON via a webhook (`POST /webhook/email`). When an email arrives, it is parsed, saved to a SQLite database with a status of `pending`, logged, and published to a Redis Stream. This service also hosts the operations dashboard — a single-page interface where anyone can see every email that has come in, what the agent decided, and where each case stands. It also has a simulation studio that lets you compose a test email and watch the agent process it in real time, step by step.

**The second service** (`agentic-workflow`) is a background worker that picks emails off the Redis Stream and runs them through an eight-step decision pipeline. Each step is deterministic where it can be and uses an LLM (Groq-hosted models) only where judgment is genuinely needed — classification and drafting. The eight steps, in order:

1. **Intake & Boundary Sanitization** — assigns an immutable Case ID, wraps the email body in untrusted-input delimiters so the LLM cannot be tricked by instructions embedded in customer text.
2. **Sender Verification** — looks up the sender in Northwind's accounts and carriers tables. If the sender is not found, the pipeline stops here. No reply is composed, no data is disclosed. The email is logged and flagged for security review.
3. **Resolution Ladder & Parcel Authorization** — extracts tracking numbers from the email using regex, then verifies each one against the sender's account. If a customer asks about a shipment that belongs to someone else, the agent does not reveal any tracking data; instead, it drafts a privacy notice explaining that the shipment is not registered to their account. If no tracking number is provided at all, the agent resolves the customer's most recent order from their account history, so it can still give a useful answer.
4. **Decision Classification (JEV Engine)** — the email, along with full shipment context and account history, is sent to a Groq LLM that classifies the intent (damage claim, late delivery, refund request, address change, carrier update, etc.), scores urgency on a 1–5 scale grounded in real tracking data, checks for legal or press threats, and detects prompt injection attempts.
5. **Facts & SLA Calculation** — deterministically computes the SLA deadline based on the customer's tier (Premium: 1 hour, Standard: 4 hours), and assembles a set of auditable evidence citations — the tracking reality, refund limits, account claim history — that justify the agent's reasoning.
6. **Policy Engine & Routing** — decides whether the case can be handled by an Operations Coordinator or needs to be escalated to the Operations Manager. Escalation triggers include refund requests exceeding the coordinator's authority ($100 for Standard, $150 for Premium), refunds exceeding the total transaction value, chronic complaint accounts with two or more claims in the last 30 days, legal or press threats, and prompt injection attacks.
7. **Response Drafting** — uses the Groq LLM to compose the actual reply draft, with strict guardrails: never mention internal limits or policy numbers to the customer, never reveal the proof-of-delivery signer's name in disputes, never invent a fake address when a carrier reports one is wrong, never make binding refund promises on behalf of the company. Internal actions (database updates, carrier claims, manager escalations) are separated into a distinct field that never leaks into the customer-facing draft.
8. **Reviewer Email Egress** — sends the complete proposal to the human reviewer's Gmail via SMTP SSL. The email contains the original message, the agent's proposed reply (watermarked so it is never confused with a real sent email), the evidence and reasoning, and two action buttons: **Approve** and **Reject**. The reviewer can decide with a single click. If approved, the case is marked completed. If rejected, it is handed back for manual handling. The buttons are one-time-use — clicking either one a second time shows a confirmation that the reviewer has already responded, preventing duplicate or contradictory decisions. Finally, the complete audit trail is persisted to the database.

### The Choices Behind the Design

**Webhook over mailbox reading.** I chose a webhook-based intake (`POST /webhook/email` accepting JSON) rather than reading a real mailbox. The reason is control and testability: a webhook lets me send any email at any time, with any content, and get an immediate response. During the live session, the panel can send unseen emails to the webhook and watch the pipeline execute in real time. A real IMAP mailbox adds latency, polling complexity, and authentication headaches that do not improve the core problem the agent is solving. (The codebase does include an IMAP IDLE push listener for production readiness, but the webhook is the primary path.)

**What the agent may propose by itself, and what it must escalate.** The agent can draft replies for routine tracking queries, status updates, damage claims, delivery disputes, address changes, and carrier partner communications — essentially any case where the coordinator's authority is sufficient and no unusual risk is present. It must escalate to a manager whenever a refund exceeds the coordinator's dollar threshold, the customer has a pattern of chronic complaints, a legal or media threat is detected, or a prompt injection attack is identified. It must never suggest, promise, or execute a refund. All financial decisions are flagged for human approval. It must never reply to unverified senders. It must never reveal internal policy limits, tier classifications, or proof-of-delivery signer names to customers.

**What the reviewer sees.** The reviewer email is designed so that the reviewer never needs to open the Northwind database, open a separate system, or read more than one screen. It contains: who sent the email and what they said, what the agent found when it looked up the shipment, what category and urgency the agent assigned and why, the full draft reply (clearly watermarked as a proposal), what internal actions should be taken, and two buttons — Approve and Reject. The dashboard provides an even deeper view: a 7-card audit trail showing every step the pipeline executed, every fact it derived, every policy rule it evaluated, and the exact text dispatched to the reviewer.

**When the reviewer is not there.** If the reviewer does not respond, the system does not go silent. For P1 Critical cases (urgency ≥ 4) or Premium-tier customers, if the review sits untouched for more than 15 minutes, the case is automatically escalated to the Operations Manager with a note that the SLA was exceeded. For Standard-tier cases, the threshold is 30 minutes. The escalation is logged in the audit trail with the exact elapsed time and reason. For unauthorized access inquiries (which are low-urgency privacy notices, not operational emergencies), escalation is intentionally suppressed — these are P3 Low (1/5 urgency) and remain with the coordinator regardless of how long they sit, because no customer data is at risk and no SLA is being breached.

**When emails are not what they seem.** Prompt injection attempts — emails that try to trick the LLM into ignoring its instructions, authorizing refunds, or revealing internal data — are caught by both a heuristic regex scanner and the LLM's own evaluation. When detected, the draft is suppressed entirely (no reply is composed), the case is P1-escalated to a security officer, and the injection attempt is logged with its score and matched patterns. Premature complaints (a customer complaining about a late delivery before the promised date has arrived, or filing a damage claim on a shipment that has not been delivered yet) are identified by cross-referencing the complaint against real tracking data, and the automated reply is gated — the inquiry is logged, but no misleading reply goes out.

**How quality is judged.** The system has 76 automated tests across both services (49 in the agentic workflow, 27 in the mail-receive API). These cover every scenario in the decision matrix: unauthorized access, premature complaints, prompt injection, SLA escalation timing, reviewer button idempotency, dynamic timestamps, carrier authorization boundaries, and the full end-to-end simulation flow. Beyond tests, quality is visible: the dashboard exposes the agent's reasoning for every single decision, so a reviewer or auditor can see not just what the agent did, but exactly why, and catch any case where the reasoning does not hold up.

**What was left out.** The agent currently processes one email at a time and does not thread or deduplicate conversations — if a customer sends three follow-ups about the same shipment, each is treated as a new case. With another week, I would add conversation threading (grouping by sender + shipment ID), a feedback loop where reviewer corrections improve future drafts, and batch processing for carrier notices that affect multiple shipments simultaneously. I would also add a confidence score to each draft so that high-confidence routine replies could be auto-approved while low-confidence edge cases are always routed to a human.

---

## How to Reach the Agent

The agent accepts emails via a JSON webhook at:

```
POST http://127.0.0.1:5000/webhook/email
```

**Request format:**
```json
{
  "from": { "name": "Marcus Webb", "email": "marcus.webb@mailbox.example" },
  "to": "support@northwind-freight.example",
  "subject": "Where is my parcel NWF-20101?",
  "body_text": "Hi, parcel NWF-20101 was promised two days ago. Can you check where it is?",
  "received_at": "2026-10-26T19:00:00+05:30",
  "attachments": []
}
```

There is also a simulation endpoint with a built-in dashboard UI:

```
POST http://127.0.0.1:5000/api/simulate/email
```

And the full operations dashboard is at:

```
http://127.0.0.1:5000/
```

**For the live session:** The agent will be running on my local machine before the session starts. The Flask API will be live on `http://127.0.0.1:5000`, the dashboard will be open and visible on screen, and the reviewer email inbox will be open in a browser tab. The panel can send emails to the webhook using any HTTP client (curl, Postman, or the built-in Simulate Sender tab on the dashboard), and we will watch the agent process them in real time — the 7-step live tracker animates each pipeline stage as it executes, and the case audit view opens automatically when processing completes.
