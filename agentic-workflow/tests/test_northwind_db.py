import pytest
from northwind_db import (
    get_shipment_context,
    get_account_by_email,
    get_carrier_by_email,
    verify_sender_email,
    get_policy_rules
)

def test_verify_customer_email_case_insensitive():
    # Mixed-case customer email
    res = verify_sender_email("Marcus.Webb@Mailbox.Example")
    assert res["verified"] is True
    assert res["sender_type"] == "customer"
    assert res["email"] == "marcus.webb@mailbox.example"
    assert res["account"] is not None
    assert res["account"]["contact"] == "Marcus Webb"
    assert res["account"]["tier"] == "Standard"

def test_verify_carrier_email_case_insensitive():
    # Mixed-case carrier ops email
    res = verify_sender_email("Ops@Swiftline.Example")
    assert res["verified"] is True
    assert res["sender_type"] == "carrier"
    assert res["email"] == "ops@swiftline.example"
    assert res["carrier"] is not None
    assert res["carrier"]["carrier"] == "Swiftline"

def test_verify_carrier_domain_match():
    # Any email from a registered carrier's domain
    res = verify_sender_email("dispatch-alerts@blueroute.example")
    assert res["verified"] is True
    assert res["sender_type"] == "carrier"
    assert res["carrier"]["carrier"] == "BlueRoute"

def test_verify_unverified_unknown_email():
    res = verify_sender_email("stranger@unknown-domain.com")
    assert res["verified"] is False
    assert res["sender_type"] == "unverified"
    assert "not verified or does not exist" in res["message"]
    assert res["account"] is None
    assert res["carrier"] is None

def test_lookup_shipment_context():
    ctx = get_shipment_context("NWF-20101")
    assert ctx is not None
    assert ctx["shipment"]["customer"] == "Marcus Webb"
    assert ctx["shipment"]["carrier"] == "Swiftline"
    assert ctx["account"] is not None
    assert ctx["account"]["contact"] == "Marcus Webb"
    assert len(ctx["events"]) > 0

def test_lookup_policy_rules():
    rules = get_policy_rules()
    assert len(rules) == 18

def test_validate_customer_shipment_authorization():
    from northwind_db import validate_customer_shipment_authorization
    # Marcus Webb (Account 8801.0)
    marcus_account = {"account_id": "8801.0", "contact": "Marcus Webb"}
    
    # Own shipment NWF-20101
    auth_valid = validate_customer_shipment_authorization(marcus_account, ["NWF-20101"])
    assert auth_valid["is_authorized"] is True
    assert auth_valid["authorized_ids"] == ["NWF-20101"]
    assert auth_valid["unauthorized_ids"] == []

    # Other customer's shipment NWF-20102 (Elena Rossi)
    auth_invalid = validate_customer_shipment_authorization(marcus_account, ["NWF-20102"])
    assert auth_invalid["authorized_ids"] == []
    assert auth_invalid["unauthorized_ids"] == ["NWF-20102"]

def test_validate_carrier_shipment_authorization():
    from northwind_db import validate_carrier_shipment_authorization
    swiftline = {"carrier": "Swiftline"}

    # NWF-20101 is Swiftline, NWF-20102 is BlueRoute
    res = validate_carrier_shipment_authorization(swiftline, ["NWF-20101", "NWF-20102"])
    assert res["authorized_ids"] == ["NWF-20101"]
    assert res["unauthorized_ids"] == ["NWF-20102"]

