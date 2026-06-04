import asyncio

from harness import HarnessConfig, Harness
from harness.testing import StubChatClient, text, tool_call


def _client():
    return StubChatClient([
        tool_call("fetch_big", {"n": 500}),
        text("the answer"),
    ])


def fetch_big(n: int) -> dict:
    """Return a big payload."""
    return {"rows": list(range(n))}


def test_solve_sync_returns_answer_and_spills(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64)
    h = Harness(cfg, client=_client(), tools=[fetch_big])
    result = h.solve("go")
    assert result.final_text == "the answer"
    assert result.handles  # developer tool spilled
    assert result.error is None


def test_asolve_matches_solve(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r2", spill_threshold_bytes=64)
    h = Harness(cfg, client=_client(), tools=[fetch_big])
    result = asyncio.run(h.asolve("go"))
    assert result.final_text == "the answer"
    assert result.handles


class _FailingClient(StubChatClient):
    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        raise RuntimeError("model boom")


def test_solve_preserves_error_on_agent_failure(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "err")
    h = Harness(cfg, client=_FailingClient([text("unused")]), tools=[])
    result = h.solve("go")
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert result.final_text == ""
    assert result.session_dir.exists()      # work-so-far / audit trail preserved
