"""Bounded, in-process caption scheduling and retained artifact ownership."""

import asyncio
import logging
import os
import queue
import re
import shutil
import threading
import time
from urllib.parse import quote

from starlette.responses import StreamingResponse


class CaptionDownloadLease:
    """One open published artifact; reads and finalization share an I/O lock."""

    def __init__(self, manager, job_id, stream):
        self.manager = manager
        self.job_id = job_id
        self.stream = stream
        self.size = os.fstat(stream.fileno()).st_size
        self._remaining = self.size
        self._io_lock = threading.Lock()
        self._closed = False

    def read(self):
        with self._io_lock:
            if self._closed or not self._remaining:
                return b""
            chunk = self.stream.read(min(65536, self._remaining))
            if not chunk:
                raise OSError("Caption file ended before its advertised length")
            self._remaining -= len(chunk)
            return chunk

    def close(self):
        with self._io_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.stream.close()
            except OSError:
                logging.exception("Could not close caption download %s", self.job_id)
            finally:
                self.manager.release_download(self.job_id)


class CaptionDownloadResponse(StreamingResponse):
    """Keep the acquisition lease until sending ends, fails, or is canceled."""

    def __init__(self, lease, filename):
        self.lease = lease
        encoded_filename = quote(filename)
        disposition = (f"attachment; filename*=utf-8''{encoded_filename}"
                       if encoded_filename != filename else f'attachment; filename="{filename}"')
        super().__init__(self._chunks(), media_type="application/x-subrip", headers={
            "Content-Disposition": disposition, "Content-Length": str(lease.size),
        })

    async def _chunks(self):
        while chunk := await asyncio.to_thread(self.lease.read):
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A canceled thread-backed read keeps the I/O lock until it returns.
            # Closing waits off-loop and releases ownership exactly once.
            await asyncio.shield(asyncio.to_thread(self.lease.close))


class CaptionJobManager:  # pylint: disable=too-many-instance-attributes
    """Reserve upload capacity before storage and run a fixed worker pool."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.jobs = {}
        self._lock = threading.RLock()
        self._occupied = set()
        self._retired = set()
        self._cleaning = set()
        self._downloads = {}
        self._sweep_lock = threading.Lock()
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
            if self._stopped.is_set():
                raise RuntimeError("Runtime stopped")
            if job_id in self._occupied | self._retired | self._cleaning or job_id in self.jobs:
                return False
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

    def open_download(self, job_id, filename):
        """Claim a retained published subtitle, then open it outside the manager lock."""
        with self._lock:
            self._retire_expired(time.time() - self.runtime.config.caption_retention_seconds)
            job = self.jobs.get(job_id)
            if (not job or job.get("status") != "done" or filename not in job.get("files", [])
                    or job_id in self._cleaning):
                raise FileNotFoundError("Published caption file is not retained")
            self._downloads[job_id] = self._downloads.get(job_id, 0) + 1
        stream = None
        try:
            stream = (self.runtime.captions_dir / job_id / filename).open("rb")
            return CaptionDownloadLease(self, job_id, stream)
        except BaseException:
            try:
                if stream is not None:
                    stream.close()
            finally:
                self.release_download(job_id)
            raise

    def release_download(self, job_id):
        with self._lock:
            self._downloads[job_id] -= 1
            if not self._downloads[job_id]:
                del self._downloads[job_id]

    async def download_response(self, job_id, filename):
        operation = asyncio.create_task(asyncio.to_thread(self.open_download, job_id, filename))
        try:
            lease = await asyncio.shield(operation)
        except BaseException:
            # The opening thread may finish after request cancellation. Its result
            # still has an owner that closes it without blocking the event loop.
            operation.add_done_callback(self._close_abandoned_download)
            raise
        return CaptionDownloadResponse(lease, filename)

    @staticmethod
    def _close_abandoned_download(operation):
        try:
            lease = operation.result()
        except BaseException:  # pylint: disable=broad-exception-caught
            return
        asyncio.get_running_loop().run_in_executor(None, lease.close)

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
            try:
                self.sweep()
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("Caption retention sweep failed; will retry")

    def _remove_directory(self, job_id):
        path = self.runtime.captions_dir / job_id
        if (self.runtime.captions_dir.is_symlink() or
                not re.fullmatch(r"[0-9a-f]{12}", job_id) or path.is_symlink()):
            return
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass

    def _retire_expired(self, cutoff):
        """Retire records immediately without waiting for filesystem cleanup."""
        with self._lock:
            for job_id, job in list(self.jobs.items()):
                if (job_id not in self._occupied and job.get("status") in ("done", "error")
                        and job.get("completed_at", float("inf")) <= cutoff):
                    self._retired.add(job_id)
                    del self.jobs[job_id]

    def _clean_unowned(self, job_id):
        # The claim prevents a reservation from reusing the ID until deletion finishes.
        # Filesystem work never holds the scheduling lock.
        with self._lock:
            if (job_id in self.jobs or job_id in self._occupied or job_id in self._cleaning
                    or job_id in self._downloads):
                return
            self._cleaning.add(job_id)
            self._retired.add(job_id)
        try:
            self._remove_directory(job_id)
        except OSError:
            logging.exception("Could not remove caption directory %s; will retry", job_id)
        else:
            with self._lock:
                self._retired.discard(job_id)
        finally:
            with self._lock:
                self._cleaning.discard(job_id)

    def sweep(self):
        """Retire state briefly under lock; clean claimed paths outside that lock."""
        cutoff = time.time() - self.runtime.config.caption_retention_seconds
        self._retire_expired(cutoff)
        # Concurrent requests can retire records without waiting for a slow sweep.
        if not self._sweep_lock.acquire(blocking=False):  # pylint: disable=consider-using-with
            return
        try:
            with self._lock:
                retired = list(self._retired)
            for job_id in retired:
                self._clean_unowned(job_id)
            root = self.runtime.captions_dir
            if not root.is_dir() or root.is_symlink():
                return
            for path in root.iterdir():
                if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{12}", path.name):
                    continue
                try:
                    if path.is_dir() and path.stat().st_mtime <= cutoff:
                        self._clean_unowned(path.name)
                except OSError:
                    logging.exception("Could not remove orphan caption directory %s", path.name)
        except OSError:
            logging.exception("Could not inspect caption directories; will retry")
        finally:
            self._sweep_lock.release()
