import os
import pytest
from app.logger import log_email_received, LOG_FILE_PATH

def test_email_logging(client):
    payload = {
        "from": {
            "name": "Logger Test User",
            "email": "logtest@example.com"
        },
        "to": "ops@northwind-freight.example",
        "subject": "Testing Ingestion Logging",
        "body_text": "This email tests structured logging.",
        "received_at": "2026-10-25T10:00:00",
        "headers": {},
        "attachments": []
    }

    # Post to webhook
    res = client.post('/webhook/email', json=payload)
    assert res.status_code == 201

    # Check log file exists and contains the logged event
    assert os.path.exists(LOG_FILE_PATH)
    with open(LOG_FILE_PATH, 'r', encoding='utf-8') as f:
        log_content = f.read()

    assert "Testing Ingestion Logging" in log_content
    assert "logtest@example.com" in log_content
    assert "Status:      pending" in log_content
