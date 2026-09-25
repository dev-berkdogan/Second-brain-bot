"""
Tests for download_media()'s YouTube extractor_args/player_client config.

app.py (root script) is loaded directly from its file path, same as
test_instagram_extraction.py, because the app/ package shadows the root
app.py module name on sys.path. yt_dlp.YoutubeDL is mocked so these tests
never touch the real network.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_PY_PATH = os.path.join(_REPO_ROOT, "app.py")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

_spec = importlib.util.spec_from_file_location("insta_bot_root_app_ytdlp", _APP_PY_PATH)
root_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = root_app
_spec.loader.exec_module(root_app)

YOUTUBE_URL = "https://www.youtube.com/watch?v=8_5lHgUU3C0"


class YoutubePlayerClientTests(unittest.TestCase):

    def setUp(self):
        self.mock_ydl_instance = MagicMock()
        self.mock_ydl_instance.extract_info.return_value = {
            "title": "t",
            "description": "d",
        }
        self.mock_ydl_instance.__enter__.return_value = self.mock_ydl_instance
        self.mock_ydl_instance.__exit__.return_value = False

        self.mock_ydl_cls = MagicMock(return_value=self.mock_ydl_instance)
        patcher = patch.object(root_app.yt_dlp, "YoutubeDL", self.mock_ydl_cls)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_does_not_force_stale_android_ios_web_player_client(self):
        """
        The hardcoded ["android", "ios", "web"] client list caused a live
        "Sign in to confirm you're not a bot" failure on 2026-09-11 (the
        android client is now heavily rate-limited by YouTube). yt-dlp's
        own maintained default client selection should be used instead of
        a stale hardcoded override.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root_app.download_media(YOUTUBE_URL, tmp)

        ydl_opts = self.mock_ydl_cls.call_args[0][0]
        youtube_args = ydl_opts.get("extractor_args", {}).get("youtube", {})
        forced_clients = youtube_args.get("player_client")

        self.assertNotEqual(
            forced_clients,
            ["android", "ios", "web"],
            "download_media() still forces the stale player_client list "
            "that caused today's YouTube bot-check failure",
        )


if __name__ == "__main__":
    unittest.main()
