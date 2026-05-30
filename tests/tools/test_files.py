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


def test_write_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        write_file(sess, "../escape.txt", "x")


def test_read_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        read_file(sess, "/etc/passwd")


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
