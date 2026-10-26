import os
import sys
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from flask import Blueprint, request, jsonify, render_template, current_app
from app.parser import parse_incoming_email_payload, EmailParseError
from app.models import save_email, get_email_by_id, list_emails, update_email_status
from app.gmail_reader import GmailReaderAgent, GmailReaderError
from app.gmail_push_listener import get_push_listener
from app.logger import log_email_received
from app.redis_stream import publish_email_to_stream

logger = logging.getLogger("mail_receive_api.routes")

# Add agentic-workflow to sys.path so we can leverage audit_builder and EmailAgent
_agentic_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'agentic-workflow'))
if _agentic_dir not in sys.path:
    sys.path.insert(0, _agentic_dir)

bp = Blueprint('api', __name__)

# Global dictionary tracking live simulation progress for each case
LIVE_TRACKING: Dict[int, Dict[str, Any]] = {}


# =============================================================================
# FRONTEND DASHBOARD & SIMULATION PAGES
# =============================================================================

@bp.route('/', methods=['GET'])
@bp.route('/dashboard', methods=['GET'])
@bp.route('/simulate', methods=['GET'])
@bp.route('/case/<int:email_id>', methods=['GET'])
def render_dashboard(email_id: Optional[int] = None):
    """Serves the operations command center dashboard."""
    reviewer_email = os.environ.get('REVIEWER_EMAIL', 'parthsarthidutt@gmail.com')
    return render_template('index.html', reviewer_email=reviewer_email)


# =============================================================================
# CORE WEBHOOK & HEALTH ENDPOINTS
# =============================================================================

@bp.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "healthy",
        "service": "email-webhook-agent"
    }), 200

@bp.route('/webhook/email', methods=['POST'])
def receive_email_webhook():
    """
    Webhook endpoint to ingest incoming email.
    Parses and extracts from, to, subject, body_text, received_at, headers, attachments.
    Persists email to SQLite with status='pending'.
    """
    payload = None
    if request.is_json:
        payload = request.get_json(silent=True)
    elif request.form and "payload" in request.form:
        try:
            payload = json.loads(request.form["payload"])
        except Exception as e:
            return jsonify({
                "error": "Bad Request",
                "message": f"Invalid JSON in form payload: {str(e)}"
            }), 400
    else:
        try:
            data = request.get_data(as_text=True)
            if data:
                payload = json.loads(data)
        except Exception:
            pass

    if payload is None:
        return jsonify({
            "error": "Bad Request",
            "message": "Expected JSON payload or application/json header."
        }), 400

    try:
        extracted = parse_incoming_email_payload(payload)
    except EmailParseError as e:
        return jsonify({
            "error": "Unprocessable Entity",
            "message": str(e)
        }), 422

    # Save to SQLite with status='pending'
    email_id = save_email(extracted)
    saved_email = get_email_by_id(email_id)
    if saved_email is None:
        return jsonify({
            "error": "Internal Server Error",
            "message": "Failed to retrieve saved email record."
        }), 500

    # Log incoming email
    log_email_received(saved_email, source="Webhook")

    # Publish to Redis Stream for downstream agentic workers (without status)
    stream_msg_id = publish_email_to_stream(saved_email)

    return jsonify({
        "message": "Email received and queued successfully",
        "email": saved_email,
        "redis_stream_id": stream_msg_id
    }), 201


# =============================================================================
# REVIEW SLA ESCALATION LOGIC (P1 / PREMIUM 15M, STANDARD 30M)
# =============================================================================

