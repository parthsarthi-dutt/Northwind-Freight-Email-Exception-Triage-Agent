import os
import re
import logging
from typing import Dict, Any, Optional, List
import requests
from config import Config
from northwind_db import (
    verify_sender_email,
    get_shipment_context,
    get_account_by_email,
    get_carrier_by_email,
    get_shipments_by_account,
    validate_customer_shipment_authorization,
    validate_carrier_shipment_authorization,
    update_shipment_notes
)
from jev_classifier import JevClassifier
from response_drafter import ResponseDrafter
from reviewer_mailer import ReviewerMailer
from audit_builder import build_case_audit

logger = logging.getLogger("agentic_workflow.agent")

class EmailAgent:
    """
    Agentic Email Processing Workflow.
    
    Workflow Steps:
      Step 1: Verification & Sender Routing:
        - Normalizes email to lowercase.
        - Verifies whether sender is a Customer, Carrier, or Unverified/Non-existent.
      Step 2: Authorization Gate:
        - Ensures customers only access their shipments.
        - Ensures carriers only access their assigned shipments.
      Step 3: Jev-1.14 / Jev-1.13 Classification:
        - Evaluates Subject + Body for category, urgency (1-5), and threats.
        - Threat grounding against real order tracking from northwind.db.
        - Prompt injection & instruction override defense.
        - Northwind Policy Section 6.1 Manager Escalation enforcement.
      Step 4: Groq LLM Response Drafting & Reviewer Gate:
        - If requires_reply is False: LLM not called, draft is None, reviewer_status = 'no_reply_needed'.
        - If requires_reply is True: Generates polite customer draft for reviewer sign-off,
          with strictly segregated suggested_internal_actions.
      Step 5: Reviewer Mail Dispatch & Database Status Update:
        - Sends formatted review email with original mail, draft, proofs, and internal actions.
        - Updates status in database to 'under_review'.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        jev_model: Optional[str] = None,
        groq_model: Optional[str] = None,
        mailer: Optional[ReviewerMailer] = None
    ):
        self.api_url = api_url or Config.INGESTION_API_URL
        target_groq = groq_model or Config.GROQ_DEFAULT_MODEL
        self.jev = JevClassifier(
            model=jev_model or os.environ.get("JEV_MODEL", "jev-1.14.0"),
            groq_model=target_groq
        )
        self.drafter = ResponseDrafter(groq_model=target_groq)
        self.mailer = mailer or ReviewerMailer()

    def update_status(self, email_id: int, new_status: str, audit_data: Optional[Dict[str, Any]] = None) -> bool:
        """Calls the ingestion microservice to update email status and audit data."""
        url = f"{self.api_url}/api/emails/{email_id}/status"
        payload: Dict[str, Any] = {"status": new_status}
        if audit_data is not None:
            payload["audit_data"] = audit_data
        try:
            resp = requests.patch(url, json=payload, timeout=5)
            if resp.status_code == 200:
                logger.info(f"Updated status for email #{email_id} -> '{new_status}' (Audit data attached: {audit_data is not None})")
                return True
            else:
                logger.warning(f"Failed to update status for #{email_id}: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.warning(f"Could not reach ingestion service at {url}: {e}")
            return False

    def extract_tracking_numbers(self, text: str) -> List[str]:
        """Extracts tracking identifiers (e.g. NWF-20101, TRACK-12345)."""
        pattern = r'\b(?:NWF|TRK|PKG|ORD)-[0-9A-Z]{4,10}\b'
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        return list(set(m.upper() for m in matches))

    def classify_intent(self, subject: str, body: str) -> str:
        """Classifies the primary intent of the incoming message."""
        combined = f"{subject} {body}".lower()
        if any(w in combined for w in ["late", "delayed", "where is", "tracking", "status"]):
            return "tracking_inquiry"
        elif any(w in combined for w in ["damaged", "broken", "missing", "refund", "claim"]):
            return "damage_or_claim"
        elif any(w in combined for w in ["quote", "price", "booking", "freight cost"]):
            return "quote_request"
        elif any(w in combined for w in ["refused", "attempt failed", "held at customs", "weather delay"]):
            return "carrier_operational_notice"
        return "general_inquiry"

    def process(self, email_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main Agent Execution Entrypoint.
        Processes popped email message (which contains everything EXCEPT status).
        """
        assert "status" not in email_data, "Agent worker must receive email content WITHOUT status!"

        email_id = email_data["id"]
        sender = email_data.get("from", {})
        raw_email = sender.get("email", "") if isinstance(sender, dict) else str(sender)
        sender_email = (raw_email or "").strip().lower()  # Normalize to lowercase
        sender_name = sender.get("name", "") if isinstance(sender, dict) else ""
        subject = email_data.get("subject", "")
        body = email_data.get("body_text", "")

        logger.info("=" * 70)
        logger.info(f"🤖 [AGENT WORKFLOW START] Email #{email_id}")
        logger.info(f"   From:    {sender_name} <{sender_email}>")
        logger.info(f"   Subject: {subject}")
        logger.info(f"   To:      {email_data.get('to')}")
        logger.info("=" * 70)

        # Update ingestion status to processing
        self.update_status(email_id, "processing")

        try:
            # =====================================================================
            # STEP 1: SENDER VERIFICATION & CLASSIFICATION (LOWERCASED COMPARISON)
            # =====================================================================
            verification = verify_sender_email(sender_email)
            logger.info(f"🔍 [STEP 1: VERIFICATION] Status: {verification['sender_type'].upper()} | {verification['message']}")

            # =====================================================================
            # STEP 2: WORKFLOW ROUTING BASED ON VERIFICATION RESULT
            # =====================================================================
            if not verification["verified"]:
                return self.handle_unverified_workflow(email_id, sender_name, sender_email, subject, body, verification, email_data=email_data)

            elif verification["sender_type"] == "customer":
                return self.handle_customer_workflow(email_id, sender_name, sender_email, subject, body, verification, email_data=email_data)

            elif verification["sender_type"] == "carrier":
                return self.handle_carrier_workflow(email_id, sender_name, sender_email, subject, body, verification, email_data=email_data)

            else:
                return self.handle_unverified_workflow(email_id, sender_name, sender_email, subject, body, verification, email_data=email_data)

        except Exception as e:
            logger.error(f"❌ [WORKFLOW HALTED - GROQ ERROR] Email #{email_id}: {e}")
            self.update_status(email_id, "groq_api_error")
            return {
                "email_id": email_id,
                "status": "error",
                "error": str(e),
                "workflow_routed_to": "error_handler",
                "action_taken": f"Execution halted: {e}",
                "requires_reply": False,
                "reviewer_status": "groq_error",
                "draft_reply": None,
                "suggested_internal_actions": [f"Technical Alert: Groq LLM failed: {e}. Check GROQ_API_KEY."],
                "internal_action": f"Technical Alert: Groq LLM failed: {e}."
            }

    # -------------------------------------------------------------------------
    # WORKFLOW BRANCH A: UNVERIFIED / NON-EXISTENT EMAIL
    # -------------------------------------------------------------------------
    def handle_unverified_workflow(
        self,
        email_id: int,
        sender_name: str,
        sender_email: str,
        subject: str,
        body: str,
        verification: Dict[str, Any],
        email_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executed when the email does NOT match any customer account or carrier in database.
        """
        logger.warning(f"⚠️ [UNVERIFIED WORKFLOW] Email '{sender_email}' does not exist in Northwind database.")
        final_status = "unverified"

        internal_act = "Security Audit: Unverified sender contact attempt logged."
        decision = {
            "email_id": email_id,
            "sender_email": sender_email,
            "status": final_status,
            "final_status": final_status,
            "verification_status": "unverified",
            "authorization_status": "unverified",
            "sender_type": "unknown",
            "message": verification["message"],
            "workflow_routed_to": "unverified_handler",
            "action_taken": "Halted at Step 2: Unverified sender; email not found in database. No reply permitted.",
            "requires_reply": False,
            "reviewer_status": "no_reply_needed",
            "draft_reply": None,
            "suggested_internal_actions": [internal_act],
            "internal_action": internal_act
        }
        e_data = email_data or {
            "id": email_id,
            "subject": subject,
            "body_text": body,
            "from": {"name": sender_name, "email": sender_email},
            "attachments": []
        }
        decision["audit_data"] = build_case_audit(e_data, decision, reviewer_email=self.mailer.reviewer_email)
        self.update_status(email_id, final_status, audit_data=decision["audit_data"])
        logger.info(f"🏁 [WORKFLOW END] Email #{email_id} -> {final_status}")
        return decision

    # -------------------------------------------------------------------------
    # WORKFLOW BRANCH B: VERIFIED CUSTOMER WORKFLOW
    # -------------------------------------------------------------------------
    def handle_customer_workflow(
        self,
        email_id: int,
        sender_name: str,
        sender_email: str,
        subject: str,
        body: str,
        verification: Dict[str, Any],
        email_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executed when the sender matches a registered Customer account.
        Enforces authorization: Customer can only access shipments belonging to their account.
        """
        account = verification["account"]
        account_id = account.get("account_id")
        tier = account.get("tier", "Standard")
        logger.info(f"👤 [CUSTOMER WORKFLOW] Customer '{account.get('contact')}' | Account #{account_id} | Tier: {tier}")

        # Determine tracking IDs & resolution ladder method
        tracking_ids = self.extract_tracking_numbers(f"{subject} {body}")
        resolution_method = "exact_id"
        if not tracking_ids:
            # Resolution Ladder: Account History Fallback
            # In case no info about shipment ID is given, look up customer's account order history
            # and take into account the most recently ordered shipment ID.
            account_shipments = get_shipments_by_account(account_id, customer_name=account.get("contact"))
            if account_shipments:
                most_recent_shipment = account_shipments[0]
                most_recent_id = most_recent_shipment.get("shipment_id")
                if most_recent_id:
                    tracking_ids = [most_recent_id]
                    resolution_method = "account_history"
                    logger.info(f"📦 [ACCOUNT HISTORY] No shipment ID provided by sender. Resolved most recent order: {most_recent_id} for account #{account_id}")

        intent = self.classify_intent(subject, body)
        logger.info(f"   Intent:       {intent}")
        logger.info(f"   Tracking IDs: {tracking_ids} (Method: {resolution_method})")

        # Check shipment authorization
        auth_check = validate_customer_shipment_authorization(account, tracking_ids)

        # CASE 1: Customer enquired about shipment(s) that DO NOT belong to this account
        if tracking_ids and len(auth_check["authorized_ids"]) == 0:
            logger.warning(f"🚫 [UNAUTHORIZED] Customer '{account.get('contact')}' attempted to access unauthorized shipment(s): {auth_check['unauthorized_ids']}")
            unauth_act = f"Security Log: Unauthorized access attempt for shipment(s) {', '.join(auth_check['unauthorized_ids'])} by sender {account.get('contact')}."
            final_status = "under_review"
            e_data = email_data or {
                "id": email_id,
                "subject": subject,
                "body_text": body,
                "from": {"name": sender_name, "email": sender_email},
                "attachments": []
            }
            jev_eval = self.jev.evaluate(
                subject=subject,
                body=body,
                sender_type="customer",
                account_context=account,
                tracking_context={"unauthorized_ids": auth_check["unauthorized_ids"]}
            )
            jev_eval["category"] = "unauthorized_parcel_access"
            jev_eval["urgency"] = 1
            jev_eval["needs_manager_approval"] = False
            if "policy_compliance" in jev_eval and isinstance(jev_eval["policy_compliance"], dict):
                jev_eval["policy_compliance"]["manager_escalation_required"] = False

            drafting_res = self.drafter.draft(
                email_data=e_data,
                jev_eval=jev_eval,
                shipment_context=None,
                account_context=account,
                authorized_ids=[],
                unauthorized_ids=auth_check["unauthorized_ids"]
            )
            draft_reply_str = str(drafting_res.get("draft_reply") or "")
            actions_list: List[str] = [str(a) for a in drafting_res.get("suggested_internal_actions", [unauth_act])]
            decision_data = {
                "email_id": email_id,
                "sender_email": sender_email,
                "status": final_status,
                "final_status": final_status,
                "verification_status": "verified",
                "authorization_status": "unauthorized",
                "sender_type": "customer",
                "account": account,
                "workflow_routed_to": "customer_workflow",
                "tracking_ids": tracking_ids,
                "authorized_ids": [],
                "unauthorized_ids": auth_check["unauthorized_ids"],
                "urgency": 1,
                "needs_manager_approval": False,
                "action_taken": f"Consignee Privacy Notice: Shipment ID(s) {', '.join(auth_check['unauthorized_ids'])} do not belong to customer ({account.get('contact')}). Privacy notice drafted for reviewer sign-off.",
                "requires_reply": True,
                "reviewer_status": "under_review",
                "draft_reply": draft_reply_str,
                "suggested_internal_actions": actions_list,
                "internal_action": str(drafting_res.get("internal_action") or unauth_act),
                "jev_evaluation": jev_eval
            }
            case_audit = build_case_audit(e_data, decision_data, shipment_context=None, account_context=account, reviewer_email=self.mailer.reviewer_email)
            decision_data["audit_data"] = case_audit

            review_dispatch = self.mailer.send_review_request(
                email_id=email_id,
                sender_type="customer",
                sender_name=sender_name or (account.get("contact") if account else "Customer") or "Customer",
                sender_email=sender_email,
                original_email=e_data,
                draft_reply=draft_reply_str,
                suggested_internal_actions=actions_list,
                jev_evaluation=jev_eval,
                shipment_context=None,
                account_context=account,
                authorized_ids=[],
                derived_facts=case_audit.get("derived_facts", [])
            )
            decision_data["reviewer_dispatch"] = review_dispatch
            self.update_status(email_id, final_status, audit_data=case_audit)
            return decision_data

        # CASE 2: Take the workflow ahead with ONLY the shipment IDs that belong to him
        authorized_ids = auth_check["authorized_ids"]
        unauthorized_ids = auth_check["unauthorized_ids"]

        if unauthorized_ids:
            logger.info(f"⚠️ [FILTERED] Proceeding only with authorized shipment(s): {authorized_ids}. (Excluded unauthorized: {unauthorized_ids})")

        shipment_context = None
        affected_shipments = []
        if authorized_ids:
            for sid in authorized_ids:
                sc = get_shipment_context(sid)
                if sc and "shipment" in sc:
                    affected_shipments.append(sc["shipment"])
            primary_context = get_shipment_context(authorized_ids[0])
            shipment_context = {
                "shipment": primary_context.get("shipment") if primary_context else {},
                "shipments": affected_shipments,
                "received_at": (email_data or {}).get("received_at")
            }

        # =====================================================================
        # STEP 3: JEV-1.14 / JEV-1.13 CLASSIFICATION & THREAT GROUNDING
        # =====================================================================
        jev_eval = self.jev.evaluate(
            subject=subject,
            body=body,
            sender_type="customer",
            account_context=account,
            tracking_context=shipment_context
        )

        decision = {
            "email_id": email_id,
            "sender_email": sender_email,
            "verification_status": "verified",
            "authorization_status": "authorized",
            "sender_type": "customer",
            "account": account,
            "workflow_routed_to": "customer_workflow",
            "intent": jev_eval["category"],
            "needs_manager_approval": jev_eval.get("needs_manager_approval", False),
            "manager_escalation_classification": jev_eval.get("manager_escalation_classification", "routine_coordinator_handling"),
            "urgency": jev_eval["urgency"],
            "jev_evaluation": jev_eval,
            "tracking_ids": tracking_ids,
            "authorized_ids": authorized_ids,
            "unauthorized_ids": unauthorized_ids,
            "resolution_method": resolution_method,
            "context": shipment_context,
            "action_taken": None,
            "requires_reply": True,
            "reviewer_status": "pending_reviewer_approval",
            "draft_reply": None,
            "suggested_internal_actions": [],
            "internal_action": None
        }

        e_data = email_data or {
            "id": email_id,
            "subject": subject,
            "body_text": body,
            "from": {"name": sender_name, "email": sender_email},
            "attachments": []
        }

        # Security Check: Prompt Injection / Instruction Override Suppression
        if jev_eval["injection_defense"]["has_injection_flag"]:
            final_status = "escalated"
            decision["action_taken"] = (
                f"SECURITY ESCALATION (P1): Prompt injection / instruction override attack detected. "
                f"Customer draft suppressed. Manager escalation triggered."
            )
            decision["requires_reply"] = False
            decision["reviewer_status"] = "no_reply_needed"
            decision["draft_reply"] = None
            sec_action = "SECURITY ESCALATION (P1): Prompt injection attack detected. Route to security officer."
            decision["suggested_internal_actions"] = [sec_action]
            decision["internal_action"] = sec_action
            case_audit = build_case_audit(e_data, decision, shipment_context=shipment_context, account_context=account, reviewer_email=self.mailer.reviewer_email)
            decision["audit_data"] = case_audit
            self.update_status(email_id, final_status, audit_data=case_audit)
            logger.warning(f"🚨 [SECURITY ESCALATION] Email #{email_id}: Prompt injection attack detected. Draft suppressed.")
            return decision

        # Premature Complaint Check:
        premature = jev_eval.get("premature_evaluation", {})
        if premature.get("is_premature_late_complaint"):
            final_status = "logged_premature_inquiry"
            decision["action_taken"] = premature.get("reason")
            decision["requires_reply"] = False
            decision["reviewer_status"] = "no_reply_needed"
            decision["draft_reply"] = None
            prem_act = f"Operational Hold: Late delivery complaint for {authorized_ids[0] if authorized_ids else 'parcel'} prior to promised date. Inquiry logged; reply suppressed."
            decision["suggested_internal_actions"] = [prem_act]
            decision["internal_action"] = prem_act
            case_audit = build_case_audit(e_data, decision, shipment_context=shipment_context, account_context=account, reviewer_email=self.mailer.reviewer_email)
            decision["audit_data"] = case_audit
            self.update_status(email_id, final_status, audit_data=case_audit)
            logger.info(f"⏳ [PREMATURE LATE COMPLAINT] Email #{email_id}: Promised date has not arrived yet. Reply suppressed.")
            return decision

        if premature.get("is_premature_damage_claim"):
            final_status = "logged_premature_claim"
            decision["action_taken"] = premature.get("reason")
            decision["requires_reply"] = False
            decision["reviewer_status"] = "no_reply_needed"
            decision["draft_reply"] = None
            prem_dmg_act = f"Operational Hold: Damage claim submitted for {authorized_ids[0] if authorized_ids else 'parcel'} prior to delivery. Claim logged; reply suppressed."
            decision["suggested_internal_actions"] = [prem_dmg_act]
            decision["internal_action"] = prem_dmg_act
            case_audit = build_case_audit(e_data, decision, shipment_context=shipment_context, account_context=account, reviewer_email=self.mailer.reviewer_email)
            decision["audit_data"] = case_audit
            self.update_status(email_id, final_status, audit_data=case_audit)
            logger.info(f"⏳ [PREMATURE DAMAGE CLAIM] Email #{email_id}: Parcel is not delivered yet. Reply suppressed.")
            return decision

        # Non-exception informational filter
        if not jev_eval.get("requires_reply", True):
            final_status = "completed"
            decision["action_taken"] = f"Processed informational message ({jev_eval.get('category')}). No reply needed."
            decision["requires_reply"] = False
            decision["reviewer_status"] = "no_reply_needed"
            decision["draft_reply"] = None
            decision["suggested_internal_actions"] = ["Archive message: Non-exception / informational message."]
            decision["internal_action"] = "Archive message: Non-exception."
            case_audit = build_case_audit(e_data, decision, shipment_context=shipment_context, account_context=account, reviewer_email=self.mailer.reviewer_email)
            decision["audit_data"] = case_audit
            self.update_status(email_id, final_status, audit_data=case_audit)
            return decision

        # =====================================================================
        # STEP 4: GROQ LLM RESPONSE DRAFTING & REVIEWER PIPELINE
        # =====================================================================
        drafting_res = self.drafter.draft(
            email_data=e_data,
            jev_eval=jev_eval,
            shipment_context=shipment_context,
            account_context=account,
            authorized_ids=authorized_ids,
            unauthorized_ids=unauthorized_ids,
            resolution_method=resolution_method
        )

        draft_reply_main = str(drafting_res.get("draft_reply") or "")
        raw_main_actions = drafting_res.get("suggested_internal_actions") or []
        actions_main_list: List[str] = [str(a) for a in raw_main_actions] if isinstance(raw_main_actions, list) else [str(raw_main_actions)]

        decision["requires_reply"] = drafting_res.get("requires_reply", True)
        decision["reviewer_status"] = drafting_res.get("reviewer_status", "pending_reviewer_approval")
        decision["draft_reply"] = draft_reply_main
        decision["suggested_internal_actions"] = actions_main_list
        decision["internal_action"] = str(drafting_res.get("internal_action") or "; ".join(actions_main_list))

        # Determine action_taken summary
        if resolution_method == "account_history" and authorized_ids:
            sh_obj = (shipment_context or {}).get("shipment")
            status_desc = sh_obj.get("status") if isinstance(sh_obj, dict) else "In transit"
            decision["action_taken"] = f"Resolved most recent order {authorized_ids[0]} from customer account history. Retrieved live status '{status_desc}'."
        elif jev_eval["category"] in ("late_or_silent_shipment", "status_query", "tracking_inquiry") and shipment_context:
            sh_obj = shipment_context.get("shipment")
            status_desc = sh_obj.get("status") if isinstance(sh_obj, dict) else "In transit"
            decision["action_taken"] = f"Retrieved live status '{status_desc}' from Northwind DB for parcel {authorized_ids[0]}"
        elif jev_eval["category"] == "damage":
            decision["action_taken"] = f"Escalated to Carrier Claims Team per Northwind Policy 2.2 (Prior claims: {account.get('damage_or_compensation_claims_last_30_days', 0)})"
        elif jev_eval["category"] == "delivery_dispute":
            decision["action_taken"] = "Initiated POD dispute investigation with carrier per Northwind Policy 2.4 (signed_by concealed pending POD check)"
        elif jev_eval["category"] == "compensation_or_refund":
            refund_eval = jev_eval["policy_compliance"].get("refund_evaluation", {})
            if refund_eval.get("exceeds_coordinator_limit") or refund_eval.get("exceeds_total_paid"):
                reason_msg = "exceeds total transaction value (order value + shipping fee)" if refund_eval.get("exceeds_total_paid") else "exceeds coordinator limit"
                decision["action_taken"] = f"Refund request of ${refund_eval.get('amount_requested')} {reason_msg} (Policy 3.3 / 6.1). Passed ahead to operations manager."
            else:
                decision["action_taken"] = "Evaluating compensation request within coordinator limit (Policy 3.1 & 3.2)"
        elif jev_eval["category"] == "legal_or_press_threat" or jev_eval.get("threat", {}).get("has_threat"):
            decision["action_taken"] = "Escalated to Operations Manager & Legal Counsel per Northwind Policy 6.1"
        elif jev_eval["policy_compliance"].get("manager_escalation_required"):
            reasons = "; ".join(jev_eval["policy_compliance"]["escalation_reasons"])
            decision["action_taken"] = f"Escalated to Operations Manager per Section 6.1: {reasons}"
        else:
            decision["action_taken"] = f"Processed customer request ({jev_eval['category']})"

        case_audit = build_case_audit(e_data, decision, shipment_context=shipment_context, account_context=account, reviewer_email=self.mailer.reviewer_email)
        decision["audit_data"] = case_audit

        # If reply is required, dispatch email to reviewer and update DB status to 'under_review'
        if decision.get("requires_reply"):
            review_dispatch = self.mailer.send_review_request(
                email_id=email_id,
                sender_type="customer",
                sender_name=sender_name or (account.get("contact") if account else "Customer") or "Customer",
                sender_email=sender_email,
                original_email=e_data,
                draft_reply=draft_reply_main,
                suggested_internal_actions=actions_main_list,
                jev_evaluation=jev_eval,
                shipment_context=shipment_context,
                account_context=account,
                authorized_ids=authorized_ids,
                derived_facts=case_audit.get("derived_facts", [])
            )
            decision["reviewer_dispatch"] = review_dispatch
            decision["reviewer_status"] = "under_review"
            final_status = "under_review"
        else:
            final_status = "completed"

        self.update_status(email_id, final_status, audit_data=case_audit)
        logger.info(f"🏁 [WORKFLOW END] Email #{email_id} -> {final_status} (Urgency: {jev_eval['urgency']}/5)")
        return decision


    # -------------------------------------------------------------------------
    # WORKFLOW BRANCH C: VERIFIED CARRIER WORKFLOW
    # -------------------------------------------------------------------------
    def handle_carrier_workflow(
        self,
        email_id: int,
        sender_name: str,
        sender_email: str,
        subject: str,
        body: str,
        verification: Dict[str, Any],
        email_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executed when the sender matches a registered Carrier.
        Enforces authorization: Carrier can only inform/know about shipments assigned to it.
        Updates shipment notes in database for authorized operational notices (delays, accidents).
        """
        carrier = verification["carrier"]
        carrier_name = carrier.get("carrier")
        logger.info(f"🚚 [CARRIER WORKFLOW] Processing operational notice from Carrier '{carrier_name}' ({carrier.get('ops_contact')})")

        tracking_ids = self.extract_tracking_numbers(f"{subject} {body}")

        # Check carrier shipment authorization
        auth_check = validate_carrier_shipment_authorization(carrier, tracking_ids)

        # CASE 1: Carrier referenced shipment(s) and NONE are assigned to this carrier
        if tracking_ids and len(auth_check["authorized_ids"]) == 0:
            logger.warning(f"🚫 [UNAUTHORIZED] Carrier '{carrier_name}' attempted to access/update unauthorized shipments: {auth_check['unauthorized_ids']}")
            sec_carrier_act = f"Security Log: Carrier '{carrier_name}' unauthorized to access or update shipment(s): {', '.join(auth_check['unauthorized_ids'])}."
            final_status = "under_review"
            e_data = email_data or {
                "id": email_id,
                "subject": subject,
                "body_text": body,
                "from": {"name": sender_name, "email": sender_email},
                "attachments": []
            }
            jev_eval = self.jev.evaluate(
                subject=subject,
                body=body,
                sender_type="carrier",
                account_context=carrier,
                tracking_context={"unauthorized_ids": auth_check["unauthorized_ids"]}
            )
            jev_eval["category"] = "unauthorized_parcel_access"
            jev_eval["urgency"] = 1
            jev_eval["needs_manager_approval"] = False
            if "policy_compliance" in jev_eval and isinstance(jev_eval["policy_compliance"], dict):
                jev_eval["policy_compliance"]["manager_escalation_required"] = False

            drafting_res = self.drafter.draft(
                email_data=e_data,
                jev_eval=jev_eval,
                shipment_context=None,
                account_context=carrier,
                authorized_ids=[],
                unauthorized_ids=auth_check["unauthorized_ids"]
            )
            carrier_draft_str = str(drafting_res.get("draft_reply") or "")
            carrier_actions_unauth_list: List[str] = [str(a) for a in drafting_res.get("suggested_internal_actions", [sec_carrier_act])]
            carrier_unauth_dec = {
                "email_id": email_id,
                "sender_email": sender_email,
                "status": final_status,
                "final_status": final_status,
                "verification_status": "verified",
                "authorization_status": "unauthorized",
                "sender_type": "carrier",
                "carrier": carrier,
                "workflow_routed_to": "carrier_workflow",
                "tracking_ids": tracking_ids,
                "authorized_ids": [],
                "unauthorized_ids": auth_check["unauthorized_ids"],
                "urgency": 1,
                "needs_manager_approval": False,
                "action_taken": f"Carrier Boundary Notice: Shipment(s) {', '.join(auth_check['unauthorized_ids'])} are assigned to other carriers in Northwind network. Boundary notice drafted for reviewer sign-off.",
                "requires_reply": True,
                "reviewer_status": "under_review",
                "draft_reply": carrier_draft_str,
                "suggested_internal_actions": carrier_actions_unauth_list,
                "internal_action": str(drafting_res.get("internal_action") or sec_carrier_act),
                "jev_evaluation": jev_eval
            }
            case_audit = build_case_audit(e_data, carrier_unauth_dec, shipment_context=None, carrier_context=carrier, reviewer_email=self.mailer.reviewer_email)
            carrier_unauth_dec["audit_data"] = case_audit

            review_dispatch = self.mailer.send_review_request(
                email_id=email_id,
                sender_type="carrier",
                sender_name=sender_name or (carrier.get("carrier") if carrier else "Carrier Operations") or "Carrier Operations",
                sender_email=sender_email,
                original_email=e_data,
                draft_reply=carrier_draft_str,
                suggested_internal_actions=carrier_actions_unauth_list,
                jev_evaluation=jev_eval,
                shipment_context=None,
                account_context=carrier,
                authorized_ids=[],
                derived_facts=case_audit.get("derived_facts", [])
            )
            carrier_unauth_dec["reviewer_dispatch"] = review_dispatch
            self.update_status(email_id, final_status, audit_data=case_audit)
            return carrier_unauth_dec

        # CASE 2: Take the workflow ahead with ONLY the shipment IDs that belong to this carrier
        authorized_ids = auth_check["authorized_ids"]
        unauthorized_ids = auth_check["unauthorized_ids"]

        if unauthorized_ids:
            logger.info(f"⚠️ [FILTERED] Proceeding only with authorized carrier shipment(s): {authorized_ids}. (Excluded mismatch: {unauthorized_ids})")

        affected_shipments = []
        primary_context = None
        for tid in authorized_ids:
            ctx = get_shipment_context(tid)
            if ctx:
                affected_shipments.append(ctx["shipment"])
                if not primary_context:
                    primary_context = ctx

        # Jev evaluation for carrier notice - pass all authorized shipments and carrier account
        tracking_payload = {
            "shipment": primary_context.get("shipment") if primary_context else {},
            "shipments": affected_shipments
        }
        jev_eval = self.jev.evaluate(
            subject=subject,
            body=body,
            sender_type="carrier",
            account_context=carrier,
            tracking_context=tracking_payload
        )

        # Operational Carrier Incident Handling: Update shipment notes in Northwind DB
        incident_type = self.drafter._extract_carrier_incident(subject, body)
        for sid in authorized_ids:
            note_content = f"Carrier notice from {carrier_name}: {incident_type} ({subject})"
            update_shipment_notes(sid, note_content)
            logger.info(f"📝 [DB NOTE UPDATED] Shipment {sid} notes updated with: '{note_content}'")

        # Groq Response Drafter & Reviewer Gating
        e_data = email_data or {
            "id": email_id,
            "subject": subject,
            "body_text": body,
            "from": {"name": sender_name, "email": sender_email},
            "attachments": []
        }
        drafting_res = self.drafter.draft(
            email_data=e_data,
            jev_eval=jev_eval,
            shipment_context=tracking_payload,
            account_context=carrier,
            authorized_ids=authorized_ids,
            unauthorized_ids=unauthorized_ids
        )

        carrier_reply = str(drafting_res.get("draft_reply") or "")
        carrier_actions_list: List[str] = [str(a) for a in drafting_res.get("suggested_internal_actions", [])]
        decision = {
            "email_id": email_id,
            "sender_email": sender_email,
            "verification_status": "verified",
            "authorization_status": "authorized",
            "sender_type": "carrier",
            "carrier": carrier,
            "workflow_routed_to": "carrier_workflow",
            "intent": jev_eval["category"],
            "urgency": jev_eval["urgency"],
            "needs_manager_approval": False,
            "manager_escalation_classification": "routine_coordinator_handling",
            "jev_evaluation": jev_eval,
            "tracking_ids": tracking_ids,
            "authorized_ids": authorized_ids,
            "unauthorized_ids": unauthorized_ids,
            "affected_shipments": affected_shipments,
            "incident_type": incident_type,
            "action_taken": f"Logged operational exception from carrier {carrier_name} for authorized shipments: {', '.join(authorized_ids) if authorized_ids else 'General notice'}",
            "requires_reply": drafting_res.get("requires_reply", True),
            "reviewer_status": drafting_res.get("reviewer_status", "pending_reviewer_approval"),
            "draft_reply": carrier_reply,
            "suggested_internal_actions": carrier_actions_list,
            "internal_action": str(drafting_res.get("internal_action") or "; ".join(carrier_actions_list))
        }

        case_audit = build_case_audit(e_data, decision, shipment_context=tracking_payload, carrier_context=carrier, reviewer_email=self.mailer.reviewer_email)
        decision["audit_data"] = case_audit

        # If reply is required, dispatch email to reviewer and update DB status to 'under_review'
        if decision.get("requires_reply"):
            review_dispatch = self.mailer.send_review_request(
                email_id=email_id,
                sender_type="carrier",
                sender_name=sender_name or (carrier.get("carrier") if carrier else "Carrier Operations") or "Carrier Operations",
                sender_email=sender_email,
                original_email=e_data,
                draft_reply=carrier_reply,
                suggested_internal_actions=carrier_actions_list,
                jev_evaluation=jev_eval,
                shipment_context=primary_context,
                account_context=carrier,
                authorized_ids=authorized_ids,
                derived_facts=case_audit.get("derived_facts", [])
            )
            decision["reviewer_dispatch"] = review_dispatch
            decision["reviewer_status"] = "under_review"
            final_status = "under_review"
        else:
            final_status = "carrier_notice_processed"

        self.update_status(email_id, final_status, audit_data=case_audit)
        logger.info(f"🏁 [WORKFLOW END] Email #{email_id} -> {final_status} (Urgency: {jev_eval['urgency']}/5)")
        return decision
