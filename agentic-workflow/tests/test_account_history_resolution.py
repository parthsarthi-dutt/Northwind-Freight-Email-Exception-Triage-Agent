import json
import pytest
from agent import EmailAgent
import northwind_db

def test_sara_lindqvist_account_history_resolution(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "patch", lambda u, json, timeout: type("Resp", (), {"status_code": 200})())

    agent = EmailAgent(api_url="http://fake-ingestion-api")
    email_data = {
        "id": 34,
        "from": {"name": "Sara Lindqvist", "email": "sara.lindqvist@mailbox.example"},
        "to": "ops@northwind-freight.example",
        "subject": "Where's my order?",
        "body_text": "Hi, I ordered something last week and it should arrive soon. Could you let me know where it is? I don't have the tracking number.",
        "received_at": "2026-10-25T19:24:53",
        "attachments": []
    }

    result = agent.process(email_data)

    assert result["verification_status"] == "verified"
    assert result["sender_type"] == "customer"
    assert result["resolution_method"] == "account_history"
    assert "NWF-20181" in result["authorized_ids"]
    assert "NWF-20181" in result["tracking_ids"]
    assert result["requires_reply"] is True
    assert result["draft_reply"] is not None
    assert "NWF-20181" in result["draft_reply"]
    # Verify that it didn't ask the customer for a tracking number
    assert "provide us with the tracking number" not in result["draft_reply"].lower()

    audit = result.get("audit_data", {})
    assert audit.get("resolution_method") == "account_history"
    evidence = audit.get("evidence_looked_up", {})
    assert evidence.get("shipment_id") == "NWF-20181"
    assert evidence.get("customer") == "Sara Lindqvist"
    assert evidence.get("carrier") == "Swiftline"
    assert "Seattle" in evidence.get("route")
    assert "Portland" in evidence.get("route")
    assert evidence.get("status") == "In transit"
