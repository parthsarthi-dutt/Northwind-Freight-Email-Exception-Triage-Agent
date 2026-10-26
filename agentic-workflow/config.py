import os
from pathlib import Path
from dotenv import load_dotenv

# Load from local directory and workspace root
load_dotenv()
load_dotenv(Path(__file__).parent.parent / ".env")

class Config:
    """Agentic Workflow Configuration."""
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    REDIS_STREAM_NAME = os.environ.get("REDIS_STREAM_NAME", "email:stream")
    REDIS_CONSUMER_GROUP = os.environ.get("REDIS_CONSUMER_GROUP", "agentic_workflow_group")
    REDIS_CONSUMER_NAME = os.environ.get("REDIS_CONSUMER_NAME", f"agent_worker_{os.getpid()}")
    
    # Ingestion microservice API for updating email status
    INGESTION_API_URL = os.environ.get("INGESTION_API_URL", "http://127.0.0.1:5000")
    
    # Groq Model Configuration
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
    GROQ_API_URL = os.environ.get("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
    GROQ_DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    GROQ_ALLOWED_MODELS = [
        "openai/gpt-oss-120b",
        "qwen/qwen3.8-27b",
        "openai/gpt-oss-20b"
    ]
    
    AGENT_NAME = os.environ.get("AGENT_NAME", "FreightOps-Agent")
    
    # Reviewer Mail & SMTP Configuration
    GMAIL_EMAIL = os.environ.get("GMAIL_EMAIL", "pdtempmail1@gmail.com")
    GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
    REVIEWER_EMAIL = os.environ.get("REVIEWER_EMAIL", os.environ.get("GMAIL_EMAIL", "reviewer@northwind-freight.example"))
    SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
    
    # 10 minutes timeout in ms for reassigning dead worker messages in Redis PEL
    PENDING_TIMEOUT_MS = int(os.environ.get("PENDING_TIMEOUT_MS", 600000))
