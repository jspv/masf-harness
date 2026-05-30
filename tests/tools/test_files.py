import pytest

from harness import HarnessConfig, PathEscapesRootError, Session
from harness.tools.files import list_files, read_file, write_file


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_write_then_read_roundtrip(tmp_path):
    sess = _session(tmp_path)
    msg = write_file(sess, "notes/a.txt", "hello\nworld\n")
    assert "a.txt" in msg
    assert read_file(sess, "notes/a.txt") == "hello\nworld\n"


def test_read_is_bounded_by_limit_and_offset(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "big.txt", "".join(f"line{i}\n" for i in range(100)))
    out = read_file(sess, "big.txt", offset=10, limit=3)
    assert out == "line10\nline11\nline12\n"


def test_read_file_caps_total_characters_on_long_lines(tmp_path):
    # Minified-HTML style: one enormous line. limit=lines doesn't bound chars, so the
    # char cap must kick in to protect the context window.
    sess = _session(tmp_path)
    write_file(sess, "big.html", "x" * 200_000)  # single 200k-char line
    out = read_file(sess, "big.html")
    assert len(out) <= 50_000 + 200  # cap + truncation note
    assert "truncated" in out.lower()


def test_read_past_eof_returns_empty(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "a.txt", "one\ntwo\n")
    assert read_file(sess, "a.txt", offset=100) == ""


def test_read_rejects_negative_offset_or_limit(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "a.txt", "one\ntwo\n")
    with pytest.raises(ValueError):
        read_file(sess, "a.txt", offset=-1)
    with pytest.raises(ValueError):
        read_file(sess, "a.txt", limit=-1)


def test_write_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        write_file(sess, "../escape.txt", "x")


def test_read_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        read_file(sess, "/etc/passwd")


def test_list_files_empty_path_means_root(tmp_path):
    # A model passing "" for "current dir" should get the root listing, not an error.
    sess = _session(tmp_path)
    write_file(sess, "a.txt", "1")
    assert "a.txt" in list_files(sess, "")


def test_list_files_returns_relative_paths(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "a.txt", "1")
    write_file(sess, "sub/b.txt", "2")
    listing = set(list_files(sess, "."))
    assert "a.txt" in listing
    assert "sub/b.txt" in listing


def test_list_files_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        list_files(sess, "..")
