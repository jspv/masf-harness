import asyncio

from harness import HarnessConfig, Session
from harness.agent import build_agent
from harness.testing import StubChatClient, tool_call, text


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_agent_runs_gather_act_verify_over_real_tools(tmp_path):
    sess = _session(tmp_path)
    sess.store.put({"sales": [120, 0, 210, 0, 95]}, source="seed", id="h1")
    script = [
        tool_call("inspect_handle", {"handle_id": "h1"}),
        tool_call("run_python", {"code":
            "from harness_sandbox import load, emit\n"
            "d = load('h1')\n"
            "emit(sum(v for v in d['sales'] if v > 0))\n"}),
        text("The total of valid sales is 425."),
    ]
    agent = build_agent(sess, sess.config, StubChatClient(script))
    resp = asyncio.run(agent.run("Total the valid sales in h1."))
    assert "425" in resp.text


def test_stub_records_tool_results_seen(tmp_path):
    sess = _session(tmp_path)
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "hi"}),
                             text("done")])
    agent = build_agent(sess, sess.config, client)
    asyncio.run(agent.run("write a file"))
    assert (sess.root / "a.txt").read_text() == "hi"
