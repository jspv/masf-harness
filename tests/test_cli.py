from harness.cli import build_parser, run_cli
from harness.testing import StubChatClient, tool_call, text


def test_parser_reads_problem_and_model(tmp_path):
    args = build_parser().parse_args(["do a thing", "--model", "gpt-5-mini"])
    assert args.problem == "do a thing"
    assert args.model == "gpt-5-mini"


def test_run_cli_prints_answer(tmp_path, capsys):
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "x"}),
                             text("Done: wrote a.txt.")])
    code = run_cli(["write a file", "--root", str(tmp_path / "r")], client=client)
    assert code == 0
    out = capsys.readouterr().out
    assert "Done: wrote a.txt" in out


def test_verbose_prints_tool_calls_and_code(tmp_path, capsys):
    client = StubChatClient([
        tool_call("run_python", {"code": "print('hi from sandbox')"}),
        text("All done."),
    ])
    run_cli(["compute something", "--root", str(tmp_path / "r"), "--verbose"], client=client)
    out = capsys.readouterr().out
    assert "→ run_python(" in out                 # the tool call is shown
    assert "print('hi from sandbox')" in out       # the code is shown
    assert "← " in out                             # the result line is shown
    assert "All done." in out                      # final answer still printed
