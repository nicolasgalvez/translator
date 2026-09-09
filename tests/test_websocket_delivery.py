"""Isolated, bounded transcript WebSocket delivery."""

import asyncio
import importlib
import json
import queue

import pytest


class RecordingSocket:  # pylint: disable=too-many-instance-attributes
    """A complete socket boundary with observable send and close behavior."""

    def __init__(self, *, send_gate=None, send_error=None, close_gate=None):
        self.send_gate = send_gate
        self.send_error = send_error
        self.close_gate = close_gate
        self.send_started = asyncio.Event()
        self.deliveries = asyncio.Queue()
        self.close_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_code = None

    async def send_text(self, message):
        self.send_started.set()
        if self.send_error is not None:
            raise self.send_error
        if self.send_gate is not None:
            await self.send_gate.wait()
        await self.deliveries.put(message)

    async def close(self, code=1000):
        self.close_code = code
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed.set()


class DelayedCancellationQueue(asyncio.Queue):
    """Model Python 3.11 delaying cancellation of an empty queue waiter."""

    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)
        self.waiting = asyncio.Event()

    async def get(self):
        self.waiting.set()
        try:
            return await super().get()
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            return await super().get()


class CloseReleasedCancellationSocket(RecordingSocket):
    """Keep a canceled send alive until socket close releases the transport."""

    async def send_text(self, message):
        self.send_started.set()
        try:
            await self.send_gate.wait()
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()
            await self.send_gate.wait()
        await self.deliveries.put(message)

    async def close(self, code=1000):
        self.close_code = code
        self.close_started.set()
        self.send_gate.set()
        self.closed.set()


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "bad", ""])
def test_websocket_delivery_capacity_rejects_invalid_configuration(value):
    module = importlib.import_module("translator_runtime")

    with pytest.raises(ValueError, match="TRANSLATOR_WEBSOCKET_QUEUE_CAPACITY"):
        module.RuntimeConfig.from_environment({"TRANSLATOR_WEBSOCKET_QUEUE_CAPACITY": value})


def test_websocket_delivery_capacity_has_a_bounded_default():
    module = importlib.import_module("translator_runtime")

    assert module.RuntimeConfig.from_environment({}).websocket_queue_capacity == 32


def test_blocked_client_does_not_delay_ordered_healthy_delivery():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event()
        slow = RecordingSocket(send_gate=gate)
        healthy = RecordingSocket()
        registry = module.WebSocketDeliveryRegistry(queue_capacity=4, send_timeout=1)
        registry.register(slow)
        registry.register(healthy)
        try:
            assert registry.broadcast("first") is None
            await asyncio.wait_for(slow.send_started.wait(), 1)
            assert await asyncio.wait_for(healthy.deliveries.get(), 1) == "first"
            registry.broadcast("second")
            assert await asyncio.wait_for(healthy.deliveries.get(), 1) == "second"
            assert slow.deliveries.empty()
        finally:
            gate.set()
            await registry.close_all()

    asyncio.run(exercise())


def test_broadcast_loop_drains_production_events_while_sender_is_blocked():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        drained = asyncio.Event()

        class ObservedQueue(queue.Queue):
            def get_nowait(self):
                item = super().get_nowait()
                if self.empty():
                    drained.set()
                return item

        gate = asyncio.Event()
        slow = RecordingSocket(send_gate=gate)
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({
            "TRANSLATOR_WEBSOCKET_QUEUE_CAPACITY": "4",
        }))
        runtime.text_queue = ObservedQueue()
        runtime.client_deliveries.register(slow)
        for index in range(3):
            runtime.text_queue.put({"id": f"event-{index}", "text": str(index)})
        broadcaster = asyncio.create_task(runtime.broadcast_loop())
        try:
            await asyncio.wait_for(slow.send_started.wait(), 1)
            await asyncio.wait_for(drained.wait(), 1)
            assert runtime.text_queue.empty()
        finally:
            gate.set()
            broadcaster.cancel()
            with pytest.raises(asyncio.CancelledError):
                await broadcaster
            await runtime.client_deliveries.close_all()

    asyncio.run(exercise())


