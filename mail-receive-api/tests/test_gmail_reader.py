import email
from email.message import EmailMessage
import pytest
from app.gmail_reader import GmailReaderAgent, decode_mime_header, html_to_plain_text
from app.models import get_email_by_id

def test_decode_mime_header():
    # Plain text
    assert decode_mime_header("Simple Subject") == "Simple Subject"
    # Encoded utf-8 header: =?utf-8?B?VGVzdCDwn5Ka?= ("Test 💚")
    encoded = "=?utf-8?B?VGVzdCDwn5Ka?="
    assert decode_mime_header(encoded) == "Test 💚"

def test_html_to_plain_text():
    html = "<div><p>Hello World!</p><br><p>Second paragraph</p></div>"
    text = html_to_plain_text(html)
    assert "Hello World!" in text
    assert "Second paragraph" in text

def test_parse_mime_message_multipart(app):
    with app.app_context():
        # Construct realistic MIME message
        msg = EmailMessage()
        msg['Subject'] = "Real Order Update #12345"
        msg['From'] = "Real Customer <customer@example.com>"
        msg['To'] = "ops@northwind-freight.example"
        msg['Date'] = "Mon, 20 Oct 2026 10:15:00 +0000"
        msg['Message-ID'] = "<abc-123@mail.gmail.com>"
        msg.set_content("Hello team,\nWhere is my order?\n\nRegards,\nCustomer")
        
        # Add an attachment
        msg.add_attachment(b"dummy pdf content", maintype="application", subtype="pdf", filename="invoice.pdf")

        agent = GmailReaderAgent()
        parsed = agent._parse_mime_message(msg)

        assert parsed["from_name"] == "Real Customer"
        assert parsed["from_email"] == "customer@example.com"
        assert parsed["to_address"] == "ops@northwind-freight.example"
        assert parsed["subject"] == "Real Order Update #12345"
        assert "Where is my order?" in parsed["body_text"]
        assert parsed["status"] == "pending"
        assert len(parsed["attachments"]) == 1
        assert parsed["attachments"][0]["filename"] == "invoice.pdf"
        assert parsed["attachments"][0]["size_bytes"] == len(b"dummy pdf content")
