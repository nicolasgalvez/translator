"""Isolated and bounded transcript delivery to WebSocket clients."""

import asyncio
from contextlib import suppress


_CLOSE = object()


def _consume_task_result(task) -> None:
    """Retrieve a detached task's eventual exception without awaiting it."""
    with suppress(asyncio.CancelledError):
        task.exception()


class WebSocketDeliverySession:
    """Own one client's bounded delivery queue and network sender task."""

    def __init__(self, registry, websocket, queue_capacity, send_timeout, close_timeout):
        self.registry = registry
        self.websocket = websocket
        self.queue = asyncio.Queue(maxsize=queue_capacity)
        self.send_timeout = send_timeout
        self.close_timeout = close_timeout
        self.sender_task = None
        self._close_task = None

    def start(self):
        """Start this session's sole sender task."""
        self.sender_task = asyncio.create_task(self._send_loop())

    def enqueue(self, message: str) -> bool:
        """Enqueue without waiting, returning false when capacity is exhausted."""
        if self._close_task is not None:
            return False
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    def begin_close(self, code: int | None):
        """Start one idempotent sender/socket cleanup task."""
        if self._close_task is None:
            self._discard_pending_messages()
            if self.sender_task is not None and not self.sender_task.done():
                self.queue.put_nowait(_CLOSE)
            self._close_task = asyncio.create_task(self._close(code))
        return self._close_task

    def _discard_pending_messages(self) -> None:
        """Make room for the close wakeup without delivering stale messages."""
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self.queue.task_done()

    async def _send_loop(self):
        try:
            while True:
                message = await self.queue.get()
                try:
                    if message is _CLOSE:
                        return
                    completed = await self.registry.wait_for_socket_io(
                        self.websocket.send_text(message), self.send_timeout,
                    )
                    if not completed:
                        raise asyncio.TimeoutError
                finally:
                    self.queue.task_done()
        except asyncio.TimeoutError:
            self.registry.detach(self, 1013)
        except Exception:  # pylint: disable=broad-exception-caught
            self.registry.detach(self, 1011)

    async def _close(self, code: int | None):
        sender = self.sender_task
        if sender is not None and not sender.done():
            sender.cancel()
            await asyncio.wait((sender,), timeout=self.close_timeout)
        if code is not None:
            with suppress(Exception):  # Network cleanup must not block other clients.
                await self.registry.wait_for_socket_io(
                    self.websocket.close(code=code), self.close_timeout,
                )
        if sender is not None and not sender.done():
            await asyncio.wait((sender,), timeout=self.close_timeout)
        if sender is not None and not sender.done():
            self.registry.observe_background_task(sender)


class WebSocketDeliveryRegistry:
    """Register delivery sessions and fan out messages without socket I/O."""

    def __init__(self, queue_capacity: int, send_timeout: float = 1,
                 close_timeout: float = 1):
        self.queue_capacity = queue_capacity
        self.send_timeout = send_timeout
        self.close_timeout = close_timeout
        self._sessions = {}
        self._cleanup_tasks = set()
        self._background_tasks = set()
        self._closing = False

    def _finish_background_task(self, task) -> None:
        self._background_tasks.discard(task)
        _consume_task_result(task)

    def observe_background_task(self, task) -> None:
        """Own a detached task until completion and retrieve its exception."""
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_background_task)

    async def wait_for_socket_io(self, operation, timeout: float) -> bool:
        """Run socket I/O with a hard wait bound and track delayed cancellation.

        An operation that ignores cancellation may outlive its session, so it
        stays tracked until completion and its eventual exception is retrieved.
        """
        task = asyncio.create_task(operation)
        self.observe_background_task(task)
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
        except BaseException:
            task.cancel()
            raise
        if task not in done:
            task.cancel()
            return False
        task.result()
        return True

    @property
    def websockets(self) -> list:
        """Return a snapshot of currently registered sockets."""
        return [session.websocket for session in self._sessions.values()]

    def register(self, websocket) -> WebSocketDeliverySession:
        """Represent one accepted socket with a started delivery session."""
        key = id(websocket)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing
        session = WebSocketDeliverySession(
            self, websocket, self.queue_capacity, self.send_timeout, self.close_timeout,
        )
        self._sessions[key] = session
        session.start()
        if self._closing:
            self.detach(session, 1001)
        return session

    def broadcast(self, message: str) -> None:
        """Enqueue for every client without awaiting any socket operation."""
        for session in tuple(self._sessions.values()):
            if not session.enqueue(message):
                self.detach(session, 1013)

    def detach(self, session: WebSocketDeliverySession, close_code: int | None):
        """Unregister immediately and begin idempotent bounded cleanup."""
        key = id(session.websocket)
        if self._sessions.get(key) is session:
            del self._sessions[key]
        task = session.begin_close(close_code)
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return task

    async def unregister(self, session: WebSocketDeliverySession) -> None:
        """Stop delivery after a peer disconnect without sending another close frame."""
        await asyncio.shield(self.detach(session, None))

    async def close_all(self) -> None:
        """Concurrently stop and close every registered or closing session."""
        self._closing = True
        while self._sessions or self._cleanup_tasks:
            for session in tuple(self._sessions.values()):
                self.detach(session, 1001)
            tasks = set(self._cleanup_tasks)
            if tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in tasks))
                self._cleanup_tasks.difference_update(tasks)
