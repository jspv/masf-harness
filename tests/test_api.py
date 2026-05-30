from harness import Harness, HarnessConfig, Result
from harness.testing import StubChatClient, tool_call, text


def test_solve_returns_result_with_answer_and_session(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    client = StubChatClient([
        tool_call("write_file", {"path": "out.txt", "content": "42"}),
        text("Wrote the answer: 42."),
    ])
    h = Harness(cfg, client=client)
    result = h.solve("write 42 to out.txt")
    assert isinstance(result, Result)
    assert "42" in result.final_text
    assert result.session_dir == h.session.root
    assert (h.session.root / "out.txt").read_text() == "42"


def test_solve_exposes_handles_created_during_the_run(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    client = StubChatClient([
        tool_call("run_python", {"code":
            "from harness_sandbox import save\nsave('out', {'k': 1})\n"}),
        text("Saved handle 'out'."),
    ])
    result = Harness(cfg, client=client).solve("save a handle")
    assert "out" in result.handles
    assert result.handles["out"]["kind"] == "json"


def test_solve_accepts_extra_user_tools(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    calls = {"n": 0}

    def my_source() -> dict:
        "Return some data."
        calls["n"] += 1
        return {"value": 7}

    client = StubChatClient([tool_call("my_source", {}), text("Got it.")])
    Harness(cfg, client=client).solve("call my_source", tools=[my_source])
    assert calls["n"] == 1


def test_solve_reports_tool_calls_via_on_tool_call(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "hi"}),
                             text("done")])
    seen = []
    Harness(cfg, client=client).solve("write a file",
                                      on_tool_call=lambda n, k, r: seen.append((n, k, r)))
    assert len(seen) == 1
    name, kwargs, result = seen[0]
    assert name == "write_file"
    assert kwargs == {"path": "a.txt", "content": "hi"}
    assert "a.txt" in result