def test_full_delivery_queue_unregisters_and_closes_only_overloaded_client():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event()
        slow = RecordingSocket(send_gate=gate)
        healthy = RecordingSocket()
        registry = module.WebSocketDeliveryRegistry(queue_capacity=1, send_timeout=1)
        slow_session = registry.register(slow)
        registry.register(healthy)
        try:
            registry.broadcast("in flight")
            await asyncio.wait_for(slow.send_started.wait(), 1)
            assert await asyncio.wait_for(healthy.deliveries.get(), 1) == "in flight"
            registry.broadcast("queued")
            assert await asyncio.wait_for(healthy.deliveries.get(), 1) == "queued"
            registry.broadcast("over capacity")
            await asyncio.wait_for(slow.closed.wait(), 1)
            assert slow.close_code == 1013
            assert slow not in registry.websockets
            assert healthy in registry.websockets
            assert await asyncio.wait_for(healthy.deliveries.get(), 1) == "over capacity"
            assert slow_session.sender_task.done()
        finally:
            gate.set()
            await registry.close_all()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode,close_code", [("failure", 1011), ("timeout", 1013)])
def test_failed_or_timed_out_sender_cleans_up_its_session(mode, close_code):
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event() if mode == "timeout" else None
        error = RuntimeError("socket failed") if mode == "failure" else None
        socket = RecordingSocket(send_gate=gate, send_error=error)
        registry = module.WebSocketDeliveryRegistry(queue_capacity=2, send_timeout=0.01)
        session = registry.register(socket)
        registry.broadcast("message")
        await asyncio.wait_for(socket.send_started.wait(), 1)
        await asyncio.wait_for(socket.closed.wait(), 1)
        assert socket.close_code == close_code
        assert socket not in registry.websockets
        assert session.sender_task.done()
        await registry.close_all()

    asyncio.run(exercise())


def test_registry_shutdown_closes_clients_concurrently_and_idempotently():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event()
        sockets = [RecordingSocket(close_gate=gate), RecordingSocket(close_gate=gate)]
        registry = module.WebSocketDeliveryRegistry(queue_capacity=2, send_timeout=1)
        sessions = [registry.register(socket) for socket in sockets]
        closing = asyncio.create_task(registry.close_all())
        try:
            await asyncio.wait_for(asyncio.gather(*(
                socket.close_started.wait() for socket in sockets
            )), 1)
        finally:
            gate.set()
        await asyncio.wait_for(closing, 1)
        assert registry.websockets == []
        assert all(session.sender_task.done() for session in sessions)
        assert all(socket.close_code == 1001 for socket in sockets)
        await registry.close_all()

    asyncio.run(exercise())


def test_registry_shutdown_wakes_sender_when_queue_cancellation_is_delayed():
    delivery = importlib.import_module("websocket_delivery")

    async def exercise():
        socket = RecordingSocket()
        registry = delivery.WebSocketDeliveryRegistry(
            queue_capacity=2, send_timeout=0.05, close_timeout=0.05,
        )
        session = delivery.WebSocketDeliverySession(
            registry, socket, queue_capacity=2,
            send_timeout=0.05, close_timeout=0.05,
        )
        session.queue = DelayedCancellationQueue(maxsize=2)
        registry._sessions[id(socket)] = session  # pylint: disable=protected-access
        session.start()
        await asyncio.wait_for(session.queue.waiting.wait(), 1)

        await asyncio.wait_for(registry.close_all(), 0.25)

        assert socket.closed.is_set()
        assert session.sender_task.done()
        assert not registry._cleanup_tasks  # pylint: disable=protected-access
        assert not registry._background_tasks  # pylint: disable=protected-access

    asyncio.run(exercise())


def test_registry_shutdown_closes_socket_when_active_send_delays_cancellation():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        socket = CloseReleasedCancellationSocket(send_gate=asyncio.Event())
        registry = module.WebSocketDeliveryRegistry(
            queue_capacity=2, send_timeout=10, close_timeout=0.05,
        )
        session = registry.register(socket)
        registry.broadcast("in flight")
        await asyncio.wait_for(socket.send_started.wait(), 1)

        await asyncio.wait_for(registry.close_all(), 0.25)

        assert socket.closed.is_set()
        assert session.sender_task.done()
        assert not registry._cleanup_tasks  # pylint: disable=protected-access
        assert not registry._background_tasks  # pylint: disable=protected-access

    asyncio.run(exercise())


