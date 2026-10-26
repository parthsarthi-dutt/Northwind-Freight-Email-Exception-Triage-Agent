"""
Real Gmail Real-Time Push Agent (IMAP IDLE)

Listens for incoming emails pushed directly from Gmail's servers in real-time
without any polling loops. When an email is pushed:
  1. Extracts structured fields (from, to, subject, body_text, received_at, headers, attachments)
  2. Ingests into SQLite database with status='pending'
  3. Dispatches payload to the webhook endpoint (POST /webhook/email)

Usage:
  python run_gmail_push_agent.py
"""
import os
import sys
import time
import signal
import logging
from dotenv import load_dotenv

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.gmail_push_listener import GmailPushListener

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def on_new_email(email_record):
    print("\n" + "="*70)
    print("🔔 [PUSH NOTIFICATION] New email received from Gmail!")
    print(f"   ID:          {email_record.get('id')}")
    print(f"   Status:      {email_record.get('status')}")
    print(f"   From:        {email_record.get('from', {}).get('name')} <{email_record.get('from', {}).get('email')}>")
    print(f"   To:          {email_record.get('to')}")
    print(f"   Subject:     {email_record.get('subject')}")
    print(f"   Received At: {email_record.get('received_at')}")
    body_snippet = (email_record.get('body_text') or '').strip().replace('\n', ' ')[:80]
    print(f"   Body:        {body_snippet}...")
    print("="*70 + "\n")

def main():
    load_dotenv()
    app = create_app()

    webhook_url = os.environ.get("WEBHOOK_LOCAL_URL", "http://127.0.0.1:5000/webhook/email")
    listener = GmailPushListener(
        webhook_url=webhook_url,
        on_email_received=on_new_email
    )

    if not listener.is_configured():
        print("[!] ERROR: Gmail credentials not configured.")
        print("    Please set GMAIL_EMAIL and GMAIL_APP_PASSWORD in your .env file.")
        print("    Example:")
        print("      GMAIL_EMAIL=you@gmail.com")
        print("      GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx")
        sys.exit(1)

    print("=" * 70)
    print("🚀 GMAIL REAL-TIME PUSH AGENT (IMAP IDLE - ZERO POLLING)")
    print(f"   Account:        {listener.username}")
    print(f"   Webhook Target: {webhook_url}")
    print("   Mode:           RFC 2177 Push Notifications (Idle Socket)")
    print("=" * 70)

    # Start push listener thread
    with app.app_context():
        listener.start(daemon=False)

        def handle_shutdown(signum, frame):
            print("\nShutting down Gmail Push Listener...")
            listener.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

        try:
            while listener.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            handle_shutdown(None, None)

if __name__ == '__main__':
    main()
