import json
import pytest

def test_reviewer_accept_action_web(client):
    """
    Tests clicking 'Accept' button from email via GET /api/emails/<id>/review/action?decision=accept.
    Verifies:
      - Status in DB is updated to 'completed'
      - Audit trail records reviewer acceptance
      - HTML confirmation page is returned
    """
    # 1. Ingest an email
    payload = {
        "from": {"name": "Marcus Webb", "email": "marcus.webb@mailbox.example"},
        "to": "support@northwind-freight.example",
        "subject": "Status Inquiry for NWF-20101",
        "body_text": "Please provide an update.",
        "received_at": "2026-10-19T09:00:00"
    }
    resp = client.post('/webhook/email', json=payload)
    assert resp.status_code == 201
    email_id = resp.json["email"]["id"]

    # 2. Click 'Accept' link (GET request from reviewer email client)
    action_resp = client.get(f'/api/emails/{email_id}/review/action?decision=accept')
    assert action_resp.status_code == 200
    html = action_resp.data.decode('utf-8')
    assert "STATUS: COMPLETED" in html
    assert "Draft Approved & Case Completed" in html
    assert f"CASE-EMAIL-{email_id:04d}" in html

    # 3. Verify in database
    fetch_resp = client.get(f'/api/emails/{email_id}')
    assert fetch_resp.status_code == 200
    assert fetch_resp.json["status"] == "completed"
    audit = fetch_resp.json["audit_data"]
    assert audit.get("reviewer_status") == "completed"
    assert audit.get("reviewer_decision") == "accept"


def test_reviewer_reject_action_web(client):
    """
    Tests clicking 'Reject' button from email via GET /api/emails/<id>/review/action?decision=reject.
    Verifies:
      - Status in DB is updated to 'passed_to_reviewer'
      - Audit trail records reviewer rejection
      - HTML confirmation page is returned
    """
    # 1. Ingest an email
    payload = {
        "from": {"name": "Elena Rossi", "email": "elena.rossi@mailbox.example"},
        "to": "support@northwind-freight.example",
        "subject": "Missing Parcel NWF-20102",
        "body_text": "I need help with this package immediately.",
        "received_at": "2026-10-19T09:05:00"
    }
    resp = client.post('/webhook/email', json=payload)
    assert resp.status_code == 201
    email_id = resp.json["email"]["id"]

    # 2. Click 'Reject' link
    action_resp = client.get(f'/api/emails/{email_id}/review/action?decision=reject')
    assert action_resp.status_code == 200
    html = action_resp.data.decode('utf-8')
    assert "STATUS: ASSIGNED TO REVIEWER" in html
    assert "Draft Rejected & Assigned to Reviewer" in html

    # 3. Verify in database
    fetch_resp = client.get(f'/api/emails/{email_id}')
    assert fetch_resp.status_code == 200
    assert fetch_resp.json["status"] == "passed_to_reviewer"
    audit = fetch_resp.json["audit_data"]
    assert audit.get("reviewer_status") == "passed_to_reviewer"
    assert audit.get("reviewer_decision") == "reject"


def test_reviewer_action_api_json(client):
    """Tests API POST interaction with JSON payload."""
    payload = {
        "from": {"name": "Sara Lindqvist", "email": "sara.lindqvist@mailbox.example"},
        "to": "support@northwind-freight.example",
        "subject": "Where is my order?",
        "body_text": "I ordered last week.",
        "received_at": "2026-10-19T09:10:00"
    }
    resp = client.post('/webhook/email', json=payload)
    email_id = resp.json["email"]["id"]

    # Post accept decision via JSON
    action_resp = client.post(
        f'/api/emails/{email_id}/review/action',
        json={"decision": "accept"},
        headers={"Accept": "application/json"}
    )
    assert action_resp.status_code == 200
    assert action_resp.json["success"] is True
    assert action_resp.json["status"] == "completed"

    # Attempt to post reject on the SAME email_id: must be blocked because user already responded
    duplicate_resp = client.post(
        f'/api/emails/{email_id}/review/action',
        json={"decision": "reject"},
        headers={"Accept": "application/json"}
    )
    assert duplicate_resp.status_code == 200
    assert duplicate_resp.json["already_responded"] is True
    assert duplicate_resp.json["status"] == "completed"

    # Post reject decision via JSON on a distinct email to verify reject works independently
    payload2 = {
        "from": {"name": "Lars Olof", "email": "lars.olof@mailbox.example"},
        "to": "support@northwind-freight.example",
        "subject": "Missing items in order",
        "body_text": "I received only part of my order.",
        "received_at": "2026-10-19T09:15:00"
    }
    resp2 = client.post('/webhook/email', json=payload2)
    email_id2 = resp2.json["email"]["id"]

    action_resp2 = client.post(
        f'/api/emails/{email_id2}/review/action',
        json={"decision": "reject"},
        headers={"Accept": "application/json"}
    )
    assert action_resp2.status_code == 200
    assert action_resp2.json["success"] is True
    assert action_resp2.json["status"] == "passed_to_reviewer"


def test_reviewer_already_responded_web(client):
    """
    Tests clicking an action link again from email after already responding.
    Verifies:
      - Returns 'You Have Already Responded' page with 'STATUS: ALREADY RESPONDED'
      - Action buttons are marked as disabled
      - Database status is not modified
    """
    # 1. Ingest an email
    payload = {
        "from": {"name": "Clara Schmidt", "email": "clara.schmidt@mailbox.example"},
        "to": "support@northwind-freight.example",
        "subject": "Shipment Query NWF-20104",
        "body_text": "Please provide status.",
        "received_at": "2026-10-19T09:30:00"
    }
    resp = client.post('/webhook/email', json=payload)
    email_id = resp.json["email"]["id"]

    # 2. Click 'Accept' first time
    first_resp = client.get(f'/api/emails/{email_id}/review/action?decision=accept')
    assert first_resp.status_code == 200
    assert "Draft Approved & Case Completed" in first_resp.data.decode('utf-8')

    # 3. Click 'Reject' afterwards (attempting to change decision)
    second_resp = client.get(f'/api/emails/{email_id}/review/action?decision=reject')
    assert second_resp.status_code == 200
    html = second_resp.data.decode('utf-8')
    assert "STATUS: ALREADY RESPONDED" in html
    assert "You Have Already Responded" in html
    assert "Disabled" in html

    # 4. Database status must remain 'completed', not overwritten
    fetch_resp = client.get(f'/api/emails/{email_id}')
    assert fetch_resp.json["status"] == "completed"