def _parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        clean = str(dt_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def check_review_sla_timeouts() -> int:
    """
    Checks all emails currently in 'under_review' status against SLA thresholds:
    - P1 Critical (urgency >= 4) OR Premium tier: escalated if under review > 15 minutes.
    - Standard tier (urgency < 4): escalated if under review > 30 minutes.
    When SLA threshold is exceeded, status is updated to 'sent_to_manager'.
    Returns the count of escalated records.
    """
    try:
        now = datetime.now(timezone.utc)
        under_review_list = list_emails(status="under_review", limit=500)
        escalated_count = 0

        for email in under_review_list:
            email_id = email.get("id")
            if not email_id:
                continue

            audit = email.get("audit_data") or {}
            if not isinstance(audit, dict):
                audit = {}

            # 0. Unauthorized inquiries exemption:
            # In case of unauthorized, do NOT escalate to manager if not replied and keep urgency 1/5
            is_unauth_case = bool(
                audit.get("category") == "unauthorized_parcel_access"
                or (audit.get("jev_classification") or {}).get("category") == "unauthorized_parcel_access"
                or (audit.get("jev_evaluation") or {}).get("category") == "unauthorized_parcel_access"
                or audit.get("authorization_status") == "unauthorized"
                or audit.get("resolution_method") == "unauthorized"
                or email.get("status") == "unauthorized"
                or audit.get("status") == "unauthorized"
                or (audit.get("unauthorized_ids") and not audit.get("authorized_ids"))
                or (isinstance(audit.get("rejection_details"), dict) and audit["rejection_details"].get("reason_type") == "SECURITY_GATE")
            )
            if is_unauth_case:
                # Unauthorized cases are strictly handled by Coordinator and never escalated to Manager on review timeout
                continue

            # 1. Priority & Urgency: P1 Critical ONLY if urgency >= 4
            jev = audit.get("jev_classification") or audit.get("jev_evaluation") or {}
            urgency_raw = audit.get("urgency") or jev.get("urgency") or 2
            try:
                urgency = int(urgency_raw)
            except Exception:
                urgency = 2
            is_p1 = (urgency >= 4)

            # 2. Tier determination
            tier = (audit.get("policy_compliance") or {}).get("tier")
            if not tier:
                tier = (audit.get("account") or audit.get("customer_account") or {}).get("tier")
            if not tier:
                for fact in audit.get("derived_facts", []):
                    val = str(fact.get("value") or "")
                    title = str(fact.get("title") or "")
                    if "premium" in val.lower() or "premium" in title.lower():
                        tier = "Premium"
                        break
                    elif "standard" in val.lower() or "standard" in title.lower():
                        tier = "Standard"
            if not tier:
                sender_email = (email.get("from") or {}).get("email", "") if isinstance(email.get("from"), dict) else ""
                if not sender_email and "from_email" in email:
                    sender_email = email["from_email"]
                if sender_email:
                    try:
                        from northwind_db import get_account_by_email  # type: ignore
                        acc = get_account_by_email(sender_email)
                        if acc and acc.get("tier"):
                            tier = acc["tier"]
                    except Exception:
                        pass
            if not tier:
                subj_body = f"{email.get('subject', '')} {email.get('body_text', '')}".lower()
                if "premium" in subj_body:
                    tier = "Premium"
                else:
                    tier = "Standard"

            is_premium = (str(tier).strip().lower() == "premium")

            # SLA Threshold: 15 mins for P1 Critical OR Premium Tier; 30 mins for Standard Tier
            threshold_mins = 15 if (is_p1 or is_premium) else 30

            start_dt = None
            if audit.get("under_review_at"):
                start_dt = _parse_iso_datetime(audit["under_review_at"])
            if not start_dt and audit.get("under_review_since"):
                start_dt = _parse_iso_datetime(audit["under_review_since"])
            if not start_dt and email.get("updated_at"):
                start_dt = _parse_iso_datetime(email["updated_at"])
            if not start_dt and email.get("created_at"):
                start_dt = _parse_iso_datetime(email["created_at"])
            if not start_dt and email.get("received_at"):
                start_dt = _parse_iso_datetime(email["received_at"])

            if not start_dt:
                continue

            elapsed_seconds = (now - start_dt).total_seconds()
            elapsed_minutes = elapsed_seconds / 60.0

            if elapsed_minutes >= threshold_mins:
                tier_label = "Premium" if is_premium else "Standard"
                prio_label = "P1 Critical" if is_p1 else "P2 Standard"
                new_status = "sent_to_manager"

                audit["reviewer_status"] = new_status
                audit["escalation_reason"] = f"Review SLA exceeded ({threshold_mins}m limit for {prio_label}/{tier_label}). Auto-escalated to Operations Manager."
                if "pipeline_audit_trail" in audit and isinstance(audit["pipeline_audit_trail"], list):
                    audit["pipeline_audit_trail"].append({
                        "id": "step_manager_sla_escalation",
                        "name": "Manager SLA Escalation",
                        "time_ms": 1,
                        "status": "ESCALATED",
                        "details": f"Review duration ({int(elapsed_minutes)}m) exceeded {threshold_mins}m SLA limit. Case status updated to 'sent_to_manager'."
                    })

                update_email_status(email_id, new_status, audit_data=audit)

                if email_id in LIVE_TRACKING:
                    LIVE_TRACKING[email_id]["status"] = new_status
                    if "audit_data" in LIVE_TRACKING[email_id] and isinstance(LIVE_TRACKING[email_id]["audit_data"], dict):
                        LIVE_TRACKING[email_id]["audit_data"]["reviewer_status"] = new_status
                        LIVE_TRACKING[email_id]["audit_data"]["escalation_reason"] = audit["escalation_reason"]
                    if "step_logs" in LIVE_TRACKING[email_id] and isinstance(LIVE_TRACKING[email_id]["step_logs"], list):
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"⚠️ [SLA EXCEEDED] Review duration exceeded {threshold_mins}m SLA ({tier_label} / {prio_label}). Case status updated to 'sent_to_manager'."
                        )

                escalated_count += 1

        return escalated_count
    except Exception as e:
        logger.warning(f"Error checking review SLA timeouts: {e}")
        return 0


# =============================================================================
# REST API: EMAIL RECORDS & STATUS
# =============================================================================

@bp.route('/api/emails', methods=['GET'])
def get_emails():
    """Lists stored emails, optionally filtered by status (e.g. ?status=pending)."""
    check_review_sla_timeouts()
    status_filter = request.args.get('status')
    limit = request.args.get('limit', default=50, type=int)
    offset = request.args.get('offset', default=0, type=int)

    emails = list_emails(status=status_filter, limit=limit, offset=offset)
    return jsonify({
        "count": len(emails),
        "status_filter": status_filter,
        "emails": emails
    }), 200

@bp.route('/api/emails/<int:email_id>', methods=['GET'])
def get_single_email(email_id: int):
    """Fetches a single email by id."""
    check_review_sla_timeouts()
    email_record = get_email_by_id(email_id)
    if not email_record:
        return jsonify({
            "error": "Not Found",
            "message": f"Email with id {email_id} not found."
        }), 404
    return jsonify(email_record), 200

@bp.route('/api/emails/<int:email_id>/status', methods=['PATCH'])
def change_email_status(email_id: int):
    """Updates the status and optional audit data of an email."""
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    audit_data = data.get("audit_data")
    if not new_status or not isinstance(new_status, str):
        return jsonify({
            "error": "Bad Request",
            "message": "Field 'status' (string) is required."
        }), 400

    updated = update_email_status(email_id, new_status.strip(), audit_data=audit_data)
    if not updated:
        return jsonify({
            "error": "Not Found",
            "message": f"Email with id {email_id} not found."
        }), 404

    return jsonify({
        "message": "Status updated successfully",
        "email": get_email_by_id(email_id)
    }), 200


