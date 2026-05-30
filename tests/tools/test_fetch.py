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


def test_fetch_raises_on_http_error_status(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(404, text="nope")

    with pytest.raises(httpx.HTTPStatusError):
        fetch_url(sess, "https://example.com/missing", client=_client(handler))
