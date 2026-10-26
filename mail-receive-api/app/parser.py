import json
from datetime import datetime, timezone
import email.utils
from typing import Any, Dict, Tuple

class EmailParseError(Exception):
    """Raised when incoming mail payload cannot be parsed or lacks required fields."""
    pass

def parse_from_field(from_val: Any) -> Tuple[str, str]:
    """
    Parses the 'from' field into (name, email).
    Supports:
      - dict: {"name": "Marcus Webb", "email": "marcus.webb@mailbox.example"}
      - str: "Marcus Webb <marcus.webb@mailbox.example>" or "marcus.webb@mailbox.example"
    """
    if isinstance(from_val, dict):
        email_addr = from_val.get("email", "").strip()
        name = from_val.get("name", "").strip()
        if not email_addr:
            raise EmailParseError("Field 'from.email' is required.")
        return name, email_addr
    elif isinstance(from_val, str) and from_val.strip():
        name, email_addr = email.utils.parseaddr(from_val.strip())
        if not email_addr:
            email_addr = from_val.strip()
        return name, email_addr
    else:
        raise EmailParseError("Field 'from' must be an object with {name, email} or a valid string.")

def parse_to_field(to_val: Any) -> str:
    """
    Parses the 'to' field into a recipient string.
    Supports:
      - str: "ops@northwind-freight.example"
      - list: ["ops@northwind-freight.example", ...]
      - dict: {"email": "ops@northwind-freight.example"}
    """
    if isinstance(to_val, str) and to_val.strip():
        return to_val.strip()
    elif isinstance(to_val, list) and len(to_val) > 0:
        first = to_val[0]
        if isinstance(first, dict):
            return first.get("email", "").strip() or str(first)
        return str(first).strip()
    elif isinstance(to_val, dict):
        return to_val.get("email", "").strip() or str(to_val)
    raise EmailParseError("Field 'to' is required and must specify a recipient email.")

def parse_incoming_email_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts structured email data from the webhook payload.
    Expected output keys:
      - from_name: str
      - from_email: str
      - to_address: str
      - subject: str
      - body_text: str
      - received_at: str (ISO format)
      - headers: dict
      - attachments: list
      - status: 'pending'
    """
    if not isinstance(payload, dict):
        raise EmailParseError("Invalid payload format: Expected a JSON object.")

    # 1. 'from'
    if "from" not in payload:
        raise EmailParseError("Missing required field: 'from'")
    from_name, from_email = parse_from_field(payload["from"])

    # 2. 'to'
    if "to" not in payload:
        raise EmailParseError("Missing required field: 'to'")
    to_address = parse_to_field(payload["to"])

    # 3. 'subject'
    subject = str(payload.get("subject", "")).strip()

    # 4. 'body_text' (fallback to 'text' or 'body' if body_text not explicitly given)
    body_text = payload.get("body_text")
    if body_text is None:
        body_text = payload.get("text") or payload.get("body") or ""
    body_text = str(body_text)

    # 5. 'received_at'
    received_at_raw = payload.get("received_at")
    if received_at_raw:
        received_at = str(received_at_raw).strip()
    else:
        # Default to current UTC ISO timestamp
        received_at = datetime.now(timezone.utc).isoformat()

    # 6. 'headers'
    headers_val = payload.get("headers")
    if headers_val is None:
        headers_dict = {}
    elif isinstance(headers_val, dict):
        headers_dict = headers_val
    elif isinstance(headers_val, list):
        # Header list of {"name": ..., "value": ...} or tuples
        headers_dict = {}
        for item in headers_val:
            if isinstance(item, dict) and "name" in item and "value" in item:
                headers_dict[str(item["name"])] = item["value"]
    else:
        headers_dict = {"raw": str(headers_val)}

    # 7. 'attachments'
    attachments_val = payload.get("attachments")
    if attachments_val is None:
        attachments_list = []
    elif isinstance(attachments_val, list):
        attachments_list = attachments_val
    else:
        attachments_list = [attachments_val]

    return {
        "from_name": from_name,
        "from_email": from_email,
        "to_address": to_address,
        "subject": subject,
        "body_text": body_text,
        "received_at": received_at,
        "headers": headers_dict,
        "attachments": attachments_list,
        "status": "pending"
    }
