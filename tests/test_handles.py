import pandas as pd
import pytest

from harness.handles import Handle, HandleStore


def test_put_and_get_json_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put({"a": 1, "b": [1, 2, 3]}, source="tool:x")
    assert h.kind == "json"
    assert store.get(h.id) == {"a": 1, "b": [1, 2, 3]}


def test_put_and_get_text_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put("hello world", source="tool:x")
    assert h.kind == "text"
    assert store.get(h.id) == "hello world"


def test_put_and_get_dataframe_roundtrip(tmp_path):
    store = HandleStore(tmp_path)
    df = pd.DataFrame({"x": [1, 2], "y": [3.0, 4.0]})
    h = store.put(df, source="tool:query")
    assert h.kind == "dataframe"
    assert h.n_rows == 2
    assert h.n_cols == 2
    assert h.schema == {"x": "int64", "y": "float64"}
    pd.testing.assert_frame_equal(store.get(h.id), df)


def test_handle_summary_omits_none_and_includes_preview(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put("abc", source="tool:x")
    summary = h.summary()
    assert summary["id"] == h.id
    assert summary["kind"] == "text"
    assert "preview" in summary
    assert "schema" not in summary  # None fields dropped


def test_ids_are_sequential_and_unique(tmp_path):
    store = HandleStore(tmp_path)
    h1 = store.put("a", source="s")
    h2 = store.put("b", source="s")
    assert (h1.id, h2.id) == ("h1", "h2")


def test_files_are_written_under_root_handles_dir(tmp_path):
    store = HandleStore(tmp_path)
    h = store.put({"a": 1}, source="s")
    assert (tmp_path / h.path).exists()
    assert h.path.startswith("handles/")


def test_get_unknown_id_raises_keyerror_with_message(tmp_path):
    store = HandleStore(tmp_path)
    with pytest.raises(KeyError, match="no handle with id"):
        store.get("nope")


def test_explicit_id_advances_counter_no_collision(tmp_path):
    store = HandleStore(tmp_path)
    store.put("seed", source="s", id="h3")   # explicit id
    nxt = store.put("auto", source="s")       # next auto-id must not reuse h1..h3
    assert nxt.id == "h4"
    assert store.get("h3") == "seed"          # explicit handle not overwritten


def test_register_invalid_record_raises_valueerror(tmp_path):
    store = HandleStore(tmp_path)
    with pytest.raises(ValueError, match="invalid handle record"):
        store.register({"id": "h1", "bogus": True})  # missing required fields


def test_register_external_record_round_trips(tmp_path):
    # Simulates the subprocess helper having written a file + metadata record.
    store = HandleStore(tmp_path)
    (tmp_path / "handles").mkdir(exist_ok=True)
    (tmp_path / "handles" / "h7.txt").write_text("from child")
    rec = {"id": "h7", "kind": "text", "path": "handles/h7.txt",
           "source": "run_python", "bytes": 10, "preview": "from child"}
    h = store.register(rec)
    assert h.id == "h7"
    assert store.get("h7") == "from child"
