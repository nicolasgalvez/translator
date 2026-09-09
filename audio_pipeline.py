"""Bounded buffering between capture, utterance splitting, and inference."""

from collections import deque
import logging
import queue

SAMPLE_RATE = 48000
CAPTURE_CHUNK = 0.25
SILENCE_THRESHOLD = 0.0005
MAX_UTTERANCE = 5
MIN_UTTERANCE = 0.5
SILENCE_CHUNKS_TO_SPLIT = 2
CAPTURE_QUEUE_CAPACITY = 8
UTTERANCE_QUEUE_CAPACITY = 2

LOGGER = logging.getLogger(__name__)


class DropOldestQueue(queue.Queue):
    """Nonblocking producers retain recent work; closing rejects all later work."""

    def __init__(self, capacity, name):
        if capacity <= 0:
            raise ValueError("Audio queue capacity must be positive")
        super().__init__(maxsize=capacity)
        self.name = name
        self._dropped_count = 0
        self._closed = False

    @property
    def dropped_count(self):
        with self.mutex:
            return self._dropped_count

    def put(self, item, block=True, timeout=None):
        """Atomically replace the oldest pending item instead of waiting for space."""
        # Match Queue.put's call shape, but this queue never waits for capacity.
        del block, timeout
        dropped = 0
        with self.not_empty:
            if self._closed:
                return
            if self._qsize() >= self.maxsize:
                self._get()
                self.unfinished_tasks -= 1
                self._dropped_count += 1
                dropped = self._dropped_count
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()
        # Report the first drop and powers of two, avoiding a warning per read.
        if dropped and dropped & (dropped - 1) == 0:
            LOGGER.warning("Audio %s overload: dropped %d oldest pending items", self.name, dropped)

    def close(self):
        """Discard pending work and prohibit enqueueing after shutdown."""
        with self.mutex:
            self._closed = True
            self.unfinished_tasks -= self._qsize()
            self.queue.clear()
            if not self.unfinished_tasks:
                self.all_tasks_done.notify_all()


class UtteranceChunker:
    """Keep silence pre-roll and join bounded chunks only at speech boundaries."""

    def __init__(self):
        self._chunks = deque()
        self.buffered_samples = 0
        self._silent_count = 0
        self._has_speech = False

    def reset(self):
        self._chunks.clear()
        self.buffered_samples = 0
        self._silent_count = 0
        self._has_speech = False

    def add(self, chunk):
        """Consume one mono capture chunk and return an utterance, if ready."""
        import numpy as np  # pylint: disable=import-outside-toplevel

        if chunk.ndim != 1 or not 0 < len(chunk) <= int(SAMPLE_RATE * CAPTURE_CHUNK):
            raise ValueError("Expected a nonempty mono audio chunk of at most 0.25 seconds")
        self._chunks.append(chunk)
        self.buffered_samples += len(chunk)
        if np.abs(chunk).mean() < SILENCE_THRESHOLD:
            self._silent_count = min(self._silent_count + 1, SILENCE_CHUNKS_TO_SPLIT)
        else:
            self._silent_count = 0
            self._has_speech = True
        if not self._has_speech:
            while len(self._chunks) > SILENCE_CHUNKS_TO_SPLIT:
                self.buffered_samples -= len(self._chunks.popleft())
            return None

        natural_break = (self._silent_count >= SILENCE_CHUNKS_TO_SPLIT
                         and self.buffered_samples >= SAMPLE_RATE * MIN_UTTERANCE)
        forced_break = self.buffered_samples >= SAMPLE_RATE * MAX_UTTERANCE
        if not (natural_break or forced_break):
            return None
        audio = np.concatenate(self._chunks)
        if forced_break:
            window = audio[:SAMPLE_RATE * MAX_UTTERANCE]
            cut = len(window) if natural_break else self.find_quietest_cut(window)
            # Copy the remainder so it cannot retain an already emitted array.
            tail = audio[cut:].copy()
            self.reset()
            if len(tail):
                self._chunks.append(tail)
                self.buffered_samples = len(tail)
                self._has_speech = True
            return audio[:cut].copy()
        self.reset()
        return audio

    @staticmethod
    def find_quietest_cut(audio):
        """Cut after the quietest 50 ms window in the last second."""
        import numpy as np  # pylint: disable=import-outside-toplevel

        win, lookback = 2400, SAMPLE_RATE
        region = audio[-lookback:]
        windows = region[:len(region) // win * win].reshape(-1, win)
        quietest = int(np.argmin(np.abs(windows).mean(axis=1)))
        return len(audio) - len(region) + (quietest + 1) * win
