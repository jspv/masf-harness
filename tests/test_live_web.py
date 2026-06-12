import os

import pytest

from tether import Tether, TetherConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("TETHER_LIVE") != "1",
    reason="set TETHER_LIVE=1 (plus OPENAI_API_KEY and TAVILY_API_KEY in .env) for the live web test",
)


def test_live_web_research_pricing(tmp_path):
    cfg = TetherConfig(root_dir=tmp_path / "r", model="gpt-5-mini")
    result = Tether(cfg).solve(
        "What are the current OpenAI API prices for their flagship model "
        "(input/output per million tokens)? Use web search and cite a source URL."
    )
    assert result.error is None
    assert "$" in result.final_text or "per million" in result.final_text.lower()
