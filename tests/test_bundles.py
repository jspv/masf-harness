from harness import HarnessConfig, Session


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def test_tools_for_code_bundle_includes_core(tmp_path):
    sess = _session(tmp_path)
    names = {t.__name__ for t in sess.tools("code")}
    assert names == {"inspect_handle", "run_python"}  # core + code


def test_tools_default_is_all_bundles(tmp_path):
    sess = _session(tmp_path)
    names = {t.__name__ for t in sess.tools()}
    assert names == {
        "inspect_handle", "run_python",
        "read_file", "write_file", "list_files", "search",
        "fetch_url", "web_search", "web_extract",
    }


def test_harness_instructions_compose_by_bundle(tmp_path):
    sess = _session(tmp_path)
    core_only = sess.harness_instructions()  # always includes core
    assert "handle" in core_only.lower()
    with_code = sess.harness_instructions("code")
    assert "run_python" in with_code
    assert "load(" in with_code
    # web fragment only appears when web is selected
    assert "web_search" not in sess.harness_instructions("code")
    assert "web_search" in sess.harness_instructions("web")
