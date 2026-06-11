import asyncio
import importlib.util
import threading

import pytest

from harness.agui import merge_status, status_to_agui
from harness.status import StatusBus, StatusEvent

_HAS_AGUI = importlib.util.find_spec("ag_ui") is not None


def _tag(status_event):
    # a fake to_event mapper so the overlay is testable without ag_ui
    return ("status", status_event.message)


def test_merge_status_yields_source_events_in_order():
    bus = StatusBus()

    async def source():
        yield "A"
        yield "B"

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    assert [e for e in out if isinstance(e, str)] == ["A", "B"]


def test_merge_status_interleaves_bus_events():
    bus = StatusBus()

    async def source():
        bus.emit(StatusEvent(tool="t", message="mid-1"))
        yield "A"
        await asyncio.sleep(0)                      # let the scheduled put run
        bus.emit(StatusEvent(tool="t", message="mid-2"))
        yield "B"
        await asyncio.sleep(0)

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    statuses = [e for e in out if isinstance(e, tuple)]
    assert ("status", "mid-1") in statuses
    assert ("status", "mid-2") in statuses


def test_merge_status_handles_emit_from_worker_thread():
    bus = StatusBus()

    async def source():
        t = threading.Thread(target=lambda: bus.emit(StatusEvent(tool="bg", message="from-thread")))
        t.start()
        t.join()
        yield "X"
        await asyncio.sleep(0)

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    assert ("status", "from-thread") in out          # cross-thread marshaling worked


def test_merge_status_unsubscribes_on_exit():
    bus = StatusBus()

    async def source():
        yield "A"

    async def run():
        async for _ in merge_status(source(), bus, to_event=_tag):
            pass

    asyncio.run(run())
    assert bus._subscribers == []                    # sink removed in finally


def test_merge_status_emitted_during_final_event_is_not_lost():
    bus = StatusBus()

    async def source():
        yield "A"
        bus.emit(StatusEvent(tool="t", message="final"))   # emitted during the LAST event, no trailing await

    out = []

    async def run():
        async for ev in merge_status(source(), bus, to_event=_tag):
            out.append(ev)

    asyncio.run(run())
    assert ("status", "final") in out


@pytest.mark.skipif(not _HAS_AGUI, reason="needs the agui extra (ag-ui-protocol)")
def test_status_to_agui_maps_fields():
    ev = status_to_agui(StatusEvent(tool="run_python", message="running", current=1, total=3))
    assert ev.name == "harness.status"
    assert ev.value == {"tool": "run_python", "message": "running", "current": 1, "total": 3}
