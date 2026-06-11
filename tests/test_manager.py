import asyncio

import pytest

from harness import HarnessConfig, Harness
from harness.manager import SessionManager
from harness.testing import StubChatClient, text


def _harness(tmp_path):
    return Harness(HarnessConfig(root_dir=tmp_path / "base"), client=StubChatClient([text("x")]))


def test_open_is_reuse_or_create_and_isolated_roots(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path))
        a = await m.aopen("t1")
        a2 = await m.aopen("t1")          # same id -> same Conversation
        b = await m.aopen("t2")           # different id -> different Conversation + root
        assert a is a2
        assert b is not a
        assert a.session.root != b.session.root      # isolated per-id roots
        await m.aclose()

    asyncio.run(run())


def test_get_and_close_are_idempotent(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path))
        c = await m.aopen("t1")
        assert m.get("t1") is c
        await m.close("t1")
        assert m.get("t1") is None
        await m.close("t1")               # idempotent: no error

    asyncio.run(run())


def test_lazy_ttl_expiry(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path), idle_ttl_s=60)
        c1 = await m.aopen("t1")
        c1.last_activity -= 1000          # simulate idle past the TTL
        assert m.get("t1") is None        # lazy expiry on get
        c2 = await m.aopen("t1")          # open re-creates a fresh conversation
        assert c2 is not c1
        await m.aclose()

    asyncio.run(run())


def test_open_rejects_path_traversal_id(tmp_path):
    # An untrusted threadId must not escape the base via separators/.. — it would be rmtree'd on
    # close. The open fails closed (ValueError) before any workspace is created.
    async def run():
        m = SessionManager(_harness(tmp_path))
        for bad in ("../../../etc/evil", "a/b", "..", r"a\b"):
            with pytest.raises(ValueError):
                await m.aopen(bad)
        assert m.get("../../../etc/evil") is None        # nothing was registered
        await m.aclose()

    asyncio.run(run())


def test_sweep_reaps_idle(tmp_path):
    async def run():
        m = SessionManager(_harness(tmp_path), idle_ttl_s=60)
        c = await m.aopen("t1")
        c.last_activity -= 1000
        await m.sweep()
        assert m.get("t1") is None

    asyncio.run(run())
