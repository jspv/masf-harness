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
