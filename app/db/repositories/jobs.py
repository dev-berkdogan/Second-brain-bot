import logging
from typing import Dict, Any

from postgrest.exceptions import APIError

from app.db.supabase_client import (
    get_supabase_client,
    get_current_utc_iso,
)

logger = logging.getLogger(__name__)


class JobRepository:

    @staticmethod
    def create_job(
        content_id: str,
        job_type: str = "analysis",
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "content_id": content_id,
            "job_type": job_type,
            "status": "queued",
            "attempts": 0,
        }

        try:
            response = (
                client
                .table("processing_jobs")
                .insert(payload)
                .execute()
            )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[JobRepository.create_job] "
                f"APIError ({content_id}): {e.message}"
            )
            raise

    @staticmethod
    def mark_processing(
        job_id: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "status": "processing",
            "started_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("processing_jobs")
                .update(payload)
                .eq("id", job_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Processing job '{job_id}' was not found."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[JobRepository.mark_processing] "
                f"APIError ({job_id}): {e.message}"
            )
            raise

    @staticmethod
    def mark_completed(
        job_id: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "status": "completed",
            "completed_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("processing_jobs")
                .update(payload)
                .eq("id", job_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Processing job '{job_id}' was not found."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[JobRepository.mark_completed] "
                f"APIError ({job_id}): {e.message}"
            )
            raise

    @staticmethod
    def mark_failed(
        job_id: str,
        error_message: str,
    ) -> Dict[str, Any]:

        client = get_supabase_client()

        payload = {
            "status": "failed",
            "error_message": error_message,
            "completed_at": get_current_utc_iso(),
        }

        try:
            response = (
                client
                .table("processing_jobs")
                .update(payload)
                .eq("id", job_id)
                .execute()
            )

            if not response.data:
                raise ValueError(
                    f"Processing job '{job_id}' was not found."
                )

            return response.data[0]

        except APIError as e:
            logger.error(
                f"[JobRepository.mark_failed] "
                f"APIError ({job_id}): {e.message}"
            )
            raise