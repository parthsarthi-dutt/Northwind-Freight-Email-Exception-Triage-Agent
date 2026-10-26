"""
Helper script to simulate an incoming mail webhook with the user's sample payload.
Usage:
  python send_sample_webhook.py [http://127.0.0.1:5000/webhook/email]
"""
import sys
import json
import urllib.request
import urllib.error

from datetime import datetime, timezone

DEFAULT_URL = "http://127.0.0.1:5000/webhook/email"

SAMPLE_EMAIL = {
    "from": {
        "name": "Marcus Webb",
        "email": "marcus.webb@mailbox.example"
    },
    "to": "ops@northwind-freight.example",
    "subject": "Parcel NWF-20101 is late",
    "body_text": "Hi, my parcel NWF-20101 was due two days ago and the tracking has not moved since Friday. Can you tell me where it is?\n\nThanks,\nMarcus Webb",
    "received_at": datetime.now(timezone.utc).isoformat(),
    "headers": {},
    "attachments": []
}

def main():
    target_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"--> Sending sample email webhook to {target_url}...")
    
    data_bytes = json.dumps(SAMPLE_EMAIL).encode('utf-8')
    req = urllib.request.Request(
        target_url,
        data=data_bytes,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as resp:
            status_code = resp.getcode()
            response_body = resp.read().decode('utf-8')
            print(f"<-- Response {status_code}:")
            print(json.dumps(json.loads(response_body), indent=2))
    except urllib.error.HTTPError as e:
        print(f"Error HTTP {e.code}: {e.read().decode('utf-8')}")
    except Exception as e:
        print(f"Connection failed: {e}")
        print("Note: Ensure the Flask server is running first using: python run.py")

if __name__ == '__main__':
    main()
