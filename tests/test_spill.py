import asyncio

import pandas as pd

from harness import HarnessConfig, Session
from harness.spill import _should_spill, wrap_external_tool


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_large_dict_return_is_spilled_to_handle_summary(tmp_path):
    sess = _session(tmp_path)

    def query_data() -> dict:
        "Return a big dataset."
        return {"rows": list(range(5000))}  # well over the default 8 KB threshold

    wrapped = wrap_external_tool(sess, query_data)
    out = wrapped()
    assert out["kind"] == "json"          # replaced with a handle summary
    assert "id" in out
    assert sess.store.get(out["id"]) == {"rows": list(range(5000))}  # full data preserved


def test_dataframe_return_is_always_spilled(tmp_path):
    sess = _session(tmp_path)
    df = pd.DataFrame({"a": [1, 2, 3]})

    def query_df() -> pd.DataFrame:
        "Return a frame."
        return df

    out = wrap_external_tool(sess, query_df)()
    assert out["kind"] == "dataframe"
    pd.testing.assert_frame_equal(sess.store.get(out["id"]), df)


def test_small_return_passes_through_unchanged(tmp_path):
    sess = _session(tmp_path)

    def add() -> int:
        "Add."
        return 42

    out = wrap_external_tool(sess, add)()
    assert out == 42
    assert sess.store.manifest_handles() == {}  # nothing spilled


def test_existing_handle_summary_is_not_respilled(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put({"x": 1}, source="seed")
    summary = h.summary()

    def passthrough() -> dict:
        "Return a handle summary."
        return summary

    out = wrap_external_tool(sess, passthrough)()
    assert out == summary
    assert len(sess.store.manifest_handles()) == 1  # no new handle created


def test_wrapper_preserves_name_and_signature(tmp_path):
    import inspect

    sess = _session(tmp_path)

    def my_tool(query: str, limit: int = 10) -> dict:
        "Docstring here."
        return {}

    wrapped = wrap_external_tool(sess, my_tool)
    assert wrapped.__name__ == "my_tool"
    assert wrapped.__doc__ == "Docstring here."
    assert list(inspect.signature(wrapped).parameters) == ["query", "limit"]


def test_async_tool_is_wrapped_and_spilled(tmp_path):
    sess = _session(tmp_path)

    async def afetch() -> dict:
        "Async source."
        return {"rows": list(range(5000))}

    wrapped = wrap_external_tool(sess, afetch)
    out = asyncio.run(wrapped())
    assert out["kind"] == "json"
    assert sess.store.get(out["id"]) == {"rows": list(range(5000))}


def test_should_spill_thresholds(tmp_path):
    sess = _session(tmp_path)
    assert _should_spill("x" * 10000, sess.config.spill_threshold_bytes) is True
    assert _should_spill("small", sess.config.spill_threshold_bytes) is False
    assert _should_spill(42, sess.config.spill_threshold_bytes) is False
