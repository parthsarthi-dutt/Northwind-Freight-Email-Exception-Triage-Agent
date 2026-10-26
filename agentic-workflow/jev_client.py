"""
TypeSafe AI Jev Decision Model Client.
Clean standalone implementation for agentic-workflow with Groq LLM inference,
typed questions evaluation, policy enforcement, and manager approval classification.
"""

import os
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
import requests

from config import Config
from northwind_db import get_connection
from jev_classifier import JevClassifier as CoreJevClassifier

logger = logging.getLogger("agentic_workflow.jev_client")


@dataclass
class SanitizedEmail:
    """Represents a sanitized incoming email payload."""
    email_id: int
    subject: str
    cleaned_body: str
    sender_email: str = ""
    sender_name: str = ""
    injection_flags: List[str] = field(default_factory=list)
    auto_submitted: bool = False
    context: Optional[Dict[str, Any]] = None


@dataclass
class ClassificationResult:
    """Represents the structured outcome of a Jev decision model evaluation."""
    category: str
    confidence: float
    urgency_score: int
    has_legal_threat: bool
    has_press_threat: bool
    has_injection_flag: bool
    is_auto_reply: bool
    needs_customer_reply: bool
    needs_manager_approval: bool = False
    manager_escalation_classification: str = "routine_coordinator_handling"
    classifier_used: str = ""
    classifier_raw: Dict[str, Any] = field(default_factory=dict)
    uncertain: bool = False


class BaseClassifier:
    """Base interface for email classification models."""
    def classify(self, email: SanitizedEmail, context: Optional[Dict[str, Any]] = None) -> ClassificationResult:
        raise NotImplementedError


class JevClassifier(BaseClassifier):
    """
    TypeSafe AI Jev 'System One' Decision Model Client.
    Evaluates state (email text) and typed questions (Category, Urgency, Threat, Injection, Manager Approval).
    Uses Groq LLM inference with fallback to policy-grounded decision engine.
    """

    def __init__(self):
        self.api_url = os.environ.get("JEV_API_URL", "https://api.typesafe.ai/v1/jev/evaluate")
        self.api_key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY", "")
        self.model = os.environ.get("JEV_MODEL", "jev-1.14.0")
        self.groq_key = Config.GROQ_API_KEY or os.environ.get("GROQ_API_KEY", "")
        self.groq_model = Config.GROQ_DEFAULT_MODEL
        self.core = CoreJevClassifier(model=self.model, groq_model=self.groq_model)

    def classify(self, email: SanitizedEmail, context: Optional[Dict[str, Any]] = None) -> ClassificationResult:
        t0 = time.time()
        combined_context = context or email.context or {}
        tracking_ctx = combined_context.get("tracking") or combined_context.get("shipment_context")
        account_ctx = combined_context.get("account") or combined_context.get("account_context")

        # Evaluate via Core Jev Classifier
        eval_result = self.core.evaluate(
            subject=email.subject,
            body=email.cleaned_body,
            sender_type="customer",
            account_context=account_ctx,
            tracking_context=tracking_ctx
        )

        latency_ms = int((time.time() - t0) * 1000)

        # Manager Approval Classification
        needs_manager = eval_result.get("needs_manager_approval", False)
        manager_classification = eval_result.get("manager_escalation_classification", "routine_coordinator_handling")

        threat_info = eval_result.get("threat", {})
        injection_info = eval_result.get("injection_defense", {})

        return ClassificationResult(
            category=eval_result.get("category", "other"),
            confidence=0.95,
            urgency_score=eval_result.get("urgency", 2),
            has_legal_threat=threat_info.get("has_legal_threat", False),
            has_press_threat=threat_info.get("has_press_threat", False),
            has_injection_flag=injection_info.get("has_injection_flag", False),
            is_auto_reply=False,
            needs_customer_reply=eval_result.get("requires_reply", True),
            needs_manager_approval=needs_manager,
            manager_escalation_classification=manager_classification,
            classifier_used=f"JEV-{self.model}|Groq-{self.groq_model}",
            classifier_raw=eval_result
        )


# Global default instance
jev_classifier = JevClassifier()
