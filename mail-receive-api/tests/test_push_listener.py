import pytest
from app.gmail_push_listener import GmailPushListener

def test_push_listener_unconfigured():
    listener = GmailPushListener(username="", password="")
    assert not listener.is_configured()
    status = listener.get_status()
    assert status["running"] is False
    assert status["configured"] is False

def test_push_listener_routes(client, monkeypatch):
    from app.gmail_push_listener import GmailPushListener
    import app.routes

    unconfigured_listener = GmailPushListener(username="", password="")
    monkeypatch.setattr(app.routes, "get_push_listener", lambda: unconfigured_listener)

    # Status endpoint when not configured
    resp = client.get('/api/gmail/push/status')
    assert resp.status_code == 200
    assert resp.json["running"] is False

    # Start when unconfigured fails with 400
    start_resp = client.post('/api/gmail/push/start')
    assert start_resp.status_code == 400
    assert "not configured" in start_resp.json["message"]

    # Stop when not running returns 200
    stop_resp = client.post('/api/gmail/push/stop')
    assert stop_resp.status_code == 200
