import pytest

from harness import HarnessConfig, PathEscapesRootError, Session
from harness.tools.files import write_file
from harness.tools.search import _search_python, search


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def _seed(sess):
    write_file(sess, "a.txt", "alpha\nbeta\nGAMMA gamma\n")
    write_file(sess, "sub/b.txt", "delta\nalpha again\n")


def test_search_finds_matches_across_files(tmp_path):
    sess = _session(tmp_path)
    _seed(sess)
    hits = search(sess, "alpha")
    files = {h["file"] for h in hits}
    assert files == {"a.txt", "sub/b.txt"}
    assert all("line" in h and "text" in h for h in hits)


def test_search_can_target_a_single_file(tmp_path):
    sess = _session(tmp_path)
    _seed(sess)
    hits = search(sess, "alpha", path="a.txt")
    assert {h["file"] for h in hits} == {"a.txt"}
    assert hits[0]["line"] == 1


def test_search_ignore_case(tmp_path):
    sess = _session(tmp_path)
    _seed(sess)
    insensitive = search(sess, "gamma", ignore_case=True)
    assert any("GAMMA" in h["text"] for h in insensitive)


def test_search_respects_max_matches(tmp_path):
    sess = _session(tmp_path)
    write_file(sess, "many.txt", "x\n" * 50)
    hits = search(sess, "x", max_matches=5)
    assert len(hits) == 5


def test_search_rejects_path_outside_root(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(PathEscapesRootError):
        search(sess, "x", path="..")


def test_python_fallback_matches_directly(tmp_path):
    # Exercise the fallback regardless of whether rg is installed.
    sess = _session(tmp_path)
    _seed(sess)
    hits = _search_python(sess.root, sess.root, "alpha", ignore_case=False, max_matches=100)
    assert {h["file"] for h in hits} == {"a.txt", "sub/b.txt"}
