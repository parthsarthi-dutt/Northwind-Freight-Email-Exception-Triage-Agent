import os
import json
import logging
from typing import Any, Dict, Optional
import redis

logger = logging.getLogger("shipsy.redis_stream")

DEFAULT_STREAM_NAME = "email:stream"

def get_redis_connection(redis_url: Optional[str] = None) -> redis.Redis:
    """Creates a Redis client from configuration."""
    url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(url, decode_responses=True)

def format_email_for_stream(email_record: Dict[str, Any]) -> Dict[str, str]:
    """
    Formats the email record for the Redis stream.
    Contains everything EXCEPT the status column, as requested:
      - id
      - from (JSON object with name and email)
      - to
      - subject
      - body_text
      - received_at
      - headers (JSON object)
      - attachments (JSON list)
      - created_at
    """
    stream_data = {
        "id": str(email_record.get("id", "")),
        "from": json.dumps(email_record.get("from", {})),
        "to": str(email_record.get("to", "")),
        "subject": str(email_record.get("subject", "")),
        "body_text": str(email_record.get("body_text", "")),
        "received_at": str(email_record.get("received_at", "")),
        "headers": json.dumps(email_record.get("headers", {})),
        "attachments": json.dumps(email_record.get("attachments", [])),
        "created_at": str(email_record.get("created_at", ""))
    }
    # Explicitly ensure 'status' is NOT in the stream payload
    assert "status" not in stream_data, "Status must not be included in the stream message"
    return stream_data

def publish_email_to_stream(
    email_record: Dict[str, Any],
    stream_name: Optional[str] = None,
    client: Optional[redis.Redis] = None
) -> Optional[str]:
    """
    Publishes an email to the configured Redis Stream using XADD.
    Returns the generated stream message ID, or None if publishing failed/disabled.
    """
    # Check if redis stream publishing is enabled
    enabled_env = os.environ.get("REDIS_ENABLED", "true").lower()
    if enabled_env in ("false", "0", "no"):
        return None

    target_stream = stream_name or os.environ.get("REDIS_STREAM_NAME", DEFAULT_STREAM_NAME)
    stream_payload = format_email_for_stream(email_record)

    try:
        r = client or get_redis_connection()
        msg_id = r.xadd(target_stream, stream_payload)
        logger.info(f"[Redis Stream] Published email ID {email_record.get('id')} to stream '{target_stream}' (msg_id: {msg_id})")
        return msg_id
    except Exception as e:
        logger.warning(f"[Redis Stream] Could not publish to stream '{target_stream}': {e}. (Verify Redis is running on {os.environ.get('REDIS_URL', 'localhost:6379')})")
        return None
