import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from hooks import add_action, add_filter, clear_hooks
from transcript_events import process_transcript_text, queue_transcript_render_event


class TranscriptEventTests(unittest.TestCase):
    def tearDown(self):
        clear_hooks()

    def test_process_transcript_text_filters_save_and_render_payloads(self):
        after_save_calls = []

        def before_save(event, context):
            event = event.copy()
            event["text"] = event["text"].upper()
            event["metadata"] = {"backend": context["backend"]}
            return event

        def before_render(event, context):
            event = event.copy()
            event["render"] = {"html": f"<strong>{event['text']}</strong>"}
            return event

        add_filter("transcript.before_save", before_save)
        add_filter("transcript.before_render", before_render)
        add_action("transcript.after_save", lambda event, context: after_save_calls.append(event.copy()))

        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "session.jsonl"
            event = process_transcript_text(
                "hello world",
                transcript_file,
                context={"backend": "test-backend"},
                now=datetime(2026, 7, 7, 12, 30, 0),
            )

            saved = json.loads(transcript_file.read_text().strip())

        self.assertEqual(saved["text"], "HELLO WORLD")
        self.assertEqual(saved["time"], "12:30:00")
        self.assertEqual(saved["metadata"], {"backend": "test-backend"})
        self.assertEqual(event["render"], {"html": "<strong>HELLO WORLD</strong>"})
        self.assertEqual(after_save_calls[0]["text"], "HELLO WORLD")

    def test_queue_transcript_render_event_fires_after_render_action(self):
        class OutputQueue:
            def __init__(self):
                self.items = []

            def put(self, item):
                self.items.append(item)

        calls = []
        event = {"id": "1", "text": "hello", "time": "12:00:00"}
        output_queue = OutputQueue()

        add_action("transcript.after_render", lambda payload, context: calls.append((payload, context)))

        queue_transcript_render_event(event, output_queue, context={"source": "test"})

        self.assertEqual(output_queue.items, [event])
        self.assertEqual(calls, [(event, {"source": "test"})])

    def test_empty_transcript_text_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript_file = Path(tmp) / "session.jsonl"
            event = process_transcript_text("  ", transcript_file)

            self.assertIsNone(event)
            self.assertFalse(transcript_file.exists())


if __name__ == "__main__":
    unittest.main()
