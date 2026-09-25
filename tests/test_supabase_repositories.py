import unittest
from unittest.mock import MagicMock, patch

from app.db.repositories.users import UserRepository
from app.db.repositories.channels import ChannelRepository
from app.db.repositories.contents import ContentRepository
from app.db.repositories.user_contents import UserContentRepository
from app.db.repositories.jobs import JobRepository


class TestSupabaseRepositories(unittest.TestCase):

    @patch("app.db.repositories.users.get_supabase_client")
    def test_users_repository(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "usr-123",
                    "preferred_language": "English",
                }
            ]
        )

        user = UserRepository.get_user("usr-123")

        self.assertIsNotNone(user)
        self.assertEqual(user["id"], "usr-123")
        self.assertEqual(user["preferred_language"], "English")

    @patch("app.db.repositories.channels.get_supabase_client")
    def test_channels_repository(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_client.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "chn-1",
                    "channel_type": "telegram",
                    "external_user_id": "999",
                }
            ]
        )

        channel = ChannelRepository.upsert_channel(
            "usr-123",
            "telegram",
            "999",
            "Tester",
            "testuser",
        )

        self.assertEqual(channel["channel_type"], "telegram")
        self.assertEqual(channel["external_user_id"], "999")

    @patch("app.db.repositories.contents.get_supabase_client")
    def test_contents_repository(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "cnt-1",
                    "canonical_url": "https://instagram.com/reel/123",
                    "source_platform": "instagram",
                    "content_type": "video",
                }
            ]
        )

        content = ContentRepository.create_content(
            canonical_url="https://instagram.com/reel/123"
        )

        self.assertEqual(
            content["source_platform"],
            "instagram",
        )

        self.assertEqual(
            content["content_type"],
            "video",
        )

    @patch("app.db.repositories.user_contents.get_supabase_client")
    def test_user_contents_repository(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_client.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "uc-1",
                    "user_id": "usr-123",
                    "content_id": "cnt-1",
                    "user_language": "English",
                }
            ]
        )

        saved = UserContentRepository.save_content(
            "usr-123",
            "cnt-1",
            "English",
        )

        self.assertEqual(
            saved["user_id"],
            "usr-123",
        )

        self.assertEqual(
            saved["user_language"],
            "English",
        )

    @patch("app.db.repositories.jobs.get_supabase_client")
    def test_jobs_repository(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock(
            data=[
                {
                    "id": "job-1",
                    "status": "queued",
                }
            ]
        )

        job = JobRepository.create_job(
            "cnt-1",
            "analysis",
        )

        self.assertEqual(
            job["status"],
            "queued",
        )


if __name__ == "__main__":
    unittest.main()