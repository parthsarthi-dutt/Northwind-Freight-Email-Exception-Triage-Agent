import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import sqlite3
from app.db import get_db

def format_email_record(row: sqlite3.Row) -> Dict[str, Any]:
    """Formats a database row into the desired structured email response."""
    audit_val = {}
    if "audit_data" in row.keys() and row["audit_data"]:
        try:
            audit_val = json.loads(row["audit_data"])
        except Exception:
            audit_val = {}

    return {
        "id": row["id"],
        "from": {
            "name": row["from_name"] or "",
            "email": row["from_email"]
        },
        "to": row["to_address"],
        "subject": row["subject"],
        "body_text": row["body_text"],
        "received_at": row["received_at"],
        "headers": json.loads(row["headers"]) if row["headers"] else {},
        "attachments": json.loads(row["attachments"]) if row["attachments"] else [],
        "status": row["status"],
        "audit_data": audit_val,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"]
    }

def save_email(email_data: Dict[str, Any]) -> int:
    """
    Saves extracted email data into the SQLite database with status='pending'.
    Returns the newly inserted record id.
    """
    db = get_db()
    from_name = email_data.get("from_name", "")
    from_email = email_data.get("from_email")
    if not from_email and "from" in email_data:
        from_val = email_data["from"]
        if isinstance(from_val, dict):
            from_name = from_name or from_val.get("name", "")
            from_email = from_val.get("email", "")
        elif isinstance(from_val, str):
            from_email = from_val

    to_address = email_data.get("to_address")
    if not to_address and "to" in email_data:
        to_val = email_data["to"]
        if isinstance(to_val, list) and to_val:
            to_address = to_val[0].get("email", "") if isinstance(to_val[0], dict) else str(to_val[0])
        elif isinstance(to_val, dict):
            to_address = to_val.get("email", "")
        elif isinstance(to_val, str):
            to_address = to_val

    now_iso = datetime.now(timezone.utc).isoformat()
    headers_json = json.dumps(email_data.get("headers", {}))
    attachments_json = json.dumps(email_data.get("attachments", []))
    status = email_data.get("status", "pending")

    cursor = db.execute(
        """
        INSERT INTO emails (
            from_name,
            from_email,
            to_address,
            subject,
            body_text,
            received_at,
            headers,
            attachments,
            status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            from_name or "",
            from_email or "unknown@mailbox.example",
            to_address or "support@northwind-freight.example",
            email_data.get("subject", ""),
            email_data.get("body_text", ""),
            email_data.get("received_at", now_iso),
            headers_json,
            attachments_json,
            status,
            now_iso,
            now_iso
        )
    )
    db.commit()
    return cursor.lastrowid

def get_email_by_id(email_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves an email by its primary key ID."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM emails WHERE id = ?",
        (email_id,)
    ).fetchone()
    
    if row is None:
        return None
    return format_email_record(row)

def list_emails(status: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Lists emails with optional status filter, ordered newest first."""
    db = get_db()
    if status:
        clean_status = str(status).strip().lower()
        if clean_status in ("escalated", "sent_to_manager", "sent to manager"):
            if clean_status == "escalated":
                rows = db.execute(
                    """
                    SELECT * FROM emails 
                    WHERE status IN ('escalated', 'sent_to_manager', 'sent to manager', 'unauthorized') 
                    ORDER BY id DESC 
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset)
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM emails 
                    WHERE status IN ('sent_to_manager', 'sent to manager') 
                    ORDER BY id DESC 
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset)
                ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT * FROM emails 
                WHERE status = ? 
                ORDER BY id DESC 
                LIMIT ? OFFSET ?
                """,
                (status, limit, offset)
            ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT * FROM emails 
            ORDER BY id DESC 
            LIMIT ? OFFSET ?
            """,
            (limit, offset)
        ).fetchall()
        
    return [format_email_record(r) for r in rows]

def update_email_status(email_id: int, new_status: str, audit_data: Optional[Dict[str, Any]] = None) -> bool:
    """Updates the status and optional audit data of an email."""
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    if audit_data is not None:
        audit_json = json.dumps(audit_data)
        cursor = db.execute(
            """
            UPDATE emails
            SET status = ?, audit_data = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_status, audit_json, now_iso, email_id)
        )
    else:
        cursor = db.execute(
            """
            UPDATE emails
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_status, now_iso, email_id)
        )
    db.commit()
    return cursor.rowcount > 0
