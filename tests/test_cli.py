from harness.cli import make_status_printer, run_cli
from harness.status import StatusEvent
from harness.testing import StubChatClient, text, tool_call


def test_status_printer_formats_event():
    lines = []
    printer = make_status_printer(write=lines.append)
    printer(StatusEvent(tool="read_document", message="converting d.pdf via Docling",
                        current=1, total=4, seq=1, timestamp=1.0))
    assert lines == ["→ read_document: converting d.pdf via Docling [1/4]"]


def test_verbose_prints_tool_status_to_stderr(tmp_path, capsys):
    client = StubChatClient([
        tool_call("run_python", {"code": "from harness_sandbox import emit\nemit(1)\n"}),
        text("done"),
    ])
    code = run_cli(["go", "-v", "--root", str(tmp_path / "r")], client=client)
    err = capsys.readouterr().err
    assert code == 0
    assert "run_python" in err                            # the instrumented tool reported
    assert "temporarily unavailable" not in err           # old notice is gone


def test_cli_prints_answer_and_session(tmp_path, capsys):
    client = StubChatClient([
        tool_call("list_files", {"path": "."}),
        text("all done"),
    ])
    code = run_cli(
        ["Summarize the workspace.", "--root", str(tmp_path / "r")],
        client=client,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "all done" in out
    assert "[session:" in out
