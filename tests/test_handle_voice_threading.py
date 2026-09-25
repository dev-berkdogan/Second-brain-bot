"""
Test that handle_voice() waits for the uploaded Gemini file to become ACTIVE
via asyncio.to_thread(_wait_for_active_file, ...) instead of calling it
directly - so the blocking poll (time.sleep-based) runs off the event loop,
not on it.

app.py (root script) is loaded directly from its file path rather than via
`import app`, because the app/ package (app/db, app/services) shadows the
root app.py module name on sys.path (same approach as the other test files
in this directory).
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_PY_PATH = os.path.join(_REPO_ROOT, "app.py")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")

_spec = importlib.util.spec_from_file_location("insta_bot_root_app", _APP_PY_PATH)
root_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = root_app
_spec.loader.exec_module(root_app)


def make_file(state_name: str, name: str = "files/voice123"):
    return types.SimpleNamespace(
        name=name,
        state=types.SimpleNamespace(name=state_name),
    )


class HandleVoiceThreadingTests(unittest.TestCase):

    def setUp(self):
        # Real asyncio.to_thread would spin up a real worker thread; for the
        # test we just execute the callable inline on the event loop (still
        # a genuinely separate code path from calling it directly), so we
        # can both let handle_voice run to completion AND assert on the call.
        async def run_inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        self.mock_to_thread = AsyncMock(side_effect=run_inline)
        to_thread_patcher = patch.object(root_app.asyncio, "to_thread", self.mock_to_thread)
        to_thread_patcher.start()
        self.addCleanup(to_thread_patcher.stop)

        sleep_patcher = patch.object(root_app.time, "sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        track_user_patcher = patch.object(
            root_app, "track_and_get_user", return_value=("user-1", "English")
        )
        track_user_patcher.start()
        self.addCleanup(track_user_patcher.stop)

        upload_patcher = patch.object(
            root_app.client.files, "upload", return_value=make_file("PROCESSING")
        )
        upload_patcher.start()
        self.addCleanup(upload_patcher.stop)

        get_patcher = patch.object(
            root_app.client.files, "get", return_value=make_file("ACTIVE")
        )
        self.mock_files_get = get_patcher.start()
        self.addCleanup(get_patcher.stop)

        patch.object(root_app.client.files, "delete").start()
        self.addCleanup(patch.stopall)

        transcribe_patcher = patch.object(
            root_app, "generate_content_with_retry", return_value="hello world"
        )
        transcribe_patcher.start()
        self.addCleanup(transcribe_patcher.stop)

        # Empty query results short-circuit handle_voice right after
        # transcription, before it would reach the (unrelated) RAG path.
        query_patcher = patch.object(
            root_app.collection, "query", return_value={"documents": [[]], "metadatas": [[]]}
        )
        query_patcher.start()
        self.addCleanup(query_patcher.stop)

    def _make_update_and_context(self):
        update = MagicMock()
        update.effective_user.id = 111
        update.effective_chat.id = 222
        update.message.voice.file_id = "voice-file-id"
        update.message.reply_text = AsyncMock(return_value=MagicMock(message_id=999))

        context = MagicMock()
        context.bot.get_file = AsyncMock(
            return_value=MagicMock(download_to_drive=AsyncMock())
        )
        context.bot.edit_message_text = AsyncMock()
        context.bot.send_message = AsyncMock()

        return update, context

    def test_handle_voice_waits_for_active_file_via_to_thread(self):
        update, context = self._make_update_and_context()

        asyncio.run(root_app.handle_voice(update, context))

        # asyncio.to_thread must have been called with _wait_for_active_file
        # as the target callable (not called directly/inline).
        calls_with_wait_fn = [
            call for call in self.mock_to_thread.call_args_list
            if call.args and call.args[0] is root_app._wait_for_active_file
        ]
        self.assertEqual(len(calls_with_wait_fn), 1)

        # And the polling helper actually ran and reached ACTIVE (proves the
        # to_thread wiring produces a working result, not just a recorded call).
        self.mock_files_get.assert_called_once()

    def test_files_get_not_called_directly_outside_to_thread(self):
        # Regression guard: the old inline `while state == "PROCESSING":
        # ... client.files.get(...)` loop must be gone. Every files.get()
        # call should happen only from inside the to_thread-dispatched
        # _wait_for_active_file, never directly in handle_voice's own frame.
        update, context = self._make_update_and_context()

        asyncio.run(root_app.handle_voice(update, context))

        self.assertTrue(self.mock_to_thread.called)
        self.assertEqual(self.mock_files_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
