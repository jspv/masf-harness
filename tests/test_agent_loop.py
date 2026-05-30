from harness import HarnessConfig, Session
from harness.agent import build_agent, run_agent_sync
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
    resp = run_agent_sync(agent, "Total the valid sales in h1.")
    assert "425" in resp.text


def test_stub_records_tool_results_seen(tmp_path):
    sess = _session(tmp_path)
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "hi"}),
                             text("done")])
    agent = build_agent(sess, sess.config, client)
    run_agent_sync(agent, "write a file")
    assert (sess.root / "a.txt").read_text() == "hi"


def test_large_external_tool_result_is_spilled_during_run(tmp_path):
    sess = _session(tmp_path)

    def big_source() -> dict:
        "Return a large dataset."
        return {"rows": list(range(5000))}  # well over the 8 KB spill threshold

    client = StubChatClient([tool_call("big_source", {}), text("Got the data.")])
    agent = build_agent(sess, sess.config, client, extra_tools=[big_source])
    run_agent_sync(agent, "fetch the big dataset")

    # The external tool's large result was spilled to a handle during the run.
    handles = sess.store.manifest_handles()
    assert len(handles) == 1
    (h,) = handles.values()
    assert h.kind == "json"
    assert sess.store.get(h.id) == {"rows": list(range(5000))}
