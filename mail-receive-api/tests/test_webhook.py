import json
import pytest

def test_health_check(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json['status'] == 'healthy'

def test_webhook_exact_user_sample(client):
    """
    Tests the exact payload requested by the user:
    "from": {
      "name": "Marcus Webb",
      "email": "marcus.webb@mailbox.example"
    },
    "to": "ops@northwind-freight.example",
    "subject": "Parcel NWF-20101 is late",
    "body_text": "Hi, my parcel NWF-20101 was due two days ago and the tracking has not moved since Friday. Can you tell me where it is?\n\nThanks,\nMarcus Webb",
    "received_at": "2026-10-19T09:07:00",
    "headers": {},
    "attachments": []
    """
    payload = {
        "from": {
            "name": "Marcus Webb",
            "email": "marcus.webb@mailbox.example"
        },
        "to": "ops@northwind-freight.example",
        "subject": "Parcel NWF-20101 is late",
        "body_text": "Hi, my parcel NWF-20101 was due two days ago and the tracking has not moved since Friday. Can you tell me where it is?\n\nThanks,\nMarcus Webb",
        "received_at": "2026-10-19T09:07:00",
        "headers": {},
        "attachments": []
    }

    response = client.post('/webhook/email', json=payload)
    assert response.status_code == 201
    data = response.json
    assert "email" in data
    email_data = data["email"]
    assert email_data["id"] == 1
    assert email_data["from"]["name"] == "Marcus Webb"
    assert email_data["from"]["email"] == "marcus.webb@mailbox.example"
    assert email_data["to"] == "ops@northwind-freight.example"
    assert email_data["subject"] == "Parcel NWF-20101 is late"
    assert "tracking has not moved" in email_data["body_text"]
    assert email_data["received_at"] == "2026-10-19T09:07:00"
    assert email_data["headers"] == {}
    assert email_data["attachments"] == []
    # Verify status is 'pending'
    assert email_data["status"] == "pending"

    # Verify querying by ID returns identical stored data
    fetch_resp = client.get(f"/api/emails/{email_data['id']}")
    assert fetch_resp.status_code == 200
    assert fetch_resp.json["status"] == "pending"
    assert fetch_resp.json["from"]["email"] == "marcus.webb@mailbox.example"

def test_webhook_string_sender_and_defaults(client):
    """Test flexible parsing of sender string and default received_at."""
    payload = {
        "from": "Alice Smith <alice@example.com>",
        "to": "support@example.com",
        "subject": "Inquiry about shipment",
        "body_text": "Where is order #456?"
    }

    response = client.post('/webhook/email', json=payload)
    assert response.status_code == 201
    email_data = response.json["email"]
    assert email_data["from"]["name"] == "Alice Smith"
    assert email_data["from"]["email"] == "alice@example.com"
    assert email_data["to"] == "support@example.com"
    assert email_data["status"] == "pending"
    assert email_data["received_at"] is not None
    assert email_data["headers"] == {}
    assert email_data["attachments"] == []

def test_list_emails_and_filter_by_status(client):
    # Post 2 emails
    p1 = {
        "from": {"name": "Bob", "email": "bob@example.com"},
        "to": "info@example.com",
        "subject": "Email 1",
        "body_text": "Content 1"
    }
    p2 = {
        "from": {"name": "Charlie", "email": "charlie@example.com"},
        "to": "info@example.com",
        "subject": "Email 2",
        "body_text": "Content 2"
    }
    r1 = client.post('/webhook/email', json=p1).json["email"]
    r2 = client.post('/webhook/email', json=p2).json["email"]

    # Filter status=pending
    resp = client.get('/api/emails?status=pending')
    assert resp.status_code == 200
    assert resp.json["count"] == 2

    # Update r1 to processed
    patch_resp = client.patch(f"/api/emails/{r1['id']}/status", json={"status": "processed"})
    assert patch_resp.status_code == 200
    assert patch_resp.json["email"]["status"] == "processed"

    # Filter status=pending now returns 1
    resp_pending = client.get('/api/emails?status=pending')
    assert resp_pending.json["count"] == 1
    assert resp_pending.json["emails"][0]["id"] == r2["id"]

    # Filter status=processed returns 1
    resp_proc = client.get('/api/emails?status=processed')
    assert resp_proc.json["count"] == 1
    assert resp_proc.json["emails"][0]["id"] == r1["id"]

def test_missing_required_fields(client):
    # Missing 'from'
    res = client.post('/webhook/email', json={"to": "test@example.com"})
    assert res.status_code == 422
    assert "Missing required field: 'from'" in res.json["message"]

    # Missing 'to'
    res = client.post('/webhook/email', json={"from": {"email": "test@example.com"}})
    assert res.status_code == 422
    assert "Missing required field: 'to'" in res.json["message"]

def test_non_existent_email_404(client):
    res = client.get('/api/emails/99999')
    assert res.status_code == 404
