import os
import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import re

from app.models import save_email, get_email_by_id
from app.logger import log_email_received
from app.redis_stream import publish_email_to_stream

class GmailReaderError(Exception):
    """Raised when there is an issue connecting or fetching from Gmail."""
    pass

def decode_mime_header(header_value: Optional[str]) -> str:
    """Decodes RFC 2047 encoded MIME headers into a clean unicode string."""
    if not header_value:
        return ""
    decoded_fragments = []
    for fragment, encoding in decode_header(header_value):
        if isinstance(fragment, bytes):
            try:
                decoded_fragments.append(fragment.decode(encoding or 'utf-8', errors='replace'))
            except LookupError:
                decoded_fragments.append(fragment.decode('utf-8', errors='replace'))
        else:
            decoded_fragments.append(str(fragment))
    return "".join(decoded_fragments).strip()

def html_to_plain_text(html_content: str) -> str:
    """Basic fallback converter from HTML to clean plain text."""
    # Remove script and style elements
    clean_html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    # Replace line breaks and paragraph tags with newlines
    clean_html = re.sub(r'<(br|/p|/div|/tr)>', '\n', clean_html, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r'<[^>]+>', '', clean_html)
    # Normalize multiple whitespace/newlines
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)

def extract_body_and_attachments(msg: email.message.Message) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Extracts plain text body and attachment metadata from a MIME message.
    """
    body_text = ""
    html_fallback = ""
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()

            if filename:
                filename = decode_mime_header(filename)

            # Check if this part is an attachment
            if "attachment" in content_disposition.lower() or filename:
                payload = part.get_payload(decode=True)
                size = len(payload) if payload else 0
                attachments.append({
                    "filename": filename or "attachment",
                    "content_type": content_type,
                    "size_bytes": size
                })
                continue

            # Check if text body
            if content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        body_text = payload.decode(charset, errors='replace')
                    except LookupError:
                        body_text = payload.decode('utf-8', errors='replace')

            elif content_type == "text/html" and not html_fallback:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        html_fallback = payload.decode(charset, errors='replace')
                    except LookupError:
                        html_fallback = payload.decode('utf-8', errors='replace')

        if not body_text and html_fallback:
            body_text = html_to_plain_text(html_fallback)

    else:
        # Non-multipart message
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            try:
                decoded_payload = payload.decode(charset, errors='replace')
            except LookupError:
                decoded_payload = payload.decode('utf-8', errors='replace')

            if content_type == "text/html":
                body_text = html_to_plain_text(decoded_payload)
            else:
                body_text = decoded_payload

    return body_text.strip(), attachments

class GmailReaderAgent:
    """
    Agent that connects to Gmail via IMAP over SSL,
    fetches real emails, parses them into the standardized format,
    and stores them into SQLite with status='pending'.
    """

    def __init__(self, username: Optional[str] = None, password: Optional[str] = None, imap_host: str = "imap.gmail.com", imap_port: int = 993):
        self.username = username if username is not None else os.environ.get("GMAIL_EMAIL")
        self.password = password if password is not None else os.environ.get("GMAIL_APP_PASSWORD")
        self.imap_host = imap_host
        self.imap_port = imap_port

    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.is_configured():
            raise GmailReaderError("GMAIL_EMAIL and GMAIL_APP_PASSWORD are required. Please configure them in your .env file.")
        try:
            # Clean password from spaces if user copied it formatted as "xxxx xxxx xxxx xxxx"
            cleaned_pwd = self.password.replace(" ", "")
            mail = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            mail.login(self.username, cleaned_pwd)
            return mail
        except imaplib.IMAP4.error as e:
            raise GmailReaderError(f"Failed to authenticate with Gmail IMAP: {e}")
        except Exception as e:
            raise GmailReaderError(f"Connection error to {self.imap_host}: {e}")

    def test_connection(self) -> Dict[str, Any]:
        """Validates credentials by attempting an IMAP login."""
        mail = self._connect()
        try:
            status, _ = mail.select("INBOX", readonly=True)
            return {
                "success": status == "OK",
                "email": self.username,
                "message": "Connected to Gmail successfully."
            }
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    def fetch_and_store_emails(self, search_criteria: str = "UNSEEN", max_count: int = 20, mark_as_read: bool = True) -> List[Dict[str, Any]]:
        """
        Fetches matching emails from Gmail INBOX, parses them into structured records,
        saves them into SQLite with status='pending', and returns the saved records.
        """
        mail = self._connect()
        saved_emails = []

        try:
            status, _ = mail.select("INBOX", readonly=not mark_as_read)
            if status != "OK":
                raise GmailReaderError("Could not access INBOX folder in Gmail.")

            status, data = mail.search(None, search_criteria)
            if status != "OK" or not data or not data[0]:
                return []

            email_ids = data[0].split()
            # Most recent first
            email_ids = email_ids[::-1][:max_count]

            for e_id in email_ids:
                status, msg_data = mail.fetch(e_id, "(RFC822)")
                if status != "OK" or not msg_data:
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        raw_email = response_part[1]
                        msg = email.message_from_bytes(raw_email)

                        parsed_email = self._parse_mime_message(msg)
                        # Save to SQLite with status='pending'
                        record_id = save_email(parsed_email)
                        saved_record = get_email_by_id(record_id)
                        if saved_record:
                            saved_emails.append(saved_record)
                            log_email_received(saved_record, source="Gmail IMAP")
                            publish_email_to_stream(saved_record)

                        if mark_as_read:
                            mail.store(e_id, '+FLAGS', '\\Seen')

            return saved_emails

        finally:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass

    def _parse_mime_message(self, msg: email.message.Message) -> Dict[str, Any]:
        """Parses a Python email.message.Message into our standardized dictionary."""
        # 1. From
        raw_from = msg.get("From", "")
        name, from_email = parseaddr(raw_from)
        name = decode_mime_header(name)

        # 2. To
        raw_to = msg.get("To", "")
        _, to_address = parseaddr(raw_to)
        if not to_address:
            to_address = raw_to or self.username or ""

        # 3. Subject
        subject = decode_mime_header(msg.get("Subject", ""))

        # 4. Received timestamp
        date_header = msg.get("Date")
        if date_header:
            try:
                dt = parsedate_to_datetime(date_header)
                received_at = dt.isoformat()
            except Exception:
                received_at = datetime.now(timezone.utc).isoformat()
        else:
            received_at = datetime.now(timezone.utc).isoformat()

        # 5. Body & Attachments
        body_text, attachments = extract_body_and_attachments(msg)

        # 6. Headers dictionary
        headers = {}
        for h in ["Message-ID", "In-Reply-To", "References", "Date", "Reply-To"]:
            val = msg.get(h)
            if val:
                headers[h] = decode_mime_header(val)

        return {
            "from_name": name,
            "from_email": from_email,
            "to_address": to_address,
            "subject": subject,
            "body_text": body_text,
            "received_at": received_at,
            "headers": headers,
            "attachments": attachments,
            "status": "pending"
        }
