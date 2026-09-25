import os
import sqlite3
import logging
from contextlib import contextmanager
from typing import Generator
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def get_db_path() -> str:
    return os.getenv("DB_PATH", "bot_database.db")


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_db_cursor() -> Generator[sqlite3.Cursor, None, None]:
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[DB Error]: {e}", exc_info=True)
        raise
    finally:
        conn.close()