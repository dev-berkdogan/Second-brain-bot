import os
import logging
from datetime import datetime, timezone
from typing import Optional
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_supabase_client: Optional[Client] = None


def get_current_utc_iso() -> str:
    """Tüm repository zaman damgaları için standart UTC ISO-8601 string döner."""
    return datetime.now(timezone.utc).isoformat()


def get_supabase_client() -> Client:
    """Tekil ve doğrulanmış Supabase client örneği döndürür."""
    global _supabase_client

    if _supabase_client is not None:
        return _supabase_client

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_secret_key = os.getenv("SUPABASE_SECRET_KEY")

    if not supabase_url:
        raise ValueError("Missing environment variable: 'SUPABASE_URL'.")

    if not supabase_secret_key:
        raise ValueError("Missing environment variable: 'SUPABASE_SECRET_KEY'.")

    try:
        _supabase_client = create_client(
            supabase_url,
            supabase_secret_key
        )
        return _supabase_client

    except Exception as e:
        logger.error(
            f"Failed to initialize Supabase client: {e}"
        )
        raise