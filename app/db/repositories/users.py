import logging
from typing import Optional, Dict, Any, List

from postgrest.exceptions import APIError

from app.db.supabase_client import (
    get_supabase_client,
    get_current_utc_iso,
)

logger = logging.getLogger(__name__)


class UserRepository:

    @staticmethod
    def get_user(user_id: str) -> Optional[Dict[str, Any]]:
        client = get_supabase_client()

        try:
            response = (
                client
                .table("users")
                .select("*")
                .eq("id", user_id)
                .execute()
            )

            return response.data[0] if response.data else None

        except APIError as e:
            logger.error(
                f"[UserRepository.get_user] APIError for user {user_id}: {e.message}"
            )
            raise

    @staticmethod
    def create_user(
        user_id: Optional[str] = None,
        preferred_language: str = "English",
        auth_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload: Dict[str, Any] = {
            "preferred_language": preferred_language,
        }

        if user_id:
            payload["id"] = user_id

        if auth_user_id:
            payload["auth_user_id"] = auth_user_id

        try:
            response = (
                client
                .table("users")
                .insert(payload)
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[UserRepository.create_user] APIError: {e.message}"
            )
            raise

    @staticmethod
    def upsert_user(
        user_id: str,
        preferred_language: str = "English",
        auth_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload: Dict[str, Any] = {
            "id": user_id,
            "preferred_language": preferred_language,
            "last_active_at": get_current_utc_iso(),
        }

        if auth_user_id:
            payload["auth_user_id"] = auth_user_id

        try:
            response = (
                client
                .table("users")
                .upsert(
                    payload,
                    on_conflict="id",
                )
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[UserRepository.upsert_user] APIError for user {user_id}: {e.message}"
            )
            raise

    @staticmethod
    def update_last_active(
        user_id: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "last_active_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("users")
                .update(payload)
                .eq("id", user_id)
                .execute()
            )

            return response.data[0] if response.data else {}

        except APIError as e:
            logger.error(
                f"[UserRepository.update_last_active] "
                f"APIError for user {user_id}: {e.message}"
            )
            raise

    @staticmethod
    def get_all_users() -> List[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("users")
                .select("*")
                .order(
                    "last_active_at",
                    desc=True,
                )
                .execute()
            )

            return response.data or []

        except APIError as e:
            logger.error(
                f"[UserRepository.get_all_users] APIError: {e.message}"
            )
            raise