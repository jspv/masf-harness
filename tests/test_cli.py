from harness.cli import build_parser, run_cli
from harness.testing import StubChatClient, tool_call, text


def test_parser_reads_problem_and_model(tmp_path):
    args = build_parser().parse_args(["do a thing", "--model", "gpt-4o-mini"])
    assert args.problem == "do a thing"
    assert args.model == "gpt-4o-mini"


def test_run_cli_prints_answer(tmp_path, capsys):
    client = StubChatClient([tool_call("write_file", {"path": "a.txt", "content": "x"}),
                             text("Done: wrote a.txt.")])
    code = run_cli(["write a file", "--root", str(tmp_path / "r")], client=client)
    assert code == 0
    out = capsys.readouterr().out
    assert "Done: wrote a.txt" in out
