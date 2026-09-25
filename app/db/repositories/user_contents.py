import logging
from typing import Optional, Dict, Any, List

from postgrest.exceptions import APIError

from app.db.supabase_client import get_supabase_client, get_current_utc_iso

logger = logging.getLogger(__name__)


class UserContentRepository:

    @staticmethod
    def save_content(
        user_id: str,
        content_id: str,
        user_language: str = "English",
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "user_id": user_id,
            "content_id": content_id,
            "user_language": user_language,
        }

        try:
            response = (
                client
                .table("user_contents")
                .upsert(
                    payload,
                    on_conflict="user_id,content_id",
                )
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[UserContentRepository.save_content] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def get_user_content(
        user_id: str,
        content_id: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("user_contents")
                .select("*")
                .eq("user_id", user_id)
                .eq("content_id", content_id)
                .execute()
            )

            return (
                response.data[0]
                if response.data
                else None
            )

        except APIError as e:
            logger.error(
                "[UserContentRepository.get_user_content] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def get_user_contents(
        user_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:

        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        client = get_supabase_client()

        try:
            response = (
                client
                .table("user_contents")
                .select("*, contents(*)")
                .eq("user_id", user_id)
                .eq("archived", False)
                .order("saved_at", desc=True)
                .limit(limit)
                .execute()
            )

            return response.data or []

        except APIError as e:
            logger.error(
                "[UserContentRepository.get_user_contents] "
                f"APIError ({user_id}): {e.message}"
            )
            raise

    @staticmethod
    def is_saved(
        user_id: str,
        content_id: str,
    ) -> bool:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("user_contents")
                .select("id")
                .eq("user_id", user_id)
                .eq("content_id", content_id)
                .execute()
            )

            return bool(response.data)

        except APIError as e:
            logger.error(
                "[UserContentRepository.is_saved] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def set_starred(
        user_id: str,
        content_id: str,
        starred: bool = True,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("user_contents")
                .update({"starred": starred})
                .eq("user_id", user_id)
                .eq("content_id", content_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Saved content not found for user '{user_id}' "
                    f"and content '{content_id}'."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[UserContentRepository.set_starred] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def increment_view_count(
        user_id: str,
        content_id: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        try:
            record = UserContentRepository.get_user_content(
                user_id,
                content_id,
            )

            if not record:
                raise ValueError(
                    f"Saved content not found for user '{user_id}' "
                    f"and content '{content_id}'."
                )

            new_views = (record.get("view_count") or 0) + 1

            response = (
                client
                .table("user_contents")
                .update(
                    {
                        "view_count": new_views,
                        "last_viewed_at": get_current_utc_iso(),
                    }
                )
                .eq("id", record["id"])
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Failed to update view count for saved content '{record['id']}'."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[UserContentRepository.increment_view_count] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def update_last_viewed(
        user_id: str,
        content_id: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "last_viewed_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("user_contents")
                .update(payload)
                .eq("user_id", user_id)
                .eq("content_id", content_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Saved content not found for user '{user_id}' "
                    f"and content '{content_id}'."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[UserContentRepository.update_last_viewed] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def archive_content(
        user_id: str,
        content_id: str,
        archived: bool = True,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("user_contents")
                .update({"archived": archived})
                .eq("user_id", user_id)
                .eq("content_id", content_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Saved content not found for user '{user_id}' "
                    f"and content '{content_id}'."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[UserContentRepository.archive_content] "
                f"APIError ({user_id}:{content_id}): {e.message}"
            )
            raise

    @staticmethod
    def count_total_saves() -> int:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("user_contents")
                .select("id", count="exact")
                .execute()
            )

            return response.count or 0

        except APIError as e:
            logger.error(
                "[UserContentRepository.count_total_saves] "
                f"APIError: {e.message}"
            )
            raise