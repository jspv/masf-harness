import inspect

from harness import HarnessConfig, Session
from harness.tools.registry import build_tools


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_build_tools_returns_expected_named_callables(tmp_path):
    tools = build_tools(_session(tmp_path))
    names = {t.__name__ for t in tools}
    assert names == {
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "run_python", "inspect_handle",
    }


def test_wrapped_tools_do_not_expose_session_param(tmp_path):
    tools = {t.__name__: t for t in build_tools(_session(tmp_path))}
    assert "session" not in inspect.signature(tools["read_file"]).parameters
    assert list(inspect.signature(tools["read_file"]).parameters)[0] == "path"


def test_wrapped_tools_keep_docstrings(tmp_path):
    tools = {t.__name__: t for t in build_tools(_session(tmp_path))}
    assert tools["search"].__doc__ and "pattern" in tools["search"].__doc__.lower()


def test_wrapped_tools_actually_work(tmp_path):
    sess = _session(tmp_path)
    tools = {t.__name__: t for t in build_tools(sess)}
    tools["write_file"]("a.txt", "hello\nworld\n")
    assert tools["read_file"]("a.txt") == "hello\nworld\n"
    out = tools["run_python"](code="from harness_sandbox import emit\nemit(5)\n")
    assert out["result"] == 5
    # search via the wrapped closure (regression: the package re-export shadowed the
    # `search` submodule, so the closure called the function as if it were a module).
    hits = tools["search"]("world", path="a.txt")
    assert hits and hits[0]["line"] == 2
    assert tools["list_files"](".") and "a.txt" in tools["list_files"](".")
