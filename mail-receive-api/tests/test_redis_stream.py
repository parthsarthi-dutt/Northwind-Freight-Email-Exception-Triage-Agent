import json
import pytest
import fakeredis
from app.redis_stream import format_email_for_stream, publish_email_to_stream

def test_format_email_for_stream_excludes_status():
    sample_email = {
        "id": 42,
        "from": {
            "name": "Marcus Webb",
            "email": "marcus.webb@mailbox.example"
        },
        "to": "ops@northwind-freight.example",
        "subject": "Parcel NWF-20101 is late",
        "body_text": "Hi, where is my parcel?",
        "received_at": "2026-10-19T09:07:00",
        "headers": {"Message-ID": "<abc@example.com>"},
        "attachments": [{"filename": "invoice.pdf"}],
        "status": "pending",  # <--- MUST NOT be in stream
        "created_at": "2026-10-25T15:00:00"
    }

    stream_payload = format_email_for_stream(sample_email)

    # 1. Assert status is explicitly absent
    assert "status" not in stream_payload

    # 2. Assert all other email content is present
    assert stream_payload["id"] == "42"
    assert json.loads(stream_payload["from"]) == sample_email["from"]
    assert stream_payload["to"] == "ops@northwind-freight.example"
    assert stream_payload["subject"] == "Parcel NWF-20101 is late"
    assert stream_payload["body_text"] == "Hi, where is my parcel?"
    assert stream_payload["received_at"] == "2026-10-19T09:07:00"
    assert json.loads(stream_payload["headers"]) == {"Message-ID": "<abc@example.com>"}
    assert json.loads(stream_payload["attachments"]) == [{"filename": "invoice.pdf"}]
    assert stream_payload["created_at"] == "2026-10-25T15:00:00"

def test_publish_to_stream_with_fakeredis():
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    stream_name = "test:email:stream"

    email_data = {
        "id": 101,
        "from": {"name": "Test Sender", "email": "sender@test.com"},
        "to": "support@shipsy.test",
        "subject": "Urgent Freight Inquiry",
        "body_text": "Please check container #7788.",
        "received_at": "2026-10-25T15:05:00",
        "headers": {},
        "attachments": [],
        "status": "pending"
    }

    # Publish to Redis Stream
    msg_id = publish_email_to_stream(email_data, stream_name=stream_name, client=fake_client)
    assert msg_id is not None

    # Verify stream length is 1
    assert fake_client.xlen(stream_name) == 1

    # Read from stream and verify status is not present
    entries = fake_client.xread({stream_name: "0-0"}, count=1)
    assert len(entries) == 1
    stream_key, messages = entries[0]
    msg_id, fields = messages[0]
    assert "status" not in fields
    assert fields["id"] == "101"
    assert fields["subject"] == "Urgent Freight Inquiry"