# =============================================================================
# HUMAN REVIEWER ACTION ENDPOINTS (ACCEPT / REJECT)
# =============================================================================

@bp.route('/api/emails/<int:email_id>/review/action', methods=['GET', 'POST'])
@bp.route('/api/emails/<int:email_id>/review/<action_decision>', methods=['GET', 'POST'])
def handle_reviewer_action(email_id: int, action_decision: Optional[str] = None):
    """
    Handles reviewer action triggered via email CTA buttons or web UI:
    - 'accept': marks status as 'completed', records approval in audit trail.
    - 'reject': marks status as 'passed_to_reviewer', assigns case for manual handling.
    Returns either clean HTML confirmation page (browser GET) or JSON response (API).
    """
    email_record = get_email_by_id(email_id)
    if not email_record:
        if request.is_json or "application/json" in request.headers.get("Accept", ""):
            return jsonify({"error": "Not Found", "message": f"Case #{email_id} not found."}), 404
        return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:40px;text-align:center;">
        <h2 style="color:#dc2626;">Case #{email_id} Not Found</h2>
        <p>The requested email case could not be located in Northwind Freight database.</p>
        </body></html>""", 404

    # Check if this case has already received a reviewer response
    audit = email_record.get("audit_data") or {}
    if not isinstance(audit, dict):
        audit = {}

    already_responded = (
        audit.get("reviewer_decision") is not None
        or email_record.get("status") in ("completed", "passed_to_reviewer", "sent_to_manager", "sent to manager")
    )

    if already_responded:
        prev_decision = audit.get("reviewer_decision") or ("accept" if email_record.get("status") == "completed" else "reject")
        prev_decision_clean = str(prev_decision).strip().lower()
        if prev_decision_clean in ("accept", "approve", "completed"):
            prev_label = "Approved (Completed)"
            prev_bg = "#16a34a"
        else:
            prev_label = "Rejected (Assigned to Reviewer)"
            prev_bg = "#dc2626"

        current_status = email_record.get("status", "completed")
        sender_info = (email_record.get("from") or {}).get("email", "Sender")
        subject_info = email_record.get("subject", "No Subject")

        # Return JSON if requested via API
        if request.is_json or ("application/json" in request.headers.get("Accept", "") and "text/html" not in request.headers.get("Accept", "")):
            return jsonify({
                "success": False,
                "already_responded": True,
                "message": f"You have already responded to Case #{email_id}. Decision cannot be modified.",
                "email_id": email_id,
                "status": current_status,
                "reviewer_decision": prev_decision_clean
            }), 200

        # Return HTML page for browser indicating already responded
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Northwind Freight — Already Responded</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background-color: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 40px 20px;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 80vh;
    }}
    .confirm-card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      max-width: 580px;
      width: 100%;
      padding: 36px 32px;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
      text-align: center;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.5px;
      color: #ffffff;
      background-color: #d97706;
      margin-bottom: 20px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      margin: 0 0 12px 0;
      color: #ffffff;
    }}
    p.summary {{
      font-size: 15px;
      color: #94a3b8;
      line-height: 1.6;
      margin: 0 0 20px 0;
    }}
    .already-box {{
      background: rgba(217, 119, 6, 0.12);
      border: 1px solid #d97706;
      border-radius: 8px;
      padding: 14px 18px;
      font-size: 13px;
      color: #fde68a;
      text-align: left;
      line-height: 1.5;
      margin-bottom: 24px;
    }}
    .disabled-btn-group {{
      display: flex;
      justify-content: center;
      gap: 12px;
      margin-bottom: 24px;
    }}
    .btn-disabled {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      font-weight: 600;
      padding: 10px 18px;
      border-radius: 6px;
      border: 1px solid #475569;
      background: #334155;
      color: #94a3b8;
      cursor: not-allowed;
      opacity: 0.6;
      text-decoration: none;
      user-select: none;
    }}
    .meta-box {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 16px;
      text-align: left;
      font-size: 13px;
      margin-bottom: 24px;
    }}
    .meta-row {{
      display: flex;
      justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px solid #1e293b;
    }}
    .meta-row:last-child {{
      border-bottom: none;
    }}
    .meta-label {{
      color: #64748b;
      font-weight: 600;
    }}
    .meta-val {{
      color: #e2e8f0;
      font-weight: 500;
    }}
    .btn {{
      display: inline-block;
      background-color: #0284c7;
      color: #ffffff;
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      padding: 12px 24px;
      border-radius: 6px;
      transition: background 0.2s;
    }}
    .btn:hover {{
      background-color: #0369a1;
    }}
  </style>
</head>
<body>
  <div class="confirm-card">
    <div class="badge">STATUS: ALREADY RESPONDED</div>
    <h1>You Have Already Responded</h1>
    <p class="summary">You have already submitted a response for Case #CASE-EMAIL-{email_id:04d}. Changes cannot be made again.</p>
    
    <div class="already-box">
      🔒 <strong>Response Locked:</strong> You previously responded with <strong>{prev_label}</strong>. To protect audit integrity, review actions cannot be changed or submitted a second time.
    </div>

    <div class="disabled-btn-group">
      <span class="btn-disabled">✓ Accept Draft (Disabled)</span>
      <span class="btn-disabled">✕ Reject &amp; Handle Manually (Disabled)</span>
    </div>

    <div class="meta-box">
      <div class="meta-row">
        <span class="meta-label">Case ID:</span>
        <span class="meta-val">CASE-EMAIL-{email_id:04d}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Recorded Decision:</span>
        <span class="meta-val" style="color:{prev_bg};font-weight:700;">{prev_label}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Database Status:</span>
        <span class="meta-val" style="color:{prev_bg};font-weight:700;">{current_status.upper()}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Sender:</span>
        <span class="meta-val">{sender_info}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Subject:</span>
        <span class="meta-val">{subject_info}</span>
      </div>
    </div>
    <a href="/case/{email_id}" class="btn">View Case on Operations Dashboard</a>
  </div>
</body>
</html>""", 200

    # Determine decision
    raw_dec = action_decision or request.args.get("decision")
    if not raw_dec and request.is_json:
        raw_dec = (request.get_json(silent=True) or {}).get("decision")
    if not raw_dec and request.form:
        raw_dec = request.form.get("decision")

    decision_clean = str(raw_dec or "accept").strip().lower()

    if decision_clean in ("accept", "approve", "completed"):
        new_status = "completed"
        action_title = "Draft Approved & Case Completed"
        action_badge = "STATUS: COMPLETED"
        badge_bg = "#16a34a"
        action_summary = "You have accepted the proposed AI response draft. The email status has been marked as COMPLETED in Northwind Freight Operations."
        reviewer_decision_log = "Accepted AI draft; marked status='completed'"
    elif decision_clean in ("reject", "passed_to_reviewer", "reviewer", "manual", "assign"):
        new_status = "passed_to_reviewer"
        action_title = "Draft Rejected & Assigned to Reviewer"
        action_badge = "STATUS: ASSIGNED TO REVIEWER"
        badge_bg = "#dc2626"
        action_summary = "You have rejected the AI response. The case status has been set to PASSED TO REVIEWER and routed directly to your queue for manual handling."
        reviewer_decision_log = "Rejected AI draft; reassigned to human reviewer for manual resolution"
    else:
        return jsonify({
            "error": "Bad Request",
            "message": f"Invalid review decision '{decision_clean}'. Must be 'accept' or 'reject'."
        }), 400

    # Update audit data
    audit["reviewer_status"] = new_status
    audit["reviewer_decision"] = decision_clean
    audit["reviewer_action_timestamp"] = datetime.now(timezone.utc).isoformat()
    if "pipeline_audit_trail" in audit and isinstance(audit["pipeline_audit_trail"], list):
        audit["pipeline_audit_trail"].append({
            "id": "step_human_review",
            "name": "Human Reviewer Decision",
            "time_ms": 1,
            "status": "COMPLETED",
            "details": f"Reviewer Action: {reviewer_decision_log}. Database updated at {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}."
        })

    # Update database
    update_email_status(email_id, new_status, audit_data=audit)

    if email_id in LIVE_TRACKING:
        LIVE_TRACKING[email_id]["status"] = new_status
        if "audit_data" in LIVE_TRACKING[email_id] and isinstance(LIVE_TRACKING[email_id]["audit_data"], dict):
            LIVE_TRACKING[email_id]["audit_data"]["reviewer_status"] = new_status
            LIVE_TRACKING[email_id]["audit_data"]["reviewer_decision"] = decision_clean

    # Return JSON if requested via API
    if request.is_json or ("application/json" in request.headers.get("Accept", "") and "text/html" not in request.headers.get("Accept", "")):
        return jsonify({
            "success": True,
            "email_id": email_id,
            "status": new_status,
            "action_title": action_title,
            "message": action_summary
        }), 200

    # Otherwise return beautiful, responsive executive confirmation HTML page
    sender_info = (email_record.get("from") or {}).get("email", "Sender")
    subject_info = email_record.get("subject", "No Subject")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Northwind Freight — Review Action Recorded</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background-color: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 40px 20px;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 80vh;
    }}
    .confirm-card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      max-width: 580px;
      width: 100%;
      padding: 36px 32px;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
      text-align: center;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.5px;
      color: #ffffff;
      background-color: {badge_bg};
      margin-bottom: 20px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      margin: 0 0 12px 0;
      color: #ffffff;
    }}
    p.summary {{
      font-size: 15px;
      color: #94a3b8;
      line-height: 1.6;
      margin: 0 0 20px 0;
    }}
    .meta-box {{
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 16px;
      text-align: left;
      font-size: 13px;
      margin-bottom: 24px;
    }}
    .meta-row {{
      display: flex;
      justify-content: space-between;
      padding: 6px 0;
      border-bottom: 1px solid #1e293b;
    }}
    .meta-row:last-child {{
      border-bottom: none;
    }}
    .meta-label {{
      color: #64748b;
      font-weight: 600;
    }}
    .meta-val {{
      color: #e2e8f0;
      font-weight: 500;
    }}
    .btn {{
      display: inline-block;
      background-color: #0284c7;
      color: #ffffff;
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      padding: 12px 24px;
      border-radius: 6px;
      transition: background 0.2s;
    }}
    .btn:hover {{
      background-color: #0369a1;
    }}
  </style>
</head>
<body>
  <div class="confirm-card">
    <div class="badge">{action_badge}</div>
    <h1>{action_title}</h1>
    <p class="summary">{action_summary}</p>
    
    <div style="display: flex; justify-content: center; gap: 12px; margin-bottom: 24px;">
      <span style="display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; padding: 10px 18px; border-radius: 6px; border: 1px solid #475569; background: #334155; color: #94a3b8; cursor: not-allowed; opacity: 0.6; text-decoration: none;">
        ✓ Accept Draft (Disabled)
      </span>
      <span style="display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; padding: 10px 18px; border-radius: 6px; border: 1px solid #475569; background: #334155; color: #94a3b8; cursor: not-allowed; opacity: 0.6; text-decoration: none;">
        ✕ Reject &amp; Handle Manually (Disabled)
      </span>
    </div>

    <div class="meta-box">
      <div class="meta-row">
        <span class="meta-label">Case ID:</span>
        <span class="meta-val">CASE-EMAIL-{email_id:04d}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Sender:</span>
        <span class="meta-val">{sender_info}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Subject:</span>
        <span class="meta-val">{subject_info}</span>
      </div>
      <div class="meta-row">
        <span class="meta-label">Database Status:</span>
        <span class="meta-val" style="color:{badge_bg};font-weight:700;">{new_status.upper()}</span>
      </div>
    </div>
    <a href="/case/{email_id}" class="btn">View Case on Operations Dashboard</a>
  </div>
