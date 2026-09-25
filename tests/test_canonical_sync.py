import unittest
from unittest.mock import MagicMock, patch

from app.db.url_utils import normalize_url
from app.db.repositories.contents import ContentRepository


class TestCanonicalURLConsistency(unittest.TestCase):

    def test_youtube_normalization(self):
        expected = "https://www.youtube.com/watch?v=hudJZYmNZz4"

        test_urls = [
            "https://www.youtube.com/watch?v=hudJZYmNZz4",
            "https://www.youtube.com/watch?v=hudJZYmNZz4&t=10s",
            "https://youtu.be/hudJZYmNZz4?si=abc123",
            "https://www.youtube.com/shorts/hudJZYmNZz4?feature=share",
        ]

        for url in test_urls:
            with self.subTest(url=url):
                self.assertEqual(
                    normalize_url(url),
                    expected,
                )

    @patch("app.db.repositories.contents.get_supabase_client")
    def test_repository_uses_same_canonical_url(
        self,
        mock_get_client,
    ):
        expected = "https://www.youtube.com/watch?v=hudJZYmNZz4"

        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_response = MagicMock()
        mock_response.data = [
            {
                "id": "test-content",
                "canonical_url": expected,
            }
        ]

        (
            mock_client
            .table.return_value
            .select.return_value
            .eq.return_value
            .execute.return_value
        ) = mock_response

        raw_url = (
            "https://www.youtube.com/watch?"
            "v=hudJZYmNZz4&t=10s&si=abc123"
        )

        result = ContentRepository.get_by_canonical_url(raw_url)

        self.assertIsNotNone(result)
        self.assertEqual(
            result["canonical_url"],
            expected,
        )

        mock_client.table.assert_called_once_with("contents")

        eq_call = (
            mock_client
            .table.return_value
            .select.return_value
            .eq
        )

        eq_call.assert_called_once_with(
            "canonical_url",
            expected,
        )


if __name__ == "__main__":
    unittest.main()