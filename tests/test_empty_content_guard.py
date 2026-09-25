"""
Tests for the empty-media/empty-caption guard in process_url_content().

app.py (root script) is loaded directly from its file path, same as
test_instagram_extraction.py, because the app/ package shadows the root
app.py module name on sys.path. All Supabase repositories, the Chroma
collection, and download_media/generate_content_with_retry are mocked so
these tests never touch the network or a real database.

Regression coverage for the 2026-09-11 incident: a TikTok download failed
silently (yt-dlp ignoreerrors=True), leaving media_files=[] and caption="",
but the pipeline still called Gemini, which hallucinated an unrelated
analysis ("German hydrogen infrastructure") and stored it as if genuine
(see BLUEPRINT.md §6).
"""

import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_PY_PATH = os.path.join(_REPO_ROOT, "app.py")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

_spec = importlib.util.spec_from_file_location("insta_bot_root_app_emptyguard", _APP_PY_PATH)
root_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = root_app
_spec.loader.exec_module(root_app)

TIKTOK_URL = "https://www.tiktok.com/@someone/video/1234567890123456789"


class EmptyContentGuardTests(unittest.TestCase):

    def setUp(self):
        self.content_repo_patcher = patch.object(root_app, "ContentRepository")
        self.mock_content_repo = self.content_repo_patcher.start()
        self.addCleanup(self.content_repo_patcher.stop)
        self.mock_content_repo.get_by_canonical_url.return_value = None
        self.mock_content_repo.create_content.return_value = {"id": "content-1"}

        self.job_repo_patcher = patch.object(root_app, "JobRepository")
        self.mock_job_repo = self.job_repo_patcher.start()
        self.addCleanup(self.job_repo_patcher.stop)
        self.mock_job_repo.create_job.return_value = {"id": "job-1"}

        self.user_content_repo_patcher = patch.object(root_app, "UserContentRepository")
        self.mock_user_content_repo = self.user_content_repo_patcher.start()
        self.addCleanup(self.user_content_repo_patcher.stop)

        self.download_media_patcher = patch.object(root_app, "download_media")
        self.mock_download_media = self.download_media_patcher.start()
        self.addCleanup(self.download_media_patcher.stop)

        self.generate_patcher = patch.object(root_app, "generate_content_with_retry")
        self.mock_generate = self.generate_patcher.start()
        self.addCleanup(self.generate_patcher.stop)

        self.collection_patcher = patch.object(root_app, "collection")
        self.mock_collection = self.collection_patcher.start()
        self.addCleanup(self.collection_patcher.stop)

    # 1. No media downloaded AND no caption -> guard fires, Gemini is never
    #    called, and the job/content are marked failed instead of completed.
    def test_no_media_and_empty_caption_blocks_gemini_call(self):
        self.mock_download_media.return_value = {"caption": ""}

        with self.assertRaises(ValueError) as cm:
            root_app.process_url_content(TIKTOK_URL, "user-1", "English")

        self.assertIn("İndirilen medya veya metin bulunamadı", str(cm.exception))
        self.mock_generate.assert_not_called()
        self.mock_job_repo.mark_failed.assert_called_once()
        self.mock_content_repo.update_status.assert_any_call(
            content_id="content-1", status="failed"
        )

    # 1b. No media AND a caption too short to be meaningful (< 10 chars)
    #     also blocks the Gemini call.
    def test_no_media_and_too_short_caption_blocks_gemini_call(self):
        self.mock_download_media.return_value = {"caption": "hi"}

        with self.assertRaises(ValueError):
            root_app.process_url_content(TIKTOK_URL, "user-1", "English")

        self.mock_generate.assert_not_called()

    # 2. Inverse case: no media, but the caption alone is long enough
    #    (>= 10 chars) to be worth analyzing -> guard does NOT fire, normal
    #    flow continues through to Gemini and a completed result.
    def test_no_media_but_long_enough_caption_proceeds_normally(self):
        self.mock_download_media.return_value = {
            "caption": "This is a perfectly normal, sufficiently long caption."
        }
        self.mock_generate.return_value = (
            "Source Language: English\nSummary: a real analysis of the caption."
        )

        result = root_app.process_url_content(TIKTOK_URL, "user-1", "English")

        self.mock_generate.assert_called_once()
        self.mock_job_repo.mark_failed.assert_not_called()
        self.mock_content_repo.update_status.assert_any_call(
            content_id="content-1", status="completed"
        )
        self.assertIn("a real analysis of the caption.", result["analysis"])


if __name__ == "__main__":
    unittest.main()