</body>
</html>""", 200


# =============================================================================
# CASE AUDIT TRAIL ENDPOINT (SCREENSHOT 7-CARD VIEW DATA)
# =============================================================================

@bp.route('/api/emails/<int:email_id>/audit', methods=['GET'])
def get_email_audit(email_id: int):
    """
    Returns the complete case audit payload matching the operations dashboard:
    Category, Assigned role, Resolution ladder, Derived facts (FACT-1 to FACT-N),
    Pipeline execution audit trail with millisecond timings, Proposal draft,
    Rejection/hold justification, and Reviewer egress status.
    """
    check_review_sla_timeouts()
    if email_id in LIVE_TRACKING and LIVE_TRACKING[email_id].get("audit_data"):
        return jsonify(LIVE_TRACKING[email_id]["audit_data"]), 200

    email_record = get_email_by_id(email_id)
    if not email_record:
        return jsonify({
            "error": "Not Found",
            "message": f"Email with id {email_id} not found."
        }), 404

    audit = email_record.get("audit_data")
    if isinstance(audit, dict) and audit:
        return jsonify(audit), 200

    if isinstance(audit, str) and audit.strip():
        try:
            parsed = json.loads(audit)
            if isinstance(parsed, dict) and parsed:
                return jsonify(parsed), 200
        except Exception:
            pass

    # If audit_data was not populated yet (e.g. pending record), evaluate via EmailAgent
    try:
        from agent import EmailAgent  # type: ignore
        agent = EmailAgent()
        worker_payload = {k: v for k, v in email_record.items() if k != "status"}
        decision = agent.process(worker_payload)
        audit_obj = decision.get("audit_data")
        if audit_obj:
            final_status = decision.get("status") or ("under_review" if decision.get("requires_reply") else "completed")
            if final_status == "under_review" and isinstance(audit_obj, dict):
                audit_obj["under_review_at"] = audit_obj.get("under_review_at") or datetime.now(timezone.utc).isoformat()
            update_email_status(email_id, final_status, audit_data=audit_obj)
            return jsonify(audit_obj), 200
    except Exception as e:
        logger.warning(f"Could not run live agent evaluation for email {email_id}: {e}")

    # Fallback to build_case_audit so all 7 card sections are guaranteed
    try:
        from audit_builder import build_case_audit  # type: ignore
        from northwind_db import verify_sender_email, get_shipment_context, get_account_by_email, get_carrier_by_email  # type: ignore
        from agent import EmailAgent  # type: ignore

        sender_email = (email_record.get("from") or {}).get("email", "")
        sender_name = (email_record.get("from") or {}).get("name", "")
        verification = verify_sender_email(sender_email)
        sender_type = verification.get("sender_type", "unknown")
        account = get_account_by_email(sender_email)
        carrier = get_carrier_by_email(sender_email)

        subject_body = f"{email_record.get('subject', '')} {email_record.get('body_text', '')}"
        tracking_ids = EmailAgent().extract_tracking_numbers(subject_body)

        resolution_method = "exact_id"
        account_id = str(account.get("account_id")) if account and account.get("account_id") is not None else None
        if not tracking_ids and sender_type == "customer" and account and account_id is not None:
            from northwind_db import get_shipments_by_account  # type: ignore
            account_shipments = get_shipments_by_account(account_id, customer_name=account.get("contact"))
            if account_shipments:
                most_recent = account_shipments[0]
                if most_recent.get("shipment_id"):
                    tracking_ids = [most_recent["shipment_id"]]
                    resolution_method = "account_history"

        # Look up all tracking contexts in Northwind DB
        affected_shipments = []
        primary_ship_ctx = None
        for tid in tracking_ids:
            ctx = get_shipment_context(tid)
            if ctx and ctx.get("shipment"):
                affected_shipments.append(ctx["shipment"])
                if not primary_ship_ctx:
                    primary_ship_ctx = ctx

        tracking_payload = {
            "shipment": primary_ship_ctx.get("shipment") if primary_ship_ctx else {},
            "shipments": affected_shipments
        }

        authorized_ids = tracking_ids
        unauthorized_ids = []
        if sender_type == "carrier" and carrier:
            from agent import validate_carrier_shipment_authorization  # type: ignore
            auth_res = validate_carrier_shipment_authorization(carrier, tracking_ids)
            authorized_ids = auth_res["authorized_ids"]
            unauthorized_ids = auth_res["unauthorized_ids"]
        elif sender_type == "customer" and account:
            from agent import validate_customer_shipment_authorization  # type: ignore
            auth_res = validate_customer_shipment_authorization(account, tracking_ids)
            authorized_ids = auth_res["authorized_ids"]
            unauthorized_ids = auth_res["unauthorized_ids"]

        parcels_str = ", ".join(authorized_ids) if authorized_ids else (tracking_ids[0] if tracking_ids else "in-transit shipment")
        is_carrier = sender_type == "carrier" or bool(carrier)

        if is_carrier:
            carrier_name = (carrier or {}).get("carrier", "Carrier Partner")
            category = "carrier_or_partner"
            action_taken = f"Logged operational exception from carrier {carrier_name} for authorized shipments: {parcels_str}."
            
            other_carrier_note = ""
            if unauthorized_ids:
                other_carrier_note = (
                    f" (Note: In our freight network, shipment(s) {', '.join(unauthorized_ids)} are assigned to separate contracted carrier partners; "
                    f"our operations dispatch is reviewing them internally)."
                )

            draft_reply = (
                f"Dear {carrier_name} Operations Team,\n\n"
                f"Thank you for notifying Northwind Freight regarding the operational transit update.\n\n"
                f"We have recorded this update in our tracking system for your assigned shipment(s): {parcels_str}.{other_carrier_note}\n\n"
                f"Our customer care team has been alerted to proactively notify affected consignees per Policy 2.1.\n\n"
                f"Please keep us informed of any further transit updates.\n\n"
                f"Best regards,\nNorthwind Operations Team"
            )
            suggested_actions = [
                f"Update shipment notes in Northwind DB for authorized parcels {authorized_ids}. Proactively alert affected consignees per Policy 2.1."
            ]
            if unauthorized_ids:
                suggested_actions.append(
                    f"Dispatch Audit: Carrier {carrier_name} reported delays for {unauthorized_ids}, which belong to Northwind but are assigned to other carriers in DB. Verify trailer transfer or interline status."
                )
            escalation_reasons = ["POLICY-2.1: Proactive notification", "POLICY-4.1: Standard SLA turnaround"]
        elif not authorized_ids and unauthorized_ids:
            cust_name = (account or {}).get("contact") or sender_name or "Valued Customer"
            unauth_str = ", ".join(unauthorized_ids)
            category = "unauthorized_parcel_access"
            action_taken = f"Consignee Privacy Notice: Shipment ID(s) {unauth_str} do not belong to customer ({cust_name}). Privacy notice drafted for reviewer sign-off."
            draft_reply = (
                f"Dear {cust_name},\n\n"
                f"Thank you for reaching out to Northwind Freight.\n\n"
                f"Regarding shipment ID(s) {unauth_str}, our records indicate that these tracking numbers are not associated with your customer account. "
                f"Under Northwind's Consignee Privacy & Account Verification policy (Policy 1.2), we are unable to disclose tracking details or status updates for shipments registered under other accounts.\n\n"
                f"If you believe this is in error, please verify the tracking number with your merchant or sender.\n\n"
                f"Best regards,\nNorthwind Customer Care Team"
            )
            suggested_actions = [
                f"Consignee Privacy Notice: Queried shipment(s) {unauthorized_ids} do not belong to account {cust_name}. Privacy notice sent."
            ]
            escalation_reasons = ["POLICY-1.2: Consignee privacy and authorization enforcement"]
        else:
            cust_name = (account or {}).get("contact") or sender_name or "Valued Customer"
            category = "status_query" if tracking_ids else "inquiry"
            action_taken = f"Case recorded with status '{email_record.get('status', 'pending')}'. Tracking checked for {parcels_str}."
            draft_reply = (
                f"Dear {cust_name},\n\n"
                f"Thank you for contacting Northwind Freight. We are actively reviewing your inquiry regarding shipment {parcels_str}.\n\n"
                f"We are monitoring the transit progress and will provide you with updates as new milestones are scanned.\n\n"
                f"Best regards,\nNorthwind Customer Care Team"
            )
            suggested_actions = [f"Review inquiry with operations coordinator for {parcels_str}."]
            escalation_reasons = ["POLICY-4.1: Standard SLA turnaround"]

        unauth_detected = bool((not authorized_ids and unauthorized_ids) or category == "unauthorized_parcel_access")
        fallback_decision = {
            "workflow_routed_to": f"{sender_type}_workflow",
            "sender_type": sender_type,
            "resolution_method": resolution_method,
            "authorized_ids": authorized_ids,
            "unauthorized_ids": unauthorized_ids,
            "requires_reply": True,
            "urgency": 1 if unauth_detected else 2,
            "needs_manager_approval": False,
            "action_taken": action_taken,
            "draft_reply": draft_reply,
            "suggested_internal_actions": suggested_actions,
            "jev_evaluation": {
                "category": category,
                "urgency": 1 if unauth_detected else 2,
                "needs_manager_approval": False,
                "threat": {"has_threat": False, "grounded_in_tracking": False},
                "injection_defense": {"has_injection_flag": False},
                "policy_compliance": {
                    "tier": (account or {}).get("tier", "Standard") if not is_carrier else "Carrier Partner",
                    "sla_hours": 1 if (account or {}).get("tier") == "Premium" else 4,
                    "escalation_reasons": escalation_reasons
                }
            }
        }
        fallback_audit = build_case_audit(
            email_record,
            fallback_decision,
            shipment_context=tracking_payload,
            account_context=account,
            carrier_context=carrier,
            reviewer_email=os.environ.get('REVIEWER_EMAIL', 'parthsarthidutt@gmail.com')
        )
        return jsonify(fallback_audit), 200
    except Exception as err:
        logger.error(f"Fallback audit building failed: {err}")

    return jsonify({
        "case_id": f"CASE-EMAIL-{email_id:04d}",
        "category": "general_inquiry",
        "assigned_role": "Operations Coordinator",
        "resolution_method": "exact_id",
        "incoming_email": {
            "sender_name": (email_record.get("from") or {}).get("name", "Sender"),
            "sender_email": (email_record.get("from") or {}).get("email", ""),
            "subject": email_record.get("subject", ""),
            "received_at": email_record.get("received_at", ""),
            "body_text": email_record.get("body_text", "")
        },
        "derived_facts": [],
        "pipeline_audit_trail": [],
        "requires_reply": False
    }), 200


# =============================================================================
# SIMULATION STUDIO & REAL-TIME ACTIVITY TRACKER
# =============================================================================

def _run_agent_simulation_worker(email_id: int, app_context_fn):
    """Executes the agentic pipeline in background with live stage tracking."""
    with app_context_fn():
        try:
            saved = get_email_by_id(email_id)
            if not saved:
                return

            # Step 3: Worker picked up from Redis Stream
            time.sleep(0.4)
            if email_id in LIVE_TRACKING:
                LIVE_TRACKING[email_id]["current_step"] = 3
                LIVE_TRACKING[email_id]["step_logs"].append(
                    f"[Step 3/7] Background worker (PID: {os.getpid()}) popped message from Redis Stream."
                )

            # Step 4: Verification & Resolution Ladder
            time.sleep(0.3)
            from northwind_db import verify_sender_email  # type: ignore
            sender_email = (saved.get("from") or {}).get("email", "")
            verification = verify_sender_email(sender_email)
            if email_id in LIVE_TRACKING:
                LIVE_TRACKING[email_id]["current_step"] = 4
                LIVE_TRACKING[email_id]["step_logs"].append(
                    f"[Step 4/7] Northwind DB: Sender verification -> {verification['sender_type'].upper()} ({verification['message']})."
                )

            # Run agent.process to evaluate workflow and authorization
            from agent import EmailAgent  # type: ignore
            agent = EmailAgent()
            worker_payload = {k: v for k, v in saved.items() if k != "status"}
            decision = agent.process(worker_payload)
            
            audit_obj = decision.get("audit_data")
            is_unverified = decision.get("verification_status") == "unverified" or decision.get("final_status") == "unverified"
            is_unauthorized = decision.get("authorization_status") == "unauthorized" or decision.get("final_status") == "unauthorized"

            if is_unverified:
                final_status = "unverified"
            elif decision.get("requires_reply"):
                final_status = "under_review"
                if isinstance(audit_obj, dict):
                    audit_obj["under_review_at"] = audit_obj.get("under_review_at") or datetime.now(timezone.utc).isoformat()
            elif is_unauthorized:
                final_status = "unauthorized"
            else:
                final_status = "completed"
            
            # Immediately persist directly to SQLite database
            update_email_status(email_id, final_status, audit_data=audit_obj)
            
            if email_id in LIVE_TRACKING:
                LIVE_TRACKING[email_id]["audit_data"] = audit_obj
                LIVE_TRACKING[email_id]["status"] = final_status
                
                if is_unverified:
                    LIVE_TRACKING[email_id]["current_step"] = 4
                    LIVE_TRACKING[email_id]["step_logs"].append(
                        f"🛑 [HALTED at Step 4] Unverified Sender: '{sender_email}' not found in Northwind DB. Workflow halted. No reply permitted."
                    )
                elif is_unauthorized and not decision.get("requires_reply"):
                    LIVE_TRACKING[email_id]["current_step"] = 4
                    unauth_list = decision.get('unauthorized_ids', [])
                    LIVE_TRACKING[email_id]["step_logs"].append(
                        f"🚫 [HALTED at Step 4] Unauthorized Parcel Access: Parcel(s) {unauth_list} do not belong to account. Workflow halted per Consignee Privacy (Policy 1.2). No reply permitted."
                    )
                else:
                    LIVE_TRACKING[email_id]["current_step"] = 7
                    reviewer = os.environ.get("REVIEWER_EMAIL", "parthsarthidutt@gmail.com")
                    if is_unauthorized and decision.get("requires_reply"):
                        unauth_list = decision.get('unauthorized_ids', [])
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"[Step 4/7] Northwind DB: Consignee Privacy Notice triggered. Parcel(s) {unauth_list} do not belong to account (Policy 1.2)."
                        )
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"[Step 5-6/7] Consignee Privacy Notice drafted: Informs customer they are not allowed to access parcel(s) {unauth_list}."
                        )
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"[Step 7/7] Dispatch complete! Final Status: 'under_review'. Privacy notice dispatched to human reviewer ({reviewer})."
                        )
                    elif decision.get("requires_reply"):
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"[Step 5-6/7] JEV Groq LLM evaluated category '{decision.get('category', 'inquiry')}'. Proposal draft generated."
                        )
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"[Step 7/7] Dispatch complete! Final Status: '{final_status}'. Dispatched to human reviewer ({reviewer})."
                        )
                    else:
                        LIVE_TRACKING[email_id]["step_logs"].append(
                            f"[Step 5-7/7] Workflow complete! Final Status: '{final_status}'. Automated reply suppressed per policy."
                        )
                LIVE_TRACKING[email_id]["is_complete"] = True

        except Exception as e:
            if email_id in LIVE_TRACKING:
                LIVE_TRACKING[email_id]["step_logs"].append(f"❌ Worker Execution Error: {str(e)}")
                LIVE_TRACKING[email_id]["is_complete"] = True

@bp.route('/api/simulate/email', methods=['POST'])
def simulate_email():
    """
    Simulates incoming email from any sender (customer or carrier).
    Saves to DB, queues to Redis Stream, tracks live execution, and processes inline.
    """
    payload = request.get_json(silent=True) or {}
    from_name = (payload.get("from_name") or "Marcus Webb").strip()
    from_email = (payload.get("from_email") or "marcus.webb@mailbox.example").strip()
    to_address = (payload.get("to_address") or "support@northwind-freight.example").strip()
    subject = (payload.get("subject") or "Status Inquiry").strip()
    body_text = (payload.get("body_text") or "").strip()
    attachments = payload.get("attachments") or []
    process_inline = bool(payload.get("process_inline", True))

    received_at_input = (payload.get("received_at") or "").strip()
    if not received_at_input:
        received_at_input = datetime.now(timezone.utc).isoformat()

    extracted = {
        "from": {"name": from_name, "email": from_email},
        "to": [{"name": "Northwind Support", "email": to_address}],
        "subject": subject,
        "body_text": body_text,
        "received_at": received_at_input,
        "headers": {},
        "attachments": attachments
    }

    email_id = save_email(extracted)
    saved_email = get_email_by_id(email_id)
    if saved_email is None:
        return jsonify({
            "error": "Internal Server Error",
            "message": "Failed to retrieve saved email record."
        }), 500

    # Log incoming simulation
    log_email_received(saved_email, source="Simulation-Webhook")

    # Publish to Redis stream
    stream_msg_id = publish_email_to_stream(saved_email)

    # Initialize live tracking
    LIVE_TRACKING[email_id] = {
        "email_id": email_id,
        "current_step": 2,
        "step_logs": [
            f"[Step 1/7] Ingested message via Webhook API from '{from_name} <{from_email}>'. Persisted to SQLite as CASE #{email_id}.",
            f"[Step 2/7] Queued into Redis Stream 'email_stream' (Stream ID: {stream_msg_id or 'mock-stream-id'})."
        ],
        "is_complete": False,
        "status": "pending"
    }

    if process_inline:
        app = getattr(current_app, "_get_current_object", lambda: current_app)()
        t = threading.Thread(
            target=_run_agent_simulation_worker,
            args=(email_id, app.app_context),
            daemon=True
        )
        t.start()

    return jsonify({
        "message": "Simulated email ingested and queued successfully",
        "email_id": email_id,
        "redis_stream_id": stream_msg_id
    }), 201

@bp.route('/api/emails/<int:email_id>/live-status', methods=['GET'])
def get_live_status(email_id: int):
    """Returns the real-time stage progress and live logs for an email."""
    if email_id in LIVE_TRACKING:
        return jsonify(LIVE_TRACKING[email_id]), 200

    saved = get_email_by_id(email_id)
    if not saved:
        return jsonify({"error": "Not Found", "message": f"Email #{email_id} not found."}), 404

    status = saved.get("status", "pending")
    is_done = status != "pending"
    return jsonify({
        "email_id": email_id,
        "current_step": 7 if is_done else 2,
        "step_logs": [
            f"Case #{email_id} recorded in database.",
            f"Current status: {status}."
        ],
        "is_complete": is_done,
        "status": status
    }), 200


# =============================================================================
# GMAIL SYNC & PUSH LISTENER ENDPOINTS
# =============================================================================

@bp.route('/api/gmail/status', methods=['GET'])
def check_gmail_status():
    """Checks Gmail configuration and tests connection."""
    agent = GmailReaderAgent()
    if not agent.is_configured():
        return jsonify({
            "configured": False,
            "message": "Gmail credentials (GMAIL_EMAIL, GMAIL_APP_PASSWORD) not set in environment or .env."
        }), 200
    try:
        status_info = agent.test_connection()
        return jsonify({
            "configured": True,
            "connection": status_info
        }), 200
    except GmailReaderError as e:
        return jsonify({
            "configured": True,
            "connected": False,
            "error": str(e)
        }), 502

@bp.route('/api/gmail/sync', methods=['POST'])
def sync_gmail_emails():
    """
    Triggers an on-demand sync from real Gmail inbox.
    Fetches unseen emails, extracts structured data, and saves to SQLite with status='pending'.
    """
    agent = GmailReaderAgent()
    if not agent.is_configured():
        return jsonify({
            "error": "Configuration Error",
            "message": "Gmail credentials are not configured. Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env"
        }), 400

    data = request.get_json(silent=True) or {}
    criteria = data.get("criteria", "UNSEEN")
    max_count = int(data.get("max_count", 20))
    mark_as_read = bool(data.get("mark_as_read", True))

    try:
        new_emails = agent.fetch_and_store_emails(
            search_criteria=criteria,
            max_count=max_count,
            mark_as_read=mark_as_read
        )
        return jsonify({
            "message": f"Successfully synced {len(new_emails)} new email(s) from Gmail.",
            "count": len(new_emails),
            "emails": new_emails
        }), 200
    except GmailReaderError as e:
        return jsonify({
            "error": "Gmail Sync Error",
            "message": str(e)
        }), 502

@bp.route('/api/gmail/push/status', methods=['GET'])
def get_push_status():
    """Returns the current status of the real-time IMAP IDLE push listener."""
    listener = get_push_listener()
    return jsonify(listener.get_status()), 200

@bp.route('/api/gmail/push/start', methods=['POST'])
def start_push_listener():
    """Starts the real-time IMAP IDLE push listener in a background thread."""
    listener = get_push_listener()
    if not listener.is_configured():
        return jsonify({
            "error": "Configuration Error",
            "message": "Gmail credentials (GMAIL_EMAIL, GMAIL_APP_PASSWORD) are not configured."
        }), 400

    if listener.is_running:
        return jsonify({
            "message": "Push listener is already running.",
            "status": listener.get_status()
        }), 200

    try:
        listener.start(daemon=True)
        return jsonify({
            "message": "Real-time Gmail push listener started successfully (zero polling, IMAP IDLE mode).",
            "status": listener.get_status()
        }), 200
    except Exception as e:
        return jsonify({
            "error": "Failed to start push listener",
            "message": str(e)
        }), 500

@bp.route('/api/gmail/push/stop', methods=['POST'])
def stop_push_listener():
    """Stops the real-time push listener."""
    listener = get_push_listener()
    if not listener.is_running:
        return jsonify({
            "message": "Push listener is not currently running.",
            "status": listener.get_status()
        }), 200

    listener.stop()
    return jsonify({
        "message": "Push listener stopped successfully.",
        "status": listener.get_status()
    }), 200
