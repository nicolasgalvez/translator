"""Bounded, in-process caption scheduling and retained artifact ownership."""

import logging
import queue
import re
import shutil
import threading
import time


class CaptionJobManager:  # pylint: disable=too-many-instance-attributes
    """Reserve upload capacity before storage and run a fixed worker pool."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.jobs = {}
        self._lock = threading.RLock()
        self._occupied = set()
        self._stopped = threading.Event()
        self._started = False
        self._queue = queue.Queue(maxsize=(runtime.config.caption_concurrency +
                                          runtime.config.caption_queue_capacity))

    def start(self):
        with self._lock:
            if self._stopped.is_set():
                raise RuntimeError("Runtime stopped")
            if self._started:
                return
            self.sweep()
            workers = []
            try:
                for index in range(self.runtime.config.caption_concurrency):
                    worker = threading.Thread(target=self._work, daemon=True,
                                              name=f"caption-worker-{index}")
                    worker.start()
                    workers.append(worker)
                sweeper = threading.Thread(target=self._sweep_loop, daemon=True,
                                           name="caption-retention")
                sweeper.start()
                workers.append(sweeper)
            except BaseException:
                self._stopped.set()
                raise
            finally:
                self.runtime.worker_threads.extend(workers)
            self._started = True

    def reserve(self, job_id):
        with self._lock:
            self.sweep()
            if self._stopped.is_set():
                raise RuntimeError("Runtime stopped")
            if len(self._occupied) >= self._queue.maxsize:
                return False
            self._occupied.add(job_id)
            return True

    def submit(self, job_id, video_path, original_filename):
        with self._lock:
            self.start()
            if job_id not in self._occupied:
                raise RuntimeError("Caption upload reservation was canceled")
            self.jobs[job_id] = {
                "status": "queued", "progress": 0, "message": "Queued...",
                "files": [], "original_filename": original_filename,
            }
            self._queue.put_nowait((job_id, video_path))

    def cancel_upload(self, job_id):
        with self._lock:
            self.jobs.pop(job_id, None)
            self._occupied.discard(job_id)

    def complete(self, job_id):
        with self._lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job["completed_at"] = time.time()
            self._occupied.discard(job_id)

    def stop(self):
        with self._lock:
            self._stopped.set()
            while True:
                try:
                    job_id, _path = self._queue.get_nowait()
                except queue.Empty:
                    break
                self.jobs[job_id].update(status="error", message="Runtime stopped")
                self.complete(job_id)
                self._queue.task_done()
            # Upload handlers keep reservations until their own cleanup finishes.

    def _work(self):
        while not self._stopped.is_set():
            try:
                job_id, video_path = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.runtime.caption_worker(job_id, video_path)
            finally:
                self._queue.task_done()

    def _sweep_loop(self):
        interval = min(60, self.runtime.config.caption_retention_seconds)
        while not self._stopped.wait(interval):
            self.sweep()

    def _remove_directory(self, job_id):
        path = self.runtime.captions_dir / job_id
        if (self.runtime.captions_dir.is_symlink() or
                not re.fullmatch(r"[0-9a-f]{12}", job_id) or path.is_symlink()):
            return
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass

    def sweep(self):
        """Remove expired records and only old, generated, unowned directories."""
        with self._lock:
            cutoff = time.time() - self.runtime.config.caption_retention_seconds
            for job_id, job in list(self.jobs.items()):
                if (job_id not in self._occupied and job.get("status") in ("done", "error")
                        and job.get("completed_at", float("inf")) <= cutoff):
                    try:
                        self._remove_directory(job_id)
                    except OSError:
                        logging.exception("Could not expire caption job %s", job_id)
                    del self.jobs[job_id]
            root = self.runtime.captions_dir
            if not root.is_dir() or root.is_symlink():
                return
            for path in root.iterdir():
                if (path.name in self.jobs or path.name in self._occupied or path.is_symlink()
                        or not re.fullmatch(r"[0-9a-f]{12}", path.name)):
                    continue
                try:
                    if path.is_dir() and path.stat().st_mtime <= cutoff:
                        self._remove_directory(path.name)
                except OSError:
                    logging.exception("Could not remove orphan caption directory %s", path.name)