def test_registry_shutdown_closes_a_client_registered_during_cleanup():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event()
        first = RecordingSocket(close_gate=gate)
        late = RecordingSocket()
        registry = module.WebSocketDeliveryRegistry(queue_capacity=2, send_timeout=1)
        registry.register(first)
        closing = asyncio.create_task(registry.close_all())
        try:
            await asyncio.wait_for(first.close_started.wait(), 1)
            late_session = registry.register(late)
        finally:
            gate.set()
        await asyncio.wait_for(closing, 1)
        assert registry.websockets == []
        assert late.closed.is_set()
        assert late.close_code == 1001
        assert late_session.sender_task.done()

    asyncio.run(exercise())


def test_cancelled_unregister_waiter_does_not_cancel_session_cleanup():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event()
        socket = RecordingSocket(close_gate=gate)
        registry = module.WebSocketDeliveryRegistry(queue_capacity=2, send_timeout=1)
        session = registry.register(socket)
        registry.detach(session, 1013)
        await asyncio.wait_for(socket.close_started.wait(), 1)
        unregistering = asyncio.create_task(registry.unregister(session))
        await asyncio.sleep(0)
        unregistering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await unregistering
        gate.set()
        await asyncio.wait_for(socket.closed.wait(), 1)
        await registry.close_all()
        assert session.sender_task.done()
        assert registry.websockets == []

    asyncio.run(exercise())


def test_cancelled_registry_shutdown_can_be_retried_to_finish_cleanup():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        gate = asyncio.Event()
        socket = RecordingSocket(close_gate=gate)
        registry = module.WebSocketDeliveryRegistry(queue_capacity=2, send_timeout=1)
        session = registry.register(socket)
        closing = asyncio.create_task(registry.close_all())
        await asyncio.wait_for(socket.close_started.wait(), 1)

        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert not session._close_task.cancelled()  # pylint: disable=protected-access
        retry = asyncio.create_task(registry.close_all())
        await asyncio.sleep(0)
        assert not retry.done()
        gate.set()
        await asyncio.wait_for(retry, 1)

        assert socket.closed.is_set()
        assert session.sender_task.done()
        assert not registry._cleanup_tasks  # pylint: disable=protected-access
        assert not registry._background_tasks  # pylint: disable=protected-access

    asyncio.run(exercise())


def test_runtime_broadcast_preserves_transcript_message_shape():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        socket = RecordingSocket()
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.client_deliveries.register(socket)
        runtime.text_queue.put({"id": "event-1", "text": "private transcript"})
        broadcaster = asyncio.create_task(runtime.broadcast_loop())
        try:
            delivered = await asyncio.wait_for(socket.deliveries.get(), 1)
            assert json.loads(delivered) == {
                "type": "transcript",
                "event": {"id": "event-1", "text": "private transcript"},
            }
        finally:
            broadcaster.cancel()
            with pytest.raises(asyncio.CancelledError):
                await broadcaster
            await runtime.client_deliveries.close_all()

    asyncio.run(exercise())


def test_runtime_broadcast_delivers_recording_status_through_the_bounded_registry():
    module = importlib.import_module("translator_runtime")

    async def exercise():
        socket = RecordingSocket()
        runtime = module.TranslatorRuntime(module.RuntimeConfig.from_environment({}))
        runtime.client_deliveries.register(socket)
        status = {
            "type": "status",
            "status": "recording-error",
            "message": "Recording stopped; live transcription continues.",
        }
        runtime.text_queue.put(status)
        broadcaster = asyncio.create_task(runtime.broadcast_loop())
        try:
            delivered = await asyncio.wait_for(socket.deliveries.get(), 1)
            assert json.loads(delivered) == status
        finally:
            broadcaster.cancel()
            with pytest.raises(asyncio.CancelledError):
                await broadcaster
            await runtime.client_deliveries.close_all()

    asyncio.run(exercise())
