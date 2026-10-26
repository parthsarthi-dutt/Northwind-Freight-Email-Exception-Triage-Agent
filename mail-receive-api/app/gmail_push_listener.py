import os
import sys
import time
import email
import threading
import logging
from typing import Optional, Dict, Any, Callable
import requests
from imapclient import IMAPClient

from app.models import save_email, get_email_by_id
from app.gmail_reader import GmailReaderAgent
from app.logger import log_email_received
from app.redis_stream import publish_email_to_stream

logger = logging.getLogger("gmail_push_listener")

class GmailPushListener:
    """
    IMAP IDLE Push Listener for Gmail.
    Maintains an open socket to imap.gmail.com without polling.
    Receives instant push notifications (RFC 2177 IDLE) from Gmail the moment an email arrives,
    parses it into structured format, stores in SQLite with status='pending',
    and optionally invokes the webhook URL.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        webhook_url: Optional[str] = None,
        on_email_received: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        self.username = username if username is not None else os.environ.get("GMAIL_EMAIL")
        self.password = password if password is not None else os.environ.get("GMAIL_APP_PASSWORD")
        self.webhook_url = webhook_url or os.environ.get("WEBHOOK_LOCAL_URL", "http://127.0.0.1:5000/webhook/email")
        self.on_email_received = on_email_received

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.is_running = False
        self.last_event_time: Optional[float] = None
        self.processed_count = 0
        self.last_error: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def start(self, daemon: bool = True):
        """Starts the push listener in a background thread."""
        if not self.is_configured():
            raise ValueError("Gmail credentials (GMAIL_EMAIL, GMAIL_APP_PASSWORD) not configured.")
        if self.is_running:
            return

        self._stop_event.clear()
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=daemon)
        self._thread.start()
        logger.info(f"Gmail Push Listener started for {self.username}")

    def stop(self, timeout: float = 5.0):
        """Stops the push listener."""
        self._stop_event.set()
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("Gmail Push Listener stopped")

    def get_status(self) -> Dict[str, Any]:
        """Returns the current state of the listener."""
        return {
            "running": self.is_running,
            "configured": self.is_configured(),
            "username": self.username,
            "webhook_forward_url": self.webhook_url,
            "processed_count": self.processed_count,
            "last_event_time": self.last_event_time,
            "last_error": self.last_error
        }

    def _run_loop(self):
        cleaned_pwd = (self.password or "").replace(" ", "")
        reader_helper = GmailReaderAgent(self.username, cleaned_pwd)

        while not self._stop_event.is_set():
            client = None
            try:
                client = IMAPClient('imap.gmail.com', ssl=True)
                client.login(self.username, cleaned_pwd)
                client.select_folder('INBOX')
                logger.info("[Push Listener] Connected to Gmail IMAP. Checking for initial unread emails...")

                # Process any existing unseen emails on connect
                self._fetch_and_process_unseen(client, reader_helper)

                logger.info("[Push Listener] Entering IMAP IDLE (waiting for incoming emails without polling)...")

                while not self._stop_event.is_set():
                    client.idle()
                    # Block waiting for push response or timeout (10 minutes refresh)
                    responses = client.idle_check(timeout=600)
                    client.idle_done()

                    if self._stop_event.is_set():
                        break

                    has_new = False
                    if responses:
                        self.last_event_time = time.time()
                        for resp in responses:
                            # Untagged response like (b'EXISTS', count) or (count, b'EXISTS') or (count, b'RECENT')
                            resp_str = str(resp).upper()
                            if "EXISTS" in resp_str or "RECENT" in resp_str:
                                has_new = True
                                break

                    if has_new:
                        logger.info(f"[Push Listener] Gmail pushed new email event: {responses}")
                        self._fetch_and_process_unseen(client, reader_helper)

            except Exception as e:
                self.last_error = str(e)
                logger.error(f"[Push Listener] Error: {e}")
                # Wait briefly before reconnecting to prevent thrashing
                if not self._stop_event.is_set():
                    time.sleep(5)
            finally:
                if client:
                    try:
                        client.logout()
                    except Exception:
                        pass

        self.is_running = False

    def _fetch_and_process_unseen(self, client: IMAPClient, reader_helper: GmailReaderAgent):
        """Fetches unseen messages, parses them, saves to SQLite with status='pending', and triggers webhook."""
        try:
            unseen_ids = client.search(['UNSEEN'])
            if not unseen_ids:
                return

            logger.info(f"[Push Listener] Processing {len(unseen_ids)} unread message(s)...")
            fetch_res = client.fetch(unseen_ids, ['RFC822', 'FLAGS'])

            for msg_id, data in fetch_res.items():
                raw_bytes = data.get(b'RFC822')
                if not raw_bytes:
                    continue

                msg = email.message_from_bytes(raw_bytes)
                parsed = reader_helper._parse_mime_message(msg)

                # Format payload matching webhook schema
                payload = {
                    "from": {
                        "name": parsed["from_name"],
                        "email": parsed["from_email"]
                    },
                    "to": parsed["to_address"],
                    "subject": parsed["subject"],
                    "body_text": parsed["body_text"],
                    "received_at": parsed["received_at"],
                    "headers": parsed["headers"],
                    "attachments": parsed["attachments"]
                }

                # Save directly or invoke webhook endpoint
                saved_id = None
                webhook_triggered = False

                if self.webhook_url:
                    try:
                        # Push to local webhook endpoint
                        resp = requests.post(self.webhook_url, json=payload, timeout=5)
                        if resp.status_code in (200, 201):
                            webhook_triggered = True
                            resp_data = resp.json()
                            if "email" in resp_data and "id" in resp_data["email"]:
                                saved_id = resp_data["email"]["id"]
                    except Exception as err:
                        logger.warning(f"[Push Listener] Webhook POST to {self.webhook_url} failed ({err}), falling back to direct DB storage.")

                if not webhook_triggered:
                    # Save directly to DB if webhook was unreachable
                    saved_id = save_email(parsed)
                    record = get_email_by_id(saved_id)
                    if record:
                        log_email_received(record, source="Gmail Push")
                        publish_email_to_stream(record)

                # Mark as read in Gmail
                client.add_flags(msg_id, ['\\Seen'])
                self.processed_count += 1

                logger.info(f"[Push Listener] Email received & saved with status='pending'! ID: {saved_id}, Subject: '{parsed['subject']}', From: {parsed['from_email']}")

                if self.on_email_received:
                    record = get_email_by_id(saved_id) if saved_id else payload
                    self.on_email_received(record)

        except Exception as e:
            logger.error(f"[Push Listener] Error fetching/processing unread messages: {e}")
            self.last_error = str(e)

# Global singleton listener for app
_global_listener: Optional[GmailPushListener] = None

def get_push_listener() -> GmailPushListener:
    global _global_listener
    if _global_listener is None:
        _global_listener = GmailPushListener()
    return _global_listener
