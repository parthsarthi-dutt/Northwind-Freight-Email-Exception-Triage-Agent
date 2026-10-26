"""
Agentic Stream Worker Microservice
-----------------------------------
Runs continuously in parallel to the Email Ingestion Service.
Listens to the Redis Stream, pops incoming email messages,
and passes the data (excluding status) to the Agentic Workflow.

Usage:
  python worker.py [--stream email:stream] [--group agentic_workflow_group] [--consumer worker_1]
"""
import os
import sys
import json
import time
import signal
import logging
import argparse
from typing import Dict, Any, Optional
import redis

# Ensure directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from agent import EmailAgent

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [Worker] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("agentic_worker")

def deserialize_stream_message(fields: Dict[str, str]) -> Dict[str, Any]:
    """
    Deserializes raw Redis Stream string fields into Python types.
    Ensures that 'status' is NOT in the payload.
    """
    assert "status" not in fields, "Stream message must NOT contain 'status'!"

    return {
        "id": int(fields.get("id", 0)),
        "from": json.loads(fields.get("from", "{}")),
        "to": fields.get("to", ""),
        "subject": fields.get("subject", ""),
        "body_text": fields.get("body_text", ""),
        "received_at": fields.get("received_at", ""),
        "headers": json.loads(fields.get("headers", "{}")),
        "attachments": json.loads(fields.get("attachments", "[]")),
        "created_at": fields.get("created_at", "")
    }

class RedisStreamWorker:
    def __init__(
        self,
        redis_url: Optional[str] = None,
        stream_name: Optional[str] = None,
        group_name: Optional[str] = None,
        consumer_name: Optional[str] = None,
        agent: Optional[EmailAgent] = None
    ):
        self.redis_url = redis_url or Config.REDIS_URL
        self.stream_name = stream_name or Config.REDIS_STREAM_NAME
        self.group_name = group_name or Config.REDIS_CONSUMER_GROUP
        self.consumer_name = consumer_name or Config.REDIS_CONSUMER_NAME
        self.agent = agent or EmailAgent()
        self.pending_timeout_ms = Config.PENDING_TIMEOUT_MS  # 10 minutes (600,000 ms)

        self.r = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self.is_running = False

    def init_group(self):
        """Ensures the consumer group and stream exist."""
        try:
            self.r.xgroup_create(self.stream_name, self.group_name, id="$", mkstream=True)
            logger.info(f"Created consumer group '{self.group_name}' on stream '{self.stream_name}'")
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.info(f"Consumer group '{self.group_name}' already exists.")
            else:
                raise

    def claim_dead_worker_pending_messages(self):
        """
        Scans Pending Entries List (PEL) for messages that have been idle
        for >= 10 minutes (600,000 ms) due to an unresponsive/dead prior worker,
        claims and reassigns them to the current consumer for processing.
        """
        try:
            pending = self.r.xpending_range(
                self.stream_name,
                self.group_name,
                min="-",
                max="+",
                count=20
            )
            claimed_messages = []
            for item in pending:
                msg_id = item.get("message_id")
                idle = item.get("idle", item.get("time_since_delivered", 0))
                consumer = item.get("consumer", "unknown")

                # If idle for >= 10 minutes (600,000 ms), reassign from dead worker
                if idle >= self.pending_timeout_ms and msg_id:
                    logger.warning(
                        f"⚠️ [DEAD WORKER REASSIGNMENT] Message {msg_id} idle for {idle}ms "
                        f"(>= {self.pending_timeout_ms}ms) from worker '{consumer}'. "
                        f"Reassigning to '{self.consumer_name}'..."
                    )
                    claimed = self.r.xclaim(
                        self.stream_name,
                        self.group_name,
                        self.consumer_name,
                        min_idle_time=self.pending_timeout_ms,
                        message_ids=[msg_id]
                    )
                    if claimed:
                        claimed_messages.extend(claimed)
            return claimed_messages
        except Exception as e:
            logger.error(f"Error checking dead worker pending messages: {e}")
            return []

    def process_and_ack_message(self, msg_id: str, fields: Dict[str, str]):
        """
        Deserializes, processes through agent workflow, dispatches reviewer email,
        acknowledges (XACK), and pops/deletes (XDEL) the message from Redis Stream.
        """
        logger.info(f"[POP] Received message {msg_id} from stream '{self.stream_name}'")
        email_payload = deserialize_stream_message(fields)

        try:
            # 1. Execute agent workflow (generates draft, mails reviewer, updates status in DB to 'under_review')
            result = self.agent.process(email_payload)

            # 2. Acknowledge message in Redis Stream
            self.r.xack(self.stream_name, self.group_name, msg_id)

            # 3. Pop / Remove message from Redis Stream
            self.r.xdel(self.stream_name, msg_id)
            logger.info(f"✅ [ACK & POPPED] Message {msg_id} acknowledged and removed from Redis Stream.")
            return result
        except Exception as wf_err:
            logger.error(f"❌ Error in agent workflow for message {msg_id}: {wf_err}")
            raise

    def start(self):
        """Main loop popping messages, claiming dead worker timeouts, and executing agent workflow."""
        self.init_group()
        self.is_running = True

        logger.info("=" * 70)
        logger.info(f"🚀 AGENTIC WORKER RUNNING")
        logger.info(f"   Stream:          {self.stream_name}")
        logger.info(f"   Group:           {self.group_name}")
        logger.info(f"   Consumer:        {self.consumer_name}")
        logger.info(f"   Ingestion API:   {Config.INGESTION_API_URL}")
        logger.info(f"   Pending Timeout: {self.pending_timeout_ms}ms (10 minutes)")
        logger.info("=" * 70)

        while self.is_running:
            try:
                # 1. Check & reassign messages idle > 10m from dead workers
                dead_entries = self.claim_dead_worker_pending_messages()
                for msg_id, fields in dead_entries:
                    logger.info(f"[CLAIMED] Processing reassigned message {msg_id} from dead worker.")
                    self.process_and_ack_message(msg_id, fields)

                # 2. XREADGROUP: block up to 2000ms for unread messages (">")
                entries = self.r.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={self.stream_name: ">"},
                    count=1,
                    block=2000
                )

                if not entries:
                    continue

                for stream_key, messages in entries:
                    for msg_id, fields in messages:
                        self.process_and_ack_message(msg_id, fields)

            except redis.exceptions.ConnectionError as ce:
                logger.warning(f"Redis connection error: {ce}. Retrying in 5 seconds...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                time.sleep(1)

    def stop(self):
        self.is_running = False
        logger.info("Worker stopped.")

def main():
    parser = argparse.ArgumentParser(description="Agentic Stream Worker")
    parser.add_argument("--stream", default=Config.REDIS_STREAM_NAME, help="Stream name")
    parser.add_argument("--group", default=Config.REDIS_CONSUMER_GROUP, help="Consumer group name")
    parser.add_argument("--consumer", default=Config.REDIS_CONSUMER_NAME, help="Consumer name")
    args = parser.parse_args()

    worker = RedisStreamWorker(
        stream_name=args.stream,
        group_name=args.group,
        consumer_name=args.consumer
    )

    def handle_signal(sig, frame):
        worker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        worker.start()
    except KeyboardInterrupt:
        worker.stop()

if __name__ == '__main__':
    main()
