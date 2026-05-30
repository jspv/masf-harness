import asyncio
from types import SimpleNamespace

import pandas as pd

from harness import HarnessConfig, Session
from harness.spill import _should_spill, make_spill_middleware


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def _run_mw(mw, function_name, result):
    """Drive the middleware: call_next sets the tool result, then mw may rewrite it."""
    ctx = SimpleNamespace(function=SimpleNamespace(name=function_name), result=None)

    async def call_next():
        ctx.result = result

    asyncio.run(mw(ctx, call_next))
    return ctx.result


def test_large_dict_is_spilled_to_handle_summary(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    big = {"rows": list(range(5000))}  # well over the default 8 KB threshold
    out = _run_mw(mw, "query_data", big)
    assert out["kind"] == "json"          # replaced with a handle summary
    assert "id" in out
    assert sess.store.get(out["id"]) == big  # full data preserved on disk


def test_dataframe_result_is_always_spilled(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    df = pd.DataFrame({"a": [1, 2, 3]})
    out = _run_mw(mw, "query_df", df)
    assert out["kind"] == "dataframe"
    pd.testing.assert_frame_equal(sess.store.get(out["id"]), df)


def test_small_result_passes_through_unchanged(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    out = _run_mw(mw, "add", 42)
    assert out == 42
    assert sess.store.manifest_handles() == {}  # nothing spilled


def test_existing_handle_summary_is_not_respilled(tmp_path):
    sess = _session(tmp_path)
    mw = make_spill_middleware(sess)
    h = sess.store.put({"x": 1}, source="seed")
    summary = h.summary()
    out = _run_mw(mw, "fetch_url", summary)  # already a handle summary
    assert out == summary
    assert len(sess.store.manifest_handles()) == 1  # no new handle created


def test_should_spill_thresholds(tmp_path):
    sess = _session(tmp_path)
    assert _should_spill("x" * 10000, sess.config.spill_threshold_bytes) is True
    assert _should_spill("small", sess.config.spill_threshold_bytes) is False
    assert _should_spill(42, sess.config.spill_threshold_bytes) is False
