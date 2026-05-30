import httpx

from harness import HarnessConfig, Session
from harness.tools.web import web_search


def _session(tmp_path, api_key="test-key"):
    cfg = HarnessConfig(root_dir=tmp_path / "r")
    cfg.search.api_key = api_key
    return Session.create(cfg)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_web_search_returns_answer_and_results(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        assert request.url.path == "/search"
        return httpx.Response(200, json={
            "answer": "Flagship input is $5/M, output $25/M.",
            "results": [
                {"title": "Pricing", "url": "https://x.com/p", "content": "five dollars", "score": 0.9},
                {"title": "Docs", "url": "https://x.com/d", "content": "twenty five", "score": 0.8},
            ],
        })

    out = web_search(sess, "model pricing", client=_client(handler))
    assert out["answer"].startswith("Flagship")
    assert len(out["results"]) == 2
    assert out["results"][0]["url"] == "https://x.com/p"
    assert out["results"][0]["title"] == "Pricing"


def test_web_search_clips_long_snippets(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"answer": None,
            "results": [{"title": "T", "url": "https://x", "content": "z" * 5000, "score": 1.0}]})

    out = web_search(sess, "q", client=_client(handler))
    assert len(out["results"][0]["content"]) <= 500


def test_web_search_missing_key_returns_error(tmp_path):
    sess = _session(tmp_path, api_key=None)
    out = web_search(sess, "q")
    assert "error" in out
    assert "TAVILY_API_KEY" in out["error"]


def test_web_search_http_error_is_structured(tmp_path):
    sess = _session(tmp_path)

    def handler(request):
        return httpx.Response(401, text="unauthorized")

    out = web_search(sess, "q", client=_client(handler))
    assert out["status"] == 401
    assert "error" in out
