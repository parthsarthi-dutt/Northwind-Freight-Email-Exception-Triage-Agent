import os
import logging
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE_PATH = os.path.join(LOG_DIR, "emails.log")

def setup_email_logger() -> logging.Logger:
    """Configures rotating file and stream logger for email events."""
    logger = logging.getLogger("shipsy.email")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # Rotating file handler (10 MB, up to 5 backups)
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger

email_logger = setup_email_logger()

def log_email_received(email_data: Dict[str, Any], source: str = "Webhook"):
    """
    Logs comprehensive details of an incoming email when it is received and saved.
    """
    sender = email_data.get("from", {})
    if isinstance(sender, dict):
        sender_str = f"{sender.get('name', '')} <{sender.get('email', '')}>".strip()
    else:
        sender_str = str(sender)

    subject = email_data.get("subject", "(No Subject)")
    recipient = email_data.get("to", "(No Recipient)")
    email_id = email_data.get("id", "N/A")
    status = email_data.get("status", "pending")
    received_at = email_data.get("received_at", "")
    attachments = email_data.get("attachments", [])
    body_text = email_data.get("body_text", "")
    preview = body_text.strip().replace("\n", " ")[:120]

    log_message = (
        f"[{source.upper()}] Email Ingested Successfully\n"
        f"  ├── ID:          {email_id}\n"
        f"  ├── Status:      {status}\n"
        f"  ├── From:        {sender_str}\n"
        f"  ├── To:          {recipient}\n"
        f"  ├── Subject:     {subject}\n"
        f"  ├── Received At: {received_at}\n"
        f"  ├── Attachments: {len(attachments)}\n"
        f"  └── Body:        {preview}..."
    )
    email_logger.info(log_message)
