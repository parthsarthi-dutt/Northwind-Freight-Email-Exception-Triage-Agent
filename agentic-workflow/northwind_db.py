"""
Permanent Northwind Freight Context Database Manager
Provides fast contextual lookups for the Agentic Workflow.
"""
import os
import sqlite3
from typing import Dict, Any, List, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "northwind.db")

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def get_shipment_context(shipment_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves full 360-degree context for a shipment:
      - Shipment details
      - Account / Customer details (tier, prior claims)
      - Chronological event tracking timeline
    """
    clean_id = shipment_id.strip().upper()
    conn = get_connection()
    try:
        # 1. Fetch shipment
        shipment_row = conn.execute(
            "SELECT * FROM shipments WHERE UPPER(shipment_id) = ?",
            (clean_id,)
        ).fetchone()

        if not shipment_row:
            return None

        shipment = dict(shipment_row)

        # 2. Fetch associated account
        account_id = shipment.get("account_id")
        account = None
        if account_id:
            acc_row = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,)
            ).fetchone()
            if acc_row:
                account = dict(acc_row)

        # 3. Fetch tracking events
        event_rows = conn.execute(
            "SELECT * FROM events WHERE UPPER(shipment_id) = ? ORDER BY timestamp ASC",
            (clean_id,)
        ).fetchall()
        events = [dict(r) for r in event_rows]

        return {
            "shipment": shipment,
            "account": account,
            "events": events
        }
    finally:
        conn.close()

def get_account_by_email(email_address: str) -> Optional[Dict[str, Any]]:
    """Looks up customer account details by email address."""
    clean_email = email_address.strip().lower()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE LOWER(email) = ?",
            (clean_email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def get_carrier_by_email(email_address: str) -> Optional[Dict[str, Any]]:
    """
    Looks up carrier details by ops_contact email or carrier email domain.
    """
    clean_email = email_address.strip().lower()
    conn = get_connection()
    try:
        # 1. Exact ops_contact match
        row = conn.execute(
            "SELECT * FROM carriers WHERE LOWER(ops_contact) = ?",
            (clean_email,)
        ).fetchone()
        if row:
            return dict(row)

        # 2. Domain matching (e.g. any @swiftline.example, @blueroute.example, etc.)
        if "@" in clean_email:
            domain = clean_email.split("@")[1].strip()
            all_carriers = conn.execute("SELECT * FROM carriers").fetchall()
            for c in all_carriers:
                c_dict = dict(c)
                ops_contact = (c_dict.get("ops_contact") or "").lower()
                if "@" in ops_contact:
                    ops_domain = ops_contact.split("@")[1].strip()
                    if domain == ops_domain:
                        return c_dict

        return None
    finally:
        conn.close()

def verify_sender_email(email_address: str) -> Dict[str, Any]:
    """
    Step 1 Verification:
    Verifies whether the incoming email is:
      1. A Carrier email (ops_contact or carrier domain match)
      2. A registered Customer account email
      3. Unverified / Does not exist in database
    """
    clean_email = (email_address or "").strip().lower()
    if not clean_email:
        return {
            "verified": False,
            "sender_type": "unverified",
            "email": "",
            "carrier": None,
            "account": None,
            "message": "Sender email is empty or missing."
        }

    # 1. Check if it's a carrier email
    carrier = get_carrier_by_email(clean_email)
    if carrier:
        return {
            "verified": True,
            "sender_type": "carrier",
            "email": clean_email,
            "carrier": carrier,
            "account": None,
            "message": f"Verified carrier email for '{carrier['carrier']}'"
        }

    # 2. Check if it's a registered customer account
    account = get_account_by_email(clean_email)
    if account:
        return {
            "verified": True,
            "sender_type": "customer",
            "email": clean_email,
            "carrier": None,
            "account": account,
            "message": f"Verified customer account for '{account.get('contact')}' (Tier: {account.get('tier')})"
        }

    # 3. Neither matches
    return {
        "verified": False,
        "sender_type": "unverified",
        "email": clean_email,
        "carrier": None,
        "account": None,
        "message": f"Email '{clean_email}' is not verified or does not exist in the database."
    }

def get_shipments_by_account(account_id: str, customer_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Finds all shipments belonging to an account, ordered by ship_date descending (most recent first)."""
    conn = get_connection()
    try:
        norm_id = normalize_account_id(account_id)
        raw_id = str(account_id).strip() if account_id else ""
        c_name = customer_name.strip().lower() if customer_name else ""
        rows = conn.execute(
            """
            SELECT * FROM shipments 
            WHERE account_id = ? OR account_id = ? OR (? != '' AND LOWER(customer) = ?)
            ORDER BY ship_date DESC
            """,
            (raw_id, norm_id, c_name, c_name)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def normalize_account_id(acc_id: Any) -> str:
    """Normalizes account_id e.g. '8801.0' -> '8801'."""
    if not acc_id:
        return ""
    s = str(acc_id).strip()
    return s.split(".")[0] if "." in s else s

def validate_customer_shipment_authorization(account: Dict[str, Any], shipment_ids: List[str]) -> Dict[str, Any]:
    """
    Validates whether the shipment IDs belong to the customer account.
    Returns categorized lists of authorized, unauthorized, and unknown shipment IDs.
    """
    customer_acc_id = normalize_account_id(account.get("account_id"))
    customer_name = (account.get("contact") or "").strip().lower()

    authorized_shipments = []
    unauthorized_shipments = []
    unknown_ids = []

    for sid in shipment_ids:
        ctx = get_shipment_context(sid)
        if not ctx:
            unknown_ids.append(sid)
            continue

        shipment = ctx["shipment"]
        shipment_acc_id = normalize_account_id(shipment.get("account_id"))
        shipment_customer = (shipment.get("customer") or "").strip().lower()

        # Check ownership match
        if (customer_acc_id and shipment_acc_id == customer_acc_id) or (customer_name and shipment_customer == customer_name):
            authorized_shipments.append(shipment)
        else:
            unauthorized_shipments.append({
                "shipment_id": sid,
                "actual_customer": shipment.get("customer"),
                "actual_account_id": shipment.get("account_id")
            })

    # Authorized if at least one belongs or no tracking IDs were mentioned
    is_authorized = len(authorized_shipments) > 0 or (len(shipment_ids) == 0)

    return {
        "is_authorized": is_authorized,
        "has_unauthorized": len(unauthorized_shipments) > 0,
        "authorized_ids": [s["shipment_id"] for s in authorized_shipments],
        "authorized_shipments": authorized_shipments,
        "unauthorized_ids": [s["shipment_id"] for s in unauthorized_shipments],
        "unauthorized_shipments": unauthorized_shipments,
        "unknown_ids": unknown_ids
    }

def validate_carrier_shipment_authorization(carrier: Dict[str, Any], shipment_ids: List[str]) -> Dict[str, Any]:
    """
    Validates whether the shipment IDs are assigned to this carrier.
    Returns categorized lists of authorized, unauthorized, and unknown shipment IDs.
    """
    carrier_name = (carrier.get("carrier") or "").strip().lower()

    authorized_shipments = []
    unauthorized_shipments = []
    unknown_ids = []

    for sid in shipment_ids:
        ctx = get_shipment_context(sid)
        if not ctx:
            unknown_ids.append(sid)
            continue

        shipment = ctx["shipment"]
        shipment_carrier = (shipment.get("carrier") or "").strip().lower()

        # Check carrier match
        if shipment_carrier == carrier_name:
            authorized_shipments.append(shipment)
        else:
            unauthorized_shipments.append({
                "shipment_id": sid,
                "assigned_carrier": shipment.get("carrier")
            })

    is_authorized = len(authorized_shipments) > 0 or (len(shipment_ids) == 0)

    return {
        "is_authorized": is_authorized,
        "has_unauthorized": len(unauthorized_shipments) > 0,
        "authorized_ids": [s["shipment_id"] for s in authorized_shipments],
        "authorized_shipments": authorized_shipments,
        "unauthorized_ids": [s["shipment_id"] for s in unauthorized_shipments],
        "unauthorized_shipments": unauthorized_shipments,
        "unknown_ids": unknown_ids
    }

def get_policy_rules() -> List[Dict[str, Any]]:
    """Returns all Northwind freight policy rules."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM policy").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def update_shipment_notes(shipment_id: str, new_note: str) -> bool:
    """Updates or appends operational carrier notes to a shipment record."""
    clean_id = shipment_id.strip().upper()
    conn = get_connection()
    try:
        cur = conn.cursor()
        existing = cur.execute("SELECT notes FROM shipments WHERE UPPER(shipment_id) = ?", (clean_id,)).fetchone()
        if existing is None:
            return False
        old_note = existing[0] or ""
        updated_note = f"{old_note}; {new_note}".strip("; ") if old_note else new_note
        cur.execute("UPDATE shipments SET notes = ? WHERE UPPER(shipment_id) = ?", (updated_note, clean_id))
        conn.commit()
        return True
    finally:
        conn.close()



