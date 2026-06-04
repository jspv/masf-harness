from harness.cli import run_cli
from harness.testing import StubChatClient, text, tool_call


def test_cli_prints_answer_and_session(tmp_path, capsys):
    client = StubChatClient([
        tool_call("list_files", {"path": "."}),
        text("all done"),
    ])
    code = run_cli(
        [str("Summarize the workspace."), "--root", str(tmp_path / "r")],
        client=client,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "all done" in out
    assert "[session:" in out
