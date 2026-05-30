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


def test_rg_and_python_backends_agree(tmp_path):
    # The two backends must return identical results (guards env-dependent drift):
    # one hit per matching line, char-offset col, same text.
    from harness.tools.files import write_file

    sess = _session(tmp_path)
    write_file(sess, "p.txt", "xx xx xx\naaé match here\nnothing\nxx again\n")

    rg_hits = search(sess, "xx")  # rg path (installed on this machine)
    py_hits = _search_python(sess.root, sess.root, "xx", ignore_case=False, max_matches=100)
    assert rg_hits == py_hits

    # Non-ASCII line: col is a CHARACTER offset on both backends.
    rg_m = search(sess, "match")
    py_m = _search_python(sess.root, sess.root, "match", ignore_case=False, max_matches=100)
    assert rg_m == py_m
    assert rg_m[0]["col"] == 4  # 'aaé ' is 4 characters


def test_long_matching_line_is_clipped(tmp_path):
    # Minified-HTML style: a single enormous line. The hit text must be bounded so a
    # search result can't flood the model context.
    from harness.tools.files import write_file

    sess = _session(tmp_path)
    write_file(sess, "min.html", "x" * 100_000 + "NEEDLE" + "y" * 100_000)
    hits = search(sess, "NEEDLE")
    assert len(hits) == 1
    assert len(hits[0]["text"]) <= 300        # clipped, not 200k chars
    assert "NEEDLE" in hits[0]["text"]         # window is centered on the match
    assert hits[0]["text"].startswith("…")     # truncation markers present


def test_one_hit_per_line_even_with_multiple_matches(tmp_path):
    from harness.tools.files import write_file

    sess = _session(tmp_path)
    write_file(sess, "m.txt", "xx xx xx\n")
    assert len(search(sess, "xx")) == 1  # one hit for the line, not three


def test_invalid_regex_raises_valueerror(tmp_path):
    from harness.tools.files import write_file

    sess = _session(tmp_path)
    write_file(sess, "a.txt", "hello\n")
    with pytest.raises(ValueError):
        search(sess, "(unclosed")  # invalid on both rg and Python backends
