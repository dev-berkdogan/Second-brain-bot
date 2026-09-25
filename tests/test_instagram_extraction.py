"""
Tests for app.py's extract_instagram_post() (Polaris GraphQL rewrite).

app.py (root script) is loaded directly from its file path rather than via
`import app`, because the app/ package (app/db, app/services) shadows the
root app.py module name on sys.path. All network calls (requests.Session,
requests.post, requests.get) and get_random_proxy() are mocked, so these
tests never touch the real network.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests as real_requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_PY_PATH = os.path.join(_REPO_ROOT, "app.py")

# extract_instagram_post's module needs these at import time; set dummy
# values (setdefault) so tests don't depend on a real .env being present.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

_spec = importlib.util.spec_from_file_location("insta_bot_root_app", _APP_PY_PATH)
root_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = root_app
_spec.loader.exec_module(root_app)


TEST_URL = "https://www.instagram.com/p/CTestShortcode1/"
LSD_HTML = '<script>window.__d("LSD",[],function(){return {"token":"testlsd123"}});</script>'
NO_LSD_HTML = "<html><body>Nothing useful here.</body></html>"


def make_response(status_code=200, text="", json_data=None, content=b"", headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    resp.headers = headers or {}

    if status_code >= 400:
        # Mirror real requests: raise_for_status() raises an HTTPError with
        # the response (so callers can inspect e.response.status_code).
        http_error = real_requests.exceptions.HTTPError(f"{status_code} Client Error")
        http_error.response = resp
        resp.raise_for_status.side_effect = http_error
    else:
        resp.raise_for_status.side_effect = None

    if json_data is not None:
        resp.json.return_value = json_data

    return resp


def graphql_payload(logged):
    return {"data": {"xig_polaris_media": {"if_not_gated_logged_out": logged}}}


class ExtractInstagramPostTests(unittest.TestCase):

    def setUp(self):
        # Patch only Session/post/get on the real `requests` module rather
        # than swapping root_app.requests for a bare MagicMock: the retry
        # helper's `except requests.exceptions.HTTPError` needs the genuine
        # exception class to still be there, not an auto-mocked attribute.
        self.mock_session = MagicMock()
        session_patcher = patch.object(
            root_app.requests, "Session", return_value=self.mock_session
        )
        session_patcher.start()
        self.addCleanup(session_patcher.stop)

        post_patcher = patch.object(root_app.requests, "post")
        self.mock_post = post_patcher.start()
        self.addCleanup(post_patcher.stop)

        get_patcher = patch.object(root_app.requests, "get")
        self.mock_get = get_patcher.start()
        self.addCleanup(get_patcher.stop)

        self.mock_session.get.return_value = make_response(text=LSD_HTML)
        self.mock_session.cookies = {}

        proxy_patcher = patch.object(
            root_app, "get_random_proxy", return_value="http://proxy.test:8080"
        )
        proxy_patcher.start()
        self.addCleanup(proxy_patcher.stop)

        # The retry helper sleeps between attempts; keep tests instant.
        sleep_patcher = patch.object(root_app.time, "sleep")
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _run(self, output_dir):
        # Exercise the Polaris implementation directly (not the public
        # extract_instagram_post() wrapper) so these tests stay focused on
        # Polaris's own behavior and don't silently fall through to the
        # legacy chain. The wrapper's dispatch logic is covered separately
        # by ExtractInstagramPostFallbackTests below.
        return root_app._extract_instagram_post_polaris(TEST_URL, output_dir)

    # 1. Happy path - single image post (no carousel_media field)
    def test_single_image_happy_path(self):
        logged = {
            "media_type": 1,
            "display_uri": "https://cdn.example.com/img.jpg",
            "caption": {"text": "hello world"},
        }
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.return_value = make_response(
            status_code=200, content=b"x" * 4000, headers={"Content-Type": "image/jpeg"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp)
            files = sorted(os.listdir(tmp))

        self.assertEqual(files, ["slide_00.jpg"])
        self.assertEqual(result["caption"], "hello world")

    # 2. Happy path - multi-item carousel, mixed image/video, mixed content-types
    def test_carousel_happy_path(self):
        items = [
            {"media_type": 1, "display_uri": "https://cdn.example.com/a.png"},
            {"media_type": 2, "display_uri": "https://cdn.example.com/b.mp4"},
            {"media_type": 1, "display_uri": "https://cdn.example.com/c.webp"},
        ]
        logged = {"carousel_media": items, "caption": {"text": "carousel caption"}}
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.side_effect = [
            make_response(status_code=200, content=b"a" * 4000, headers={"Content-Type": "image/png"}),
            make_response(status_code=200, content=b"b" * 4000, headers={"Content-Type": "video/mp4"}),
            make_response(status_code=200, content=b"c" * 4000, headers={"Content-Type": "image/webp"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            files = sorted(os.listdir(tmp))

        self.assertEqual(files, ["slide_00.png", "slide_01.mp4", "slide_02.webp"])

    # 3. Partial failure - one item 404s, the rest still get saved
    def test_carousel_partial_failure_keeps_successful_items(self):
        items = [
            {"media_type": 1, "display_uri": "https://cdn.example.com/a.jpg"},
            {"media_type": 1, "display_uri": "https://cdn.example.com/b.jpg"},
        ]
        logged = {"carousel_media": items}
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.side_effect = [
            make_response(status_code=404),
            make_response(status_code=200, content=b"b" * 4000, headers={"Content-Type": "image/jpeg"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            files = sorted(os.listdir(tmp))

        self.assertEqual(files, ["slide_01.jpg"])

    # 4. Complete failure - every item fails -> ValueError, no silent empty success
    def test_all_items_failing_raises_value_error(self):
        items = [
            {"media_type": 1, "display_uri": "https://cdn.example.com/a.jpg"},
            {"media_type": 1, "display_uri": "https://cdn.example.com/b.jpg"},
        ]
        logged = {"carousel_media": items}
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.side_effect = [
            make_response(status_code=404),
            make_response(status_code=500),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._run(tmp)

    # 5. LSD token missing from the page HTML (markup/endpoint changed)
    def test_missing_lsd_token_raises_value_error(self):
        self.mock_session.get.return_value = make_response(text=NO_LSD_HTML)

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "LSD"):
                self._run(tmp)

    # 6. Gated/auth-required content (if_not_gated_logged_out is null)
    #
    # Raises the dedicated InstagramGatedContentError (a ValueError
    # subclass) rather than a generic parse-failure ValueError, so the
    # fallback wrapper can tell this apart from a technical failure and
    # skip the legacy chain (see ExtractInstagramPostFallbackTests below).
    def test_gated_content_raises_clean_value_error(self):
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(None)
        )

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(root_app.InstagramGatedContentError):
                self._run(tmp)

    # 7. Rate limit / 5xx retry (page fetch + GraphQL), each with 2-3
    #    attempts and short backoff.

    def test_rate_limited_page_fetch_retries_then_succeeds(self):
        self.mock_session.get.side_effect = [
            make_response(status_code=429),
            make_response(status_code=200, text=LSD_HTML),
        ]
        logged = {
            "media_type": 1,
            "display_uri": "https://cdn.example.com/img.jpg",
        }
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.return_value = make_response(
            status_code=200, content=b"x" * 4000, headers={"Content-Type": "image/jpeg"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            files = sorted(os.listdir(tmp))

        self.assertEqual(files, ["slide_00.jpg"])
        self.assertEqual(self.mock_session.get.call_count, 2)
        self.mock_sleep.assert_called_once_with(1)

    def test_rate_limited_page_fetch_exhausts_retries_then_raises(self):
        self.mock_session.get.side_effect = [
            make_response(status_code=429),
            make_response(status_code=503),
            make_response(status_code=429),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(real_requests.exceptions.HTTPError):
                self._run(tmp)

        self.assertEqual(self.mock_session.get.call_count, 3)
        self.assertEqual(self.mock_sleep.call_count, 2)
        self.mock_sleep.assert_any_call(1)
        self.mock_sleep.assert_any_call(2)

    def test_graphql_rate_limited_retries_then_succeeds(self):
        logged = {
            "media_type": 1,
            "display_uri": "https://cdn.example.com/img.jpg",
        }
        self.mock_session.post.side_effect = [
            make_response(status_code=503),
            make_response(json_data=graphql_payload(logged)),
        ]
        self.mock_get.return_value = make_response(
            status_code=200, content=b"x" * 4000, headers={"Content-Type": "image/jpeg"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            files = sorted(os.listdir(tmp))

        self.assertEqual(files, ["slide_00.jpg"])
        self.assertEqual(self.mock_session.post.call_count, 2)

    def test_non_retryable_4xx_does_not_retry(self):
        # A plain 404 (not 429/5xx) must fail immediately, with no retry
        # and no backoff sleep - side_effect has only one item, so a second
        # call would raise StopIteration and fail this test.
        self.mock_session.get.side_effect = [make_response(status_code=404)]

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(real_requests.exceptions.HTTPError):
                self._run(tmp)

        self.assertEqual(self.mock_session.get.call_count, 1)
        self.mock_sleep.assert_not_called()

    # 8. Tiny placeholder/broken download is filtered out, real one is kept
    def test_small_file_is_filtered_but_normal_one_kept(self):
        items = [
            {"media_type": 1, "display_uri": "https://cdn.example.com/tiny.jpg"},
            {"media_type": 1, "display_uri": "https://cdn.example.com/real.jpg"},
        ]
        logged = {"carousel_media": items}
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.side_effect = [
            make_response(status_code=200, content=b"x" * 100, headers={"Content-Type": "image/jpeg"}),
            make_response(status_code=200, content=b"y" * 4000, headers={"Content-Type": "image/jpeg"}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp)
            files = sorted(os.listdir(tmp))

        self.assertEqual(files, ["slide_01.jpg"])

    # 9. GraphQL request must reuse the page-fetch session (cookie
    #    continuity), not a fresh module-level requests.post call.
    def test_graphql_request_uses_session_not_module_level_requests(self):
        logged = {
            "media_type": 1,
            "display_uri": "https://cdn.example.com/img.jpg",
        }
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.return_value = make_response(
            status_code=200, content=b"x" * 4000, headers={"Content-Type": "image/jpeg"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            # Tolerate a failure here: against the pre-fix code, the GraphQL
            # call goes through the (unconfigured) module-level requests.post
            # mock instead, which yields garbage the parser can't handle.
            # What this test cares about is *which* mock received the call.
            try:
                self._run(tmp)
            except Exception:
                pass

        self.mock_session.post.assert_called_once()
        self.mock_post.assert_not_called()

    # 10. X-CSRFToken must be read from the session's csrftoken cookie
    #     (set by the page fetch), not hardcoded to an empty string.
    def test_graphql_uses_csrftoken_cookie_from_session(self):
        self.mock_session.cookies = {"csrftoken": "real-csrf-abc123"}

        logged = {
            "media_type": 1,
            "display_uri": "https://cdn.example.com/img.jpg",
        }
        self.mock_session.post.return_value = make_response(
            json_data=graphql_payload(logged)
        )
        self.mock_get.return_value = make_response(
            status_code=200, content=b"x" * 4000, headers={"Content-Type": "image/jpeg"}
        )

        with tempfile.TemporaryDirectory() as tmp:
            try:
                self._run(tmp)
            except Exception:
                pass

        self.assertTrue(self.mock_session.post.called)
        _, kwargs = self.mock_session.post.call_args
        self.assertEqual(kwargs["headers"]["X-CSRFToken"], "real-csrf-abc123")


class ExtractInstagramPostFallbackTests(unittest.TestCase):
    """
    Tests for the extract_instagram_post() Polaris/legacy dispatch wrapper.

    These patch _extract_instagram_post_polaris / _extract_instagram_post_legacy
    directly rather than the network layer, since it's the dispatch logic
    itself (not either implementation's internals, already covered above /
    pre-existing) that's under test here.
    """

    def test_technical_failure_falls_back_to_legacy_chain(self):
        legacy_result = {"caption": "legacy result"}

        with tempfile.TemporaryDirectory() as tmp:
            # A stray file left behind by a partially-failed Polaris attempt
            # should be cleaned up before the legacy chain writes into the
            # same output_dir.
            stray_file = os.path.join(tmp, "slide_00.jpg")
            with open(stray_file, "wb") as f:
                f.write(b"partial polaris leftover")

            with patch.object(
                root_app,
                "_extract_instagram_post_polaris",
                side_effect=ValueError("LSD token bulunamadi"),
            ) as mock_polaris, patch.object(
                root_app,
                "_extract_instagram_post_legacy",
                return_value=legacy_result,
            ) as mock_legacy:
                result = root_app.extract_instagram_post(TEST_URL, tmp)

            mock_polaris.assert_called_once_with(TEST_URL, tmp)
            mock_legacy.assert_called_once_with(TEST_URL, tmp)
            self.assertEqual(result, legacy_result)
            self.assertFalse(os.path.exists(stray_file))

    def test_gated_content_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                root_app,
                "_extract_instagram_post_polaris",
                side_effect=root_app.InstagramGatedContentError("özel hesap"),
            ) as mock_polaris, patch.object(
                root_app,
                "_extract_instagram_post_legacy",
            ) as mock_legacy:
                with self.assertRaises(root_app.InstagramGatedContentError):
                    root_app.extract_instagram_post(TEST_URL, tmp)

            mock_polaris.assert_called_once_with(TEST_URL, tmp)
            mock_legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
