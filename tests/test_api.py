import asyncio

from tether import TetherConfig, Tether, report_progress
from tether.testing import StubChatClient, text, tool_call


def test_solve_is_ephemeral_by_default(tmp_path):
    client = StubChatClient([text("the answer")])
    h = Tether(TetherConfig(root_dir=tmp_path / "r"), client=client)
    result = h.solve("go")
    assert result.final_text == "the answer"
    assert not (tmp_path / "r").exists()             # one-shot reaped its workspace


def test_solve_keep_retains_workspace(tmp_path):
    client = StubChatClient([text("the answer")])
    h = Tether(TetherConfig(root_dir=tmp_path / "k"), client=client)
    result = h.solve("go", keep=True)
    assert (tmp_path / "k").exists()                 # audit trail retained
    assert result.session_dir == tmp_path / "k"


def test_aopen_returns_persistent_conversation(tmp_path):
    import asyncio

    async def run():
        h = Tether(TetherConfig(root_dir=tmp_path / "base"), client=StubChatClient([text("x")]))
        conv = await h.aopen("t1")
        same = await h.aopen("t1")
        assert conv is same                          # open-or-create via the manager
        await h.aclose_sessions()
        return conv

    asyncio.run(run())


def noisy(n: int) -> str:
    """Emit two progress updates, then return."""
    report_progress("step A", tool="noisy", current=1, total=2)
    report_progress("step B", tool="noisy", current=2, total=2)
    return "ok"


def test_on_status_receives_tool_events_end_to_end(tmp_path):
    events = []
    client = StubChatClient([tool_call("noisy", {"n": 1}), text("done")])
    h = Tether(TetherConfig(root_dir=tmp_path / "s"), client=client,
                tools=[noisy], on_status=events.append)
    result = h.solve("go")
    assert result.final_text == "done"
    seen = [(e.tool, e.message, e.current, e.total) for e in events]
    assert ("noisy", "step A", 1, 2) in seen
    assert ("noisy", "step B", 2, 2) in seen
    assert [e.seq for e in events] == sorted(e.seq for e in events)   # ordered


def test_on_status_can_be_passed_per_call(tmp_path):
    events = []
    client = StubChatClient([tool_call("noisy", {"n": 1}), text("done")])
    h = Tether(TetherConfig(root_dir=tmp_path / "s2"), client=client, tools=[noisy])
    h.solve("go", on_status=events.append)                # per-call sets the sink
    assert any(e.message == "step A" for e in events)


def test_raising_on_status_does_not_break_the_run(tmp_path):
    def boom(event):
        raise RuntimeError("subscriber boom")

    client = StubChatClient([tool_call("noisy", {"n": 1}), text("done")])
    h = Tether(TetherConfig(root_dir=tmp_path / "s3"), client=client,
                tools=[noisy], on_status=boom)
    result = h.solve("go")
    assert result.final_text == "done"                    # status is best-effort; run completes
    assert result.error is None


def _client():
    return StubChatClient([
        tool_call("fetch_big", {"n": 500}),
        text("the answer"),
    ])


def fetch_big(n: int) -> dict:
    """Return a big payload."""
    return {"rows": list(range(n))}


def test_solve_sync_returns_answer_and_spills(tmp_path):
    cfg = TetherConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64)
    h = Tether(cfg, client=_client(), tools=[fetch_big])
    result = h.solve("go")
    assert result.final_text == "the answer"
    assert result.handles  # developer tool spilled
    assert result.error is None


def test_asolve_matches_solve(tmp_path):
    cfg = TetherConfig(root_dir=tmp_path / "r2", spill_threshold_bytes=64)
    h = Tether(cfg, client=_client(), tools=[fetch_big])
    result = asyncio.run(h.asolve("go"))
    assert result.final_text == "the answer"
    assert result.handles


class _FailingClient(StubChatClient):
    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        raise RuntimeError("model boom")


def test_solve_preserves_error_on_agent_failure(tmp_path):
    cfg = TetherConfig(root_dir=tmp_path / "err")
    h = Tether(cfg, client=_FailingClient([text("unused")]), tools=[])
    result = h.solve("go", keep=True)       # keep retains the work-so-far / audit trail
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert result.final_text == ""
    assert result.session_dir.exists()      # work-so-far / audit trail preserved
