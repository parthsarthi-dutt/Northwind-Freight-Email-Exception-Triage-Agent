"""
Northwind Freight Reviewer Email Dispatcher.
--------------------------------------------
Constructs and transmits formatted review requests to human reviewers (Policy 5.2).
Includes:
  1. Header with Sender Info (Customer or Carrier) and Urgency/Escalation badges.
  2. The incoming message (Sender, Subject, Body, Attachments).
  3. The proposed LLM-generated Draft Reply (guaranteed free of internal limits or account IDs).
  4. Derived Facts & Evidence Citations (EXACTLY matching the Operations Dashboard).
  5. Suggested Internal Operational Actions (Manager approval, DB updates, etc.).
  6. Real email transmission via SMTP (Gmail SSL/TLS) using configured credentials.
"""

import os
import re
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Any, List, Optional

from config import Config

logger = logging.getLogger("agentic_workflow.reviewer_mailer")


class ReviewerMailer:
    def __init__(
        self,
        sender_email: Optional[str] = None,
        app_password: Optional[str] = None,
        reviewer_email: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None
    ):
        self.sender_email = sender_email or Config.GMAIL_EMAIL
        self.app_password = (app_password or Config.GMAIL_APP_PASSWORD or "").replace(" ", "")
        self.reviewer_email = reviewer_email or Config.REVIEWER_EMAIL or self.sender_email
        self.smtp_host = smtp_host or Config.SMTP_HOST
        self.smtp_port = smtp_port or Config.SMTP_PORT

    def format_subject(
        self,
        sender_type: str,
        sender_name: str,
        original_subject: str,
        email_id: int,
        urgency: int = 2,
        needs_manager: bool = False
    ) -> str:
        """Generates clear, distinctive subject line for reviewer with high urgency indicator."""
        type_str = "Customer" if sender_type.lower() == "customer" else "Carrier"
        name_str = sender_name.strip() if sender_name else "Inquiry"
        clean_subj = original_subject.strip() or "No Subject"
        prefix = "🚨 [URGENT P1] " if (urgency >= 4) else ""
        return f"{prefix}[Review Required] Draft Reply for {type_str}: {name_str} | {clean_subj} (Email #{email_id})"

    def _resolve_derived_facts(
        self,
        original_email: Dict[str, Any],
        jev_evaluation: Dict[str, Any],
        draft_reply: str,
        suggested_internal_actions: List[str],
        shipment_context: Optional[Dict[str, Any]],
        account_context: Optional[Dict[str, Any]],
        authorized_ids: List[str],
        sender_type: str,
        derived_facts: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """Resolves the exact same derived facts as displayed on the dashboard."""
        if derived_facts is not None and len(derived_facts) > 0:
            return derived_facts
        try:
            from audit_builder import build_case_audit
            synth_dec = {
                "jev_evaluation": jev_evaluation,
                "authorized_ids": authorized_ids or [],
                "unauthorized_ids": [],
                "requires_reply": True,
                "draft_reply": draft_reply,
                "suggested_internal_actions": suggested_internal_actions,
                "intent": jev_evaluation.get("category", "other"),
                "sender_type": sender_type
            }
            c_audit = build_case_audit(
                original_email,
                synth_dec,
                shipment_context=shipment_context,
                account_context=account_context if sender_type == "customer" else None,
                carrier_context=account_context if sender_type == "carrier" else None
            )
            return c_audit.get("derived_facts", [])
        except Exception as e:
            logger.debug(f"Could not build derived facts in mailer: {e}")
            return []

    def _clean_facts(self, facts: List[Dict[str, Any]], account_context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Sanitizes facts to guarantee no internal account IDs or confidential leaked values."""
        import re
        clean_list = []
        raw_id = ""
        clean_id = ""
        if account_context:
            raw_id = str(account_context.get("account_id") or account_context.get("carrier_id") or "")
            clean_id = raw_id.split('.')[0] if '.' in raw_id else raw_id

        for f in facts:
            cf = dict(f)
            for k in ("title", "value", "source", "context"):
                val = str(cf.get(k, ""))
                if raw_id and raw_id != "None" and raw_id in val:
                    val = val.replace(raw_id, "")
                if clean_id and len(clean_id) >= 3 and clean_id in val:
                    val = re.sub(r'(?i)\b(?:Account\s*(?:ID|#)?\s*[:#]?\s*)?' + re.escape(clean_id) + r'\b', '', val)
                val = re.sub(r'(?i)\b(?:Account\s*(?:ID|#)?\s*[:#]?\s*)[0-9]+(?:\.[0-9]+)?\b', '', val)
                val = re.sub(r'\s{2,}', ' ', val).strip()
                cf[k] = val
            clean_list.append(cf)
        return clean_list

    def build_plain_text(
        self,
        email_id: int,
        sender_type: str,
        sender_name: str,
        sender_email: str,
        original_email: Dict[str, Any],
        draft_reply: str,
        suggested_internal_actions: List[str],
        jev_evaluation: Dict[str, Any],
        shipment_context: Optional[Dict[str, Any]] = None,
        account_context: Optional[Dict[str, Any]] = None,
        authorized_ids: Optional[List[str]] = None,
        derived_facts: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Constructs plain-text fallback representation with clean structure and action links."""
        authorized_ids = authorized_ids or []
        facts = self._resolve_derived_facts(
            original_email=original_email,
            jev_evaluation=jev_evaluation,
            draft_reply=draft_reply,
            suggested_internal_actions=suggested_internal_actions,
            shipment_context=shipment_context,
            account_context=account_context,
            authorized_ids=authorized_ids,
            sender_type=sender_type,
            derived_facts=derived_facts
        )
        facts = self._clean_facts(facts, account_context)
        clean_draft_text = re.sub(r'(?i)\b(?:Account\s*(?:ID|#)?\s*[:#]?\s*)[0-9]+(?:\.[0-9]+)?\b', '', draft_reply).strip()

        api_base = (os.environ.get("API_BASE_URL") or os.environ.get("INGESTION_API_URL") or "http://127.0.0.1:5000").rstrip("/")
        accept_url = f"{api_base}/api/emails/{email_id}/review/action?decision=accept"
        reject_url = f"{api_base}/api/emails/{email_id}/review/action?decision=reject"

        actions_str = "\n".join(f"  * {a}" for a in suggested_internal_actions) if suggested_internal_actions else "  * None"
        attachments = original_email.get("attachments", [])
        att_str = ", ".join(a.get("filename", "file") for a in attachments) if attachments else "None"

        if facts:
            facts_lines = []
            for f in facts:
                f_title = f.get("title", "")
                f_val = f.get("value", "")
                f_src = f.get("source", "")
                f_ctx = f.get("context", "")
                ctx_part = f" — {f_ctx}" if f_ctx else ""
                facts_lines.append(f"  * {f_title}: {f_val} [{f_src}]{ctx_part}")
            facts_block = "\n".join(facts_lines)
        is_high_urgency = (jev_evaluation.get("urgency", 2) >= 4)
        urgent_banner_plain = ""
        if is_high_urgency:
            urgent_banner_plain = (
                "********************************************************************************\n"
                f"🚨 HIGH URGENCY & PRIORITY (LEVEL {jev_evaluation.get('urgency', 2)}/5 — P1 ESCALATION) 🚨\n"
                "FAST HANDLING REQUIRED: Immediate human review and operational dispatch necessary!\n"
                "********************************************************************************\n\n"
            )

        return f"""{urgent_banner_plain}================================================================================
NORTHWIND FREIGHT — OPERATIONS REVIEWER DISPATCH
================================================================================
Case ID:        CASE-EMAIL-{email_id:04d} (Email #{email_id})
Sender:         {sender_name} <{sender_email}> ({sender_type.upper()})
Subject:        {original_email.get('subject')}
Received At:    {original_email.get('received_at')}
Category:       {jev_evaluation.get('category')}
Urgency:        {jev_evaluation.get('urgency', 2)}/5 {'[P1 CRITICAL / HIGH]' if is_high_urgency else '[P2 Routine]'}
Escalation:     {'MANAGER APPROVAL REQUIRED' if jev_evaluation.get('needs_manager_approval') else 'Routine Coordinator Handling'}
Attachments:    {att_str}

--------------------------------------------------------------------------------
REVIEWER ACTIONS (Click to update case status in Northwind DB):
--------------------------------------------------------------------------------
[ACCEPT DRAFT & MARK COMPLETED]:
{accept_url}

[REJECT DRAFT & HANDLE MANUALLY]:
{reject_url}

--------------------------------------------------------------------------------
PROPOSED RESPONSE DRAFT
--------------------------------------------------------------------------------
{clean_draft_text}

--------------------------------------------------------------------------------
DECISION FACTS & POLICY RATIONALE
--------------------------------------------------------------------------------
{facts_block}

--------------------------------------------------------------------------------
REQUIRED INTERNAL ACTIONS
--------------------------------------------------------------------------------
{actions_str}

--------------------------------------------------------------------------------
ORIGINAL INCOMING MESSAGE
--------------------------------------------------------------------------------
{original_email.get('body_text', '')}

================================================================================
"""

    def build_html(
        self,
        email_id: int,
        sender_type: str,
        sender_name: str,
        sender_email: str,
        original_email: Dict[str, Any],
        draft_reply: str,
        suggested_internal_actions: List[str],
        jev_evaluation: Dict[str, Any],
        shipment_context: Optional[Dict[str, Any]] = None,
        account_context: Optional[Dict[str, Any]] = None,
        authorized_ids: Optional[List[str]] = None,
        derived_facts: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Constructs clean, executive HTML email with Accept & Reject action buttons and dynamic decision facts."""
        authorized_ids = authorized_ids or []
        facts = self._resolve_derived_facts(
            original_email=original_email,
            jev_evaluation=jev_evaluation,
            draft_reply=draft_reply,
            suggested_internal_actions=suggested_internal_actions,
            shipment_context=shipment_context,
            account_context=account_context,
            authorized_ids=authorized_ids,
            sender_type=sender_type,
            derived_facts=derived_facts
        )
        facts = self._clean_facts(facts, account_context)

        api_base = (os.environ.get("API_BASE_URL") or os.environ.get("INGESTION_API_URL") or "http://127.0.0.1:5000").rstrip("/")
        accept_url = f"{api_base}/api/emails/{email_id}/review/action?decision=accept"
        reject_url = f"{api_base}/api/emails/{email_id}/review/action?decision=reject"

        urgency = jev_evaluation.get("urgency", 2)
        needs_mgr = jev_evaluation.get("needs_manager_approval", False)
        category = jev_evaluation.get("category", "General")
        attachments = original_email.get("attachments", []) or []
        urgency_bg = "#dc2626" if urgency >= 4 else "#ea580c" if urgency == 3 else "#16a34a"

        is_high_urgency = (urgency >= 4)
        container_style = "border: 2px solid #ef4444; box-shadow: 0 4px 20px rgba(239, 68, 68, 0.25);" if is_high_urgency else "border: 1px solid #e2e8f0; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);"
        header_style = "background: linear-gradient(135deg, #7f1d1d 0%, #991b1b 50%, #450a0a 100%); color: #ffffff; padding: 22px 24px;" if is_high_urgency else "background: #0f172a; color: #ffffff; padding: 22px 24px;"
        header_border = "border-bottom: 2px solid #ef4444;" if is_high_urgency else ""

        # Color-coded badges
        if is_high_urgency:
            urgency_badge = f'<span class="badge" style="background-color:#dc2626; border:1px solid #f87171; box-shadow:0 0 10px rgba(220,38,38,0.5);">URGENCY: {urgency}/5 (P1 CRITICAL)</span>'
        else:
            urgency_badge = f'<span class="badge" style="background-color:{urgency_bg};">URGENCY: {urgency}/5</span>'

        type_bg = "#2563eb" if sender_type.lower() == "customer" else "#7c3aed"
        mgr_badge = '<span class="badge" style="background-color:#b91c1c; border:1px solid #f87171; box-shadow:0 0 8px rgba(185,28,28,0.5);">MANAGER APPROVAL REQUIRED</span>' if needs_mgr else '<span class="badge" style="background-color:#059669;">ROUTINE COORDINATOR</span>'

        urgent_banner_html = ""
        if is_high_urgency:
            urgent_banner_html = f"""
            <div style="background:#fef2f2; border:2px solid #ef4444; border-radius:8px; padding:14px 18px; margin-bottom:18px;">
              <table style="width:100%; border-collapse:collapse;">
                <tr>
                  <td style="width:36px; vertical-align:middle; font-size:24px;">🚨</td>
                  <td style="vertical-align:middle;">
                    <div style="font-size:14px; font-weight:800; text-transform:uppercase; color:#b91c1c; letter-spacing:0.5px;">
                      HIGH URGENCY &amp; PRIORITY P1 — FAST HANDLING REQUIRED
                    </div>
                    <div style="font-size:12px; font-weight:600; color:#7f1d1d; margin-top:2px;">
                      Urgency Score: Level {urgency}/5 &bull; Immediate human review needed for fast operational resolution.
                    </div>
                  </td>
                </tr>
              </table>
            </div>
            """

        # Clean actions list
        actions_html = "".join(f"<li style='margin-bottom:8px;color:#1e293b;'>{act}</li>" for act in suggested_internal_actions) if suggested_internal_actions else "<li>No operational internal action required.</li>"

        # Attachments string
        if attachments:
            att_items = "".join(f"<span style='background:#f1f5f9;border:1px solid #cbd5e1;padding:3px 8px;border-radius:4px;font-size:12px;margin-right:6px;'>📎 {a.get('filename', 'attachment')}</span>" for a in attachments)
        else:
            att_items = "<span style='color:#64748b;font-size:12px;'>None</span>"

        # Safe paragraph conversion with strict internal ID scrubbing
        clean_draft_html = re.sub(r'(?i)\b(?:Account\s*(?:ID|#)?\s*[:#]?\s*)[0-9]+(?:\.[0-9]+)?\b', '', draft_reply).strip()
        safe_draft = clean_draft_html.replace("\n", "<br/>")
        safe_body = original_email.get("body_text", "").replace("\n", "<br/>")

        # Verified facts table rows
        facts_rows = []
        for f in facts:
            f_title = f.get("title", "")
            f_val = f.get("value", "")
            f_src = f.get("source", "")
            f_ctx = f.get("context", "")
            ctx_div = f"<div style='color:#64748b;font-size:12px;margin-top:3px;'>{f_ctx}</div>" if f_ctx else ""
            facts_rows.append(f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#1e293b;width:32%;vertical-align:top;">{f_title}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;color:#0f172a;font-weight:600;width:30%;vertical-align:top;">{f_val}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;width:38%;vertical-align:top;"><span style="color:#0284c7;font-size:12px;font-weight:600;">{f_src}</span>{ctx_div}</td>
            </tr>
            """)
        facts_rows_html = "".join(facts_rows) if facts_rows else "<tr><td colspan='3' style='padding:14px;text-align:center;color:#64748b;'>No decision facts recorded.</td></tr>"

        action_buttons_html = f"""
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:18px 20px;text-align:center;margin:16px 0;">
          <div style="font-size:14px;font-weight:600;color:#1e293b;margin-bottom:14px;">Reviewer Sign-Off: Select an action to update Northwind DB</div>
          <table align="center" style="margin:0 auto;border-collapse:collapse;">
            <tr>
              <td style="padding:0 8px;">
                <a href="{accept_url}" target="_blank" style="display:inline-block;background-color:#16a34a;color:#ffffff;font-size:14px;font-weight:700;padding:12px 24px;border-radius:6px;text-decoration:none;">
                  ✓ Accept Draft & Mark Completed
                </a>
              </td>
              <td style="padding:0 8px;">
                <a href="{reject_url}" target="_blank" style="display:inline-block;background-color:#dc2626;color:#ffffff;font-size:14px;font-weight:700;padding:12px 24px;border-radius:6px;text-decoration:none;">
                  ✕ Reject & Handle Manually
                </a>
              </td>
            </tr>
          </table>
          <div style="font-size:12px;color:#64748b;margin-top:10px;">
            Accepting marks status as <strong>completed</strong>. Rejecting assigns case to <strong>reviewer</strong> for manual resolution.
          </div>
        </div>
        """

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; color: #0f172a; margin: 0; padding: 20px; background-color: #f8fafc; }}
  .container {{ max-width: 740px; margin: 0 auto; background: #ffffff; border-radius: 10px; {container_style} overflow: hidden; }}
  .header {{ {header_style} }}
  .header h1 {{ margin: 0 0 8px 0; font-size: 20px; font-weight: 700; }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }}
  .badge {{ display: inline-block; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: 700; color: #fff; }}
  .content {{ padding: 24px; }}
  .card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 20px; overflow: hidden; }}
  .card-header {{ background: #f8fafc; border-bottom: 1px solid #e2e8f0; padding: 12px 16px; font-size: 13px; font-weight: 700; color: #334155; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card-body {{ padding: 16px; }}
  .draft-box {{ background: #f0fdf4; border: 1px solid #bbf7d0; border-left: 4px solid #16a34a; padding: 16px; border-radius: 6px; font-size: 14px; color: #14532d; line-height: 1.6; }}
  .quote-box {{ background: #f1f5f9; border-left: 4px solid #64748b; padding: 14px; border-radius: 0 6px 6px 0; font-size: 14px; color: #334155; line-height: 1.5; }}
  .footer {{ background: #f8fafc; border-top: 1px solid #e2e8f0; padding: 16px; font-size: 12px; color: #64748b; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <!-- HEADER -->
  <div class="header" style="{header_border}">
    <h1>Northwind Freight — Human Reviewer Sign-Off</h1>
    <div style="font-size: 13px; color: #cbd5e1;">
      Case ID: CASE-EMAIL-{email_id:04d} &bull; Email #{email_id}
    </div>
    <div class="badges">
      <span class="badge" style="background-color: {type_bg};">{sender_type.upper()} INQUIRY</span>
      {urgency_badge}
      <span class="badge" style="background-color: #475569;">{category.upper()}</span>
      {mgr_badge}
    </div>
  </div>

  <div class="content">
    <!-- URGENT ALERT BANNER (IF HIGH URGENCY / P1) -->
    {urgent_banner_html}

    <!-- ACTION BUTTONS BAR (TOP) -->
    {action_buttons_html}

    <!-- PROPOSED DRAFT REPLY -->
    <div class="card" style="border: 2px solid #86efac;">
      <div class="card-header" style="background: #f0fdf4; color: #166534;">
        <span>Proposed Response Draft</span>
      </div>
      <div class="card-body">
        <div class="draft-box">
          {safe_draft}
        </div>
      </div>
    </div>

    <!-- DYNAMIC DECISION FACTS & POLICIES -->
    <div class="card">
      <div class="card-header" style="display: flex; justify-content: space-between; align-items: center;">
        <span>Decision-Supporting Facts & Policies</span>
        <span style="font-size: 11px; color: #0284c7; background: #e0f2fe; padding: 2px 8px; border-radius: 4px; font-weight: 600;">{len(facts)} Facts</span>
      </div>
      <div class="card-body" style="padding: 0;">
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
          <thead>
            <tr style="background: #f8fafc; border-bottom: 2px solid #e2e8f0; text-align: left;">
              <th style="padding: 10px 12px; font-size: 11px; color: #64748b; text-transform: uppercase;">Decision Factor</th>
              <th style="padding: 10px 12px; font-size: 11px; color: #64748b; text-transform: uppercase;">Verified Finding</th>
              <th style="padding: 10px 12px; font-size: 11px; color: #64748b; text-transform: uppercase;">Policy & Grounding</th>
            </tr>
          </thead>
          <tbody>
            {facts_rows_html}
          </tbody>
        </table>
      </div>
    </div>

    <!-- SUGGESTED INTERNAL ACTIONS -->
    <div class="card">
      <div class="card-header">
        <span>Required Operational Actions</span>
      </div>
      <div class="card-body">
        <ul style="margin: 0; padding-left: 20px; font-size: 13px;">
          {actions_html}
        </ul>
      </div>
    </div>

    <!-- ORIGINAL INCOMING EMAIL -->
    <div class="card">
      <div class="card-header">
        <span>Original Incoming Message</span>
      </div>
      <div class="card-body">
        <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 12px;">
          <tr>
            <td style="color: #64748b; width: 20%; padding: 4px 0;">From:</td>
            <td style="color: #0f172a; padding: 4px 0;"><strong>{sender_name}</strong> &lt;{sender_email}&gt;</td>
          </tr>
          <tr>
            <td style="color: #64748b; padding: 4px 0;">Subject:</td>
            <td style="color: #0f172a; padding: 4px 0;"><strong>{original_email.get('subject')}</strong></td>
          </tr>
          <tr>
            <td style="color: #64748b; padding: 4px 0;">Received:</td>
            <td style="color: #0f172a; padding: 4px 0;">{original_email.get('received_at')}</td>
          </tr>
          <tr>
            <td style="color: #64748b; padding: 4px 0;">Attachments:</td>
            <td style="color: #0f172a; padding: 4px 0;">{att_items}</td>
          </tr>
        </table>
        <div class="quote-box">
          {safe_body}
        </div>
      </div>
    </div>

    <!-- ACTION BUTTONS BAR (BOTTOM) -->
    {action_buttons_html}
  </div>

  <!-- FOOTER -->
  <div class="footer">
    Northwind Freight Operations &bull; Automated Decision Audit &bull; Case #{email_id}
  </div>
</div>
</body>
</html>
"""

    def send_review_request(
        self,
        email_id: int,
        sender_type: str,
        sender_name: str,
        sender_email: str,
        original_email: Dict[str, Any],
        draft_reply: str,
        suggested_internal_actions: List[str],
        jev_evaluation: Dict[str, Any],
        shipment_context: Optional[Dict[str, Any]] = None,
        account_context: Optional[Dict[str, Any]] = None,
        authorized_ids: Optional[List[str]] = None,
        derived_facts: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Constructs and dispatches the review email to the reviewer.
        Returns dispatch status and metadata.
        """
        subject = self.format_subject(
            sender_type=sender_type,
            sender_name=sender_name,
            original_subject=original_email.get("subject", ""),
            email_id=email_id,
            urgency=jev_evaluation.get("urgency", 2),
            needs_manager=jev_evaluation.get("needs_manager_approval", False)
        )

        plain_text = self.build_plain_text(
            email_id=email_id,
            sender_type=sender_type,
            sender_name=sender_name,
            sender_email=sender_email,
            original_email=original_email,
            draft_reply=draft_reply,
            suggested_internal_actions=suggested_internal_actions,
            jev_evaluation=jev_evaluation,
            shipment_context=shipment_context,
            account_context=account_context,
            authorized_ids=authorized_ids,
            derived_facts=derived_facts
        )

        html_text = self.build_html(
            email_id=email_id,
            sender_type=sender_type,
            sender_name=sender_name,
            sender_email=sender_email,
            original_email=original_email,
            draft_reply=draft_reply,
            suggested_internal_actions=suggested_internal_actions,
            jev_evaluation=jev_evaluation,
            shipment_context=shipment_context,
            account_context=account_context,
            authorized_ids=authorized_ids,
            derived_facts=derived_facts
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Northwind Triage Agent <{self.sender_email}>"
        msg["To"] = self.reviewer_email

        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_text, "html", "utf-8"))

        if dry_run or os.environ.get("MOCK_SMTP") == "1":
            logger.info(f"🧪 [DRY RUN / MOCK SMTP] Review request email prepared for {self.reviewer_email}: '{subject}'")
            return {
                "sent": True,
                "recipient": self.reviewer_email,
                "subject": subject,
                "mode": "mock_smtp"
            }

        # Real SMTP Transmission via Gmail
        try:
            logger.info(f"📧 [SMTP] Connecting to {self.smtp_host}:{self.smtp_port} to send reviewer email...")
            if self.smtp_port == 465:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=12)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=12)
                server.starttls()

            if self.app_password:
                server.login(self.sender_email, self.app_password)

            server.sendmail(self.sender_email, [self.reviewer_email], msg.as_string())
            server.quit()

            logger.info(f"✅ [SMTP SENT] Reviewer notification delivered to {self.reviewer_email} for Email #{email_id}")
            return {
                "sent": True,
                "recipient": self.reviewer_email,
                "subject": subject,
                "mode": "smtp_live"
            }
        except Exception as err:
            logger.error(f"❌ [SMTP ERROR] Failed to send email to reviewer: {err}")
            return {
                "sent": False,
                "error": str(err),
                "recipient": self.reviewer_email,
                "subject": subject,
                "mode": "smtp_error"
            }
