import logging
import hashlib
from typing import Optional, Dict, Any, List

from postgrest.exceptions import APIError

from app.db.supabase_client import (
    get_supabase_client,
    get_current_utc_iso,
)
from app.db.url_utils import normalize_url

logger = logging.getLogger(__name__)


def detect_source_platform(url: str) -> str:
    """
    Detect the source platform from a URL.
    """
    u = url.lower()

    if "instagram.com" in u:
        return "instagram"

    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"

    if "tiktok.com" in u:
        return "tiktok"

    if "linkedin.com" in u:
        return "linkedin"

    if "twitter.com" in u or "x.com" in u:
        return "twitter"

    if u.startswith("http://") or u.startswith("https://"):
        return "web"

    return "unknown"


def detect_content_type(url: str, platform: str) -> str:
    """
    URL-based best-effort content type detection.

    Instagram /p/ is intentionally returned as "unknown" because
    the URL alone cannot distinguish image, carousel, or video.
    The final type is determined from downloaded media.
    """
    u = url.lower()

    if platform in ("youtube", "tiktok"):
        return "video"

    if "/reel/" in u or "/tv/" in u:
        return "video"

    if platform == "instagram" and "/p/" in u:
        return "unknown"

    if platform == "web":
        return "article"

    return "unknown"


def generate_content_hash(canonical_url: str) -> str:
    """
    Generate a deterministic fingerprint based on the canonical URL.

    NOTE:
    This is currently a URL fingerprint, not a true media-content fingerprint.
    """
    return hashlib.sha256(
        canonical_url.encode("utf-8")
    ).hexdigest()


class ContentRepository:

    @staticmethod
    def get_by_id(
        content_id: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("contents")
                .select("*")
                .eq("id", content_id)
                .execute()
            )

            return (
                response.data[0]
                if response.data
                else None
            )

        except APIError as e:
            logger.error(
                f"[ContentRepository.get_by_id] "
                f"APIError ({content_id}): {e.message}"
            )
            raise

    @staticmethod
    def get_by_canonical_url(
        canonical_url: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()
        clean_url = normalize_url(canonical_url)

        try:
            response = (
                client
                .table("contents")
                .select("*")
                .eq("canonical_url", clean_url)
                .execute()
            )

            return (
                response.data[0]
                if response.data
                else None
            )

        except APIError as e:
            logger.error(
                "[ContentRepository.get_by_canonical_url] "
                f"APIError: {e.message}"
            )
            raise

    @staticmethod
    def get_by_hash(
        content_hash: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("contents")
                .select("*")
                .eq("content_hash", content_hash)
                .execute()
            )

            return (
                response.data[0]
                if response.data
                else None
            )

        except APIError as e:
            logger.error(
                f"[ContentRepository.get_by_hash] "
                f"APIError: {e.message}"
            )
            raise

    @staticmethod
    def create_content(
        canonical_url: Optional[str] = None,
        source_platform: Optional[str] = None,
        content_type: Optional[str] = None,
        title: Optional[str] = None,
        author_name: Optional[str] = None,
        author_external_id: Optional[str] = None,
        original_language: Optional[str] = None,
        caption: Optional[str] = None,
        transcript: Optional[str] = None,
        status: str = "received",
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        normalized_url = (
            normalize_url(canonical_url)
            if canonical_url
            else None
        )

        platform = (
            source_platform
            or (
                detect_source_platform(normalized_url)
                if normalized_url
                else "unknown"
            )
        )

        content_type_value = (
            content_type
            or (
                detect_content_type(
                    normalized_url,
                    platform,
                )
                if normalized_url
                else "unknown"
            )
        )

        content_hash = (
            generate_content_hash(normalized_url)
            if normalized_url
            else None
        )

        payload: Dict[str, Any] = {
            "canonical_url": normalized_url,
            "source_platform": platform,
            "content_type": content_type_value,
            "title": title or "",
            "author_name": author_name or "",
            "author_external_id": author_external_id or "",
            "original_language": original_language,
            "caption": caption or "",
            "transcript": transcript or "",
            "content_hash": content_hash,
            "status": status,
        }

        try:
            response = (
                client
                .table("contents")
                .insert(payload)
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[ContentRepository.create_content] "
                f"APIError: {e.message}"
            )
            raise

    @staticmethod
    def update_content(
        content_id: str,
        updates: Dict[str, Any],
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("contents")
                .update(updates)
                .eq("id", content_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Content '{content_id}' was not found."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[ContentRepository.update_content] "
                f"APIError ({content_id}): {e.message}"
            )
            raise

    @staticmethod
    def update_status(
        content_id: str,
        status: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload: Dict[str, Any] = {
            "status": status,
        }

        if status == "completed":
            payload["processed_at"] = get_current_utc_iso()

        try:
            response = (
                client
                .table("contents")
                .update(payload)
                .eq("id", content_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Content '{content_id}' was not found."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[ContentRepository.update_status] "
                f"APIError ({content_id}): {e.message}"
            )
            raise

    @staticmethod
    def save_canonical_analysis(
        content_id: str,
        summary: str,
        raw_analysis: str,
        source_language: str,
        model_name: str = "gemini-3.6-flash",
        model_provider: str = "google",
        prompt_version: str = "v1",
        key_points: Optional[List[str]] = None,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "content_id": content_id,
            "model_provider": model_provider,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "summary": summary,
            "raw_analysis": raw_analysis,
            "source_language": source_language,
            "key_points": key_points or [],
        }

        try:
            response = (
                client
                .table("content_analyses")
                .insert(payload)
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[ContentRepository.save_canonical_analysis] "
                f"APIError ({content_id}): {e.message}"
            )
            raise

    @staticmethod
    def get_latest_analysis(
        content_id: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("content_analyses")
                .select("*")
                .eq("content_id", content_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

            return (
                response.data[0]
                if response.data
                else None
            )

        except APIError as e:
            logger.error(
                "[ContentRepository.get_latest_analysis] "
                f"APIError ({content_id}): {e.message}"
            )
            raise

    @staticmethod
    def save_translation(
        content_id: str,
        language: str,
        translated_text: str,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        key_points: Optional[List[str]] = None,
        model_name: Optional[str] = "gemini-3.6-flash",
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "content_id": content_id,
            "language": language,
            "translated_text": translated_text,
            "title": title or "",
            "summary": summary or "",
            "key_points": key_points or [],
            "model_name": model_name,
        }

        try:
            response = (
                client
                .table("content_translations")
                .upsert(
                    payload,
                    on_conflict="content_id,language",
                )
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                "[ContentRepository.save_translation] "
                f"APIError ({content_id}:{language}): {e.message}"
            )
            raise

    @staticmethod
    def get_translation(
        content_id: str,
        language: str,
    ) -> Optional[Dict[str, Any]]:

        client = get_supabase_client()

        try:
            response = (
                client
                .table("content_translations")
                .select("*")
                .eq("content_id", content_id)
                .eq("language", language)
                .execute()
            )

            return (
                response.data[0]
                if response.data
                else None
            )

        except APIError as e:
            logger.error(
                "[ContentRepository.get_translation] "
                f"APIError ({content_id}:{language}): {e.message}"
            )
            raise