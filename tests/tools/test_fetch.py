import httpx
import pytest

from harness import HarnessConfig, Session
from harness.tools.fetch import fetch_url


def _session(tmp_path):
    return Session.create(HarnessConfig(root_dir=tmp_path / "r"))


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_json_returns_json_handle(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"hello": "world"})

    summary = fetch_url(sess, "https://example.com/data", client=_client(handler))
    assert summary["kind"] == "json"
    assert sess.store.get(summary["id"]) == {"hello": "world"}


def test_fetch_text_returns_text_handle(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, text="plain body", headers={"content-type": "text/plain"})

    summary = fetch_url(sess, "https://example.com/page", client=_client(handler))
    assert summary["kind"] == "text"
    assert sess.store.get(summary["id"]) == "plain body"


def test_fetch_rejects_disallowed_scheme(tmp_path):
    sess = _session(tmp_path)
    with pytest.raises(ValueError, match="scheme"):
        fetch_url(sess, "file:///etc/passwd")


def test_fetch_rejects_body_over_max_bytes(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    cfg.fetch.max_bytes = 5
    sess = Session.create(cfg)

    def handler(request):
        return httpx.Response(200, text="this is way too long", headers={"content-type": "text/plain"})

    with pytest.raises(ValueError, match="exceeds"):
        fetch_url(sess, "https://example.com/big", client=_client(handler))


def test_fetch_returns_structured_error_on_http_error(tmp_path):
    # 4xx/5xx is data the model can adapt to, not a crash.
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(403, text="forbidden")

    out = fetch_url(sess, "https://example.com/blocked", client=_client(handler))
    assert out["status"] == 403
    assert "error" in out
    assert out["url"] == "https://example.com/blocked"
    assert sess.store.manifest_handles() == {}  # nothing stored on error


def test_fetch_returns_structured_error_on_network_failure(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        raise httpx.ConnectError("boom")

    out = fetch_url(sess, "https://example.com/down", client=_client(handler))
    assert out["status"] is None
    assert "request failed" in out["error"]


def test_fetch_follows_redirects(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://example.com/new"})
        return httpx.Response(200, text="final page", headers={"content-type": "text/plain"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    summary = fetch_url(sess, "https://example.com/old", client=client)
    assert summary["kind"] == "text"
    assert sess.store.get(summary["id"]) == "final page"


def test_default_client_has_user_agent_and_follows_redirects(tmp_path):
    from harness.tools.fetch import _default_client

    c = _default_client(HarnessConfig().fetch)
    try:
        assert c.follow_redirects is True
        assert "Mozilla" in c.headers["user-agent"]
    finally:
        c.close()
