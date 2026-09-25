"""
Tests for _fetch_generic_web_text() (generic web article extraction).

app.py (root script) is loaded directly from its file path, same as
test_instagram_extraction.py, because the app/ package shadows the root
app.py module name on sys.path. requests.get and trafilatura are mocked so
these tests never touch the real network.
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

_spec = importlib.util.spec_from_file_location("insta_bot_root_app_webfetch", _APP_PY_PATH)
root_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = root_app
_spec.loader.exec_module(root_app)

TEST_URL = "https://medium.com/@deepakchhugani/yc-as-a-solo-nontechnical-founder-2424c5c25e25"


class FetchGenericWebTextTests(unittest.TestCase):

    def setUp(self):
        get_patcher = patch.object(root_app.requests, "get")
        self.mock_get = get_patcher.start()
        self.addCleanup(get_patcher.stop)

        extract_patcher = patch.object(root_app.trafilatura, "extract")
        self.mock_extract = extract_patcher.start()
        self.addCleanup(extract_patcher.stop)

        fetch_url_patcher = patch.object(root_app.trafilatura, "fetch_url")
        self.mock_fetch_url = fetch_url_patcher.start()
        self.addCleanup(fetch_url_patcher.stop)

    # 1. A realistic browser header set is sent on the first attempt, not
    #    the bare "Mozilla/5.0" that WAFs recognize as a scraper signature.
    def test_first_attempt_uses_realistic_browser_headers(self):
        self.mock_get.return_value = MagicMock(status_code=200, text="<html>ok</html>")
        self.mock_extract.return_value = "article text"

        root_app._fetch_generic_web_text(TEST_URL)

        _, kwargs = self.mock_get.call_args
        sent_headers = kwargs["headers"]
        self.assertIn("Chrome", sent_headers["User-Agent"])
        self.assertNotEqual(sent_headers["User-Agent"], "Mozilla/5.0")
        self.assertIn("Accept", sent_headers)
        self.assertIn("Accept-Language", sent_headers)

    # 2. An exception on the first attempt is logged (not silently
    #    swallowed by a bare `except: pass`), so the real failure reason
    #    is visible instead of an opaque "Web page could not be fetched."
    def test_first_attempt_exception_is_logged_not_swallowed(self):
        self.mock_get.side_effect = ConnectionError("proxy refused connection")
        self.mock_fetch_url.return_value = "<html>fallback</html>"
        self.mock_extract.return_value = "fallback text"

        with self.assertLogs(root_app.logger, level="WARNING") as cm:
            result = root_app._fetch_generic_web_text(TEST_URL)

        self.assertTrue(
            any("proxy refused connection" in line for line in cm.output),
            f"Expected the exception message in logs, got: {cm.output}",
        )
        self.assertEqual(result, "fallback text")

    # 3. The trafilatura.fetch_url() fallback is also given the same
    #    browser User-Agent via its config, instead of trafilatura's own
    #    self-identifying default ("trafilatura/x.y.z ...").
    def test_fallback_passes_same_user_agent_via_config(self):
        self.mock_get.return_value = MagicMock(status_code=403, text="")
        self.mock_extract.return_value = "fallback text"
        self.mock_fetch_url.return_value = "<html>fallback</html>"

        result = root_app._fetch_generic_web_text(TEST_URL)

        self.mock_fetch_url.assert_called_once()
        _, kwargs = self.mock_fetch_url.call_args
        config = kwargs["config"]
        self.assertEqual(
            config.get("DEFAULT", "USER_AGENTS"),
            root_app.GENERIC_WEB_USER_AGENT,
        )
        self.assertEqual(result, "fallback text")


if __name__ == "__main__":
    unittest.main()
