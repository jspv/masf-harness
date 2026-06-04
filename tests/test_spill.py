import pandas as pd

from harness import HarnessConfig, Session
from harness.spill import looks_like_mcp, make_spill_parser, spill_tool


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r", spill_threshold_bytes=64))


def _as_text(parsed) -> str:
    # parse_result returns list[Content]; pull the text out of the first item.
    return parsed[0].text if isinstance(parsed, list) else parsed


def test_parser_spills_oversized_dict_to_handle(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "big_tool")
    big = {"rows": list(range(500))}
    parsed = parse(big)
    text = _as_text(parsed)
    assert '"id"' in text and '"path"' in text          # a handle summary, not raw rows
    assert sess.handles                                  # a handle was created
    assert next(iter(sess.handles.values()))["source"] == "tool:big_tool"


def test_parser_passes_small_result_through(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "tiny")
    parsed = parse({"ok": True})
    assert "ok" in _as_text(parsed)
    assert not sess.handles                              # nothing spilled


def test_parser_spills_dataframe_with_schema(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "frame")
    parse(pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]}))
    summ = next(iter(sess.handles.values()))
    assert summ["kind"] == "dataframe"
    assert summ["schema"] == {"a": "int64", "b": "int64"}


def test_parser_does_not_double_spill_handle_summary(tmp_path):
    sess = _session(tmp_path)
    parse = make_spill_parser(sess, "passthru")
    already = {"id": "h1", "kind": "json", "path": "handles/h1.json"}
    parse(already)
    assert not sess.handles                              # recognized as a summary; no new handle


def test_spill_tool_builds_functiontool_with_name_and_doc(tmp_path):
    sess = _session(tmp_path)

    def my_tool(x: int) -> dict:
        """Returns a big payload."""
        return {"rows": list(range(x))}

    ft = spill_tool(sess, my_tool)
    assert ft.name == "my_tool"
    assert "big payload" in ft.description


def test_looks_like_mcp_detection(tmp_path):
    class FakeMCP:
        functions = []
        async def connect(self): ...
        async def close(self): ...

    def plain(): return 1

    assert looks_like_mcp(FakeMCP())
    assert not looks_like_mcp(plain)
