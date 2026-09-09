from __future__ import annotations

import json
import uuid
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from hooks import apply_filters, do_action


def create_transcript_event(text: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    return {
        "id": uuid.uuid4().hex,
        "text": text,
        "time": now.strftime("%H:%M:%S"),
        "metadata": {},
    }


def process_transcript_text(
    text: str,
    transcript_file: Path,
    *,
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
    commit_guard: Callable[[], AbstractContextManager[bool]] | None = None,
) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None

    event = create_transcript_event(stripped, now=now)
    hook_context = {"transcript_file": transcript_file, **(context or {})}

    event = apply_filters("transcript.before_save", event, hook_context)
    if not event or not event.get("text"):
        return None

    serialized = json.dumps(event) + "\n"
    # Callbacks run outside the guard so shutdown never waits on plugin code.
    with commit_guard() if commit_guard else nullcontext(True) as allowed:
        if not allowed:
            return None
        with open(transcript_file, "a", encoding="utf-8") as f:
            f.write(serialized)

    do_action("transcript.after_save", event, hook_context)

    render_event = apply_filters("transcript.before_render", event.copy(), hook_context)
    if not render_event:
        return None

    return render_event


def queue_transcript_render_event(
    render_event: dict[str, Any],
    output_queue: Any,
    *,
    context: dict[str, Any] | None = None,
    commit_guard: Callable[[], AbstractContextManager[bool]] | None = None,
) -> None:
    with commit_guard() if commit_guard else nullcontext(True) as allowed:
        if not allowed:
            return
        output_queue.put(render_event)
    do_action("transcript.after_render", render_event, context or {})
