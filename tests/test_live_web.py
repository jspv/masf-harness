import os

import pytest

from harness import Harness, HarnessConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE") != "1",
    reason="set HARNESS_LIVE=1 (plus OPENAI_API_KEY and TAVILY_API_KEY in .env) for the live web test",
)


def test_live_web_research_pricing(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", model="gpt-4o-mini")
    result = Harness(cfg).solve(
        "What are the current OpenAI API prices for their flagship model "
        "(input/output per million tokens)? Use web search and cite a source URL."
    )
    assert result.error is None
    assert "$" in result.final_text or "per million" in result.final_text.lower()
