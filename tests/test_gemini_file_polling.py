"""
Tests for app.py's Gemini video-file ACTIVE-state polling helper
(_wait_for_active_file, used by analyze_canonical_multimodal_content's
video upload block).

app.py (root script) is loaded directly from its file path rather than via
`import app`, because the app/ package (app/db, app/services) shadows the
root app.py module name on sys.path (same approach as
tests/test_instagram_extraction.py). client.files.get and time.sleep are
mocked, so these tests never touch the real Gemini API or sleep for real.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_PY_PATH = os.path.join(_REPO_ROOT, "app.py")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

_spec = importlib.util.spec_from_file_location("insta_bot_root_app", _APP_PY_PATH)
root_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = root_app
_spec.loader.exec_module(root_app)


def make_file(state_name: str, name: str = "files/abc123"):
    """A stand-in for google.genai's File object: only .state.name and .name matter here."""
    return types.SimpleNamespace(
        name=name,
        state=types.SimpleNamespace(name=state_name),
    )


class WaitForActiveFileTests(unittest.TestCase):

    def setUp(self):
        get_patcher = patch.object(root_app.client.files, "get")
        self.mock_get = get_patcher.start()
        self.addCleanup(get_patcher.stop)

        sleep_patcher = patch.object(root_app.time, "sleep")
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    # 1. PROCESSING -> ACTIVE transition
    def test_processing_then_active_returns_active_file(self):
        uploaded = make_file("PROCESSING")
        self.mock_get.side_effect = [
            make_file("PROCESSING"),
            make_file("ACTIVE"),
        ]

        result = root_app._wait_for_active_file(uploaded)

        self.assertEqual(result.state.name, "ACTIVE")
        self.assertEqual(self.mock_get.call_count, 2)
        self.assertEqual(self.mock_sleep.call_count, 2)
        for call in self.mock_sleep.call_args_list:
            self.assertEqual(call.args[0], 5)

    def test_already_active_polls_nothing(self):
        uploaded = make_file("ACTIVE")

        result = root_app._wait_for_active_file(uploaded)

        self.assertEqual(result.state.name, "ACTIVE")
        self.mock_get.assert_not_called()
        self.mock_sleep.assert_not_called()

    # 2. PROCESSING -> FAILED transition
    def test_failed_state_raises_immediately_without_further_polling(self):
        uploaded = make_file("PROCESSING")
        self.mock_get.side_effect = [
            make_file("PROCESSING"),
            make_file("FAILED"),
        ]

        with self.assertRaises(root_app.GeminiFileProcessingError):
            root_app._wait_for_active_file(uploaded)

        # Stops right after seeing FAILED - no extra poll beyond that.
        self.assertEqual(self.mock_get.call_count, 2)

    def test_failed_on_initial_upload_response_raises_without_polling(self):
        uploaded = make_file("FAILED")

        with self.assertRaises(root_app.GeminiFileProcessingError):
            root_app._wait_for_active_file(uploaded)

        self.mock_get.assert_not_called()
        self.mock_sleep.assert_not_called()

    # 3. Timeout scenario - never reaches ACTIVE or FAILED
    def test_stuck_in_processing_times_out_after_max_attempts(self):
        uploaded = make_file("PROCESSING")
        self.mock_get.side_effect = [make_file("PROCESSING") for _ in range(20)]

        with self.assertRaises(root_app.GeminiFileProcessingError):
            root_app._wait_for_active_file(uploaded, poll_interval=5, max_attempts=12)

        self.assertEqual(self.mock_get.call_count, 12)
        self.assertEqual(self.mock_sleep.call_count, 12)


if __name__ == "__main__":
    unittest.main()
