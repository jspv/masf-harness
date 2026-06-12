import pandas as pd
import pytest

from tether import TetherConfig, Session
from tether.tools.inspect import inspect_handle


def _session(tmp_path):
    return Session.create(TetherConfig(root_dir=tmp_path / "r"))


def test_inspect_text_handle_returns_more_preview(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put("line\n" * 100, source="s")
    out = inspect_handle(sess, h.id, rows=10)
    assert out["kind"] == "text"
    assert out["preview"].count("line") == 10  # more than the stored summary preview


def test_inspect_dataframe_returns_head_rows(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put(pd.DataFrame({"a": range(50)}), source="s")
    out = inspect_handle(sess, h.id, rows=7)
    assert out["kind"] == "dataframe"
    assert out["n_rows"] == 50
    assert len(out["head"]) == 7


def test_inspect_dataframe_with_stats(tmp_path):
    sess = _session(tmp_path)
    h = sess.store.put(pd.DataFrame({"a": [1, 2, 3, 4]}), source="s")
    out = inspect_handle(sess, h.id, stats=True)
    assert "describe" in out
    assert out["describe"]["a"]["max"] == 4


def test_inspect_unknown_handle_raises(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(KeyError):
        inspect_handle(sess, "nope")
