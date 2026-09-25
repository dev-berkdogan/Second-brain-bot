import logging
from typing import Optional, Dict, Any

from postgrest.exceptions import APIError

from app.db.supabase_client import (
    get_supabase_client,
    get_current_utc_iso,
)

logger = logging.getLogger(__name__)


class ChannelRepository:

    @staticmethod
    def get_channel_by_external_id(
        channel_type: str,
        external_user_id: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("channels")
                .select("*")
                .eq("channel_type", channel_type)
                .eq("external_user_id", str(external_user_id))
                .execute()
            )

            return response.data[0] if response.data else None

        except APIError as e:
            logger.error(
                "[ChannelRepository.get_channel_by_external_id] "
                f"APIError ({channel_type}:{external_user_id}): {e.message}"
            )
            raise

    @staticmethod
    def upsert_channel(
        user_id: str,
        channel_type: str,
        external_user_id: str,
        display_name: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "user_id": user_id,
            "channel_type": channel_type,
            "external_user_id": str(external_user_id),
            "display_name": display_name or "",
            "username": username or "",
            "last_seen_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("channels")
                .upsert(
                    payload,
                    on_conflict="channel_type,external_user_id",
                )
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[ChannelRepository.upsert_channel] APIError: {e.message}"
            )
            raise

    @staticmethod
    def update_last_seen(
        channel_id: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "last_seen_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("channels")
                .update(payload)
                .eq("id", channel_id)
                .execute()
            )

            return response.data[0] if response.data else {}

        except APIError as e:
            logger.error(
                f"[ChannelRepository.update_last_seen] "
                f"APIError ({channel_id}): {e.message}"
            )
            raise