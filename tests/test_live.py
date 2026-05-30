import os

import pytest

from harness import Harness, HarnessConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE") != "1",
    reason="set HARNESS_LIVE=1 (and OPENAI_API_KEY in .env) to run the live smoke test",
)


def test_live_gather_act_verify(tmp_path):
    cfg = HarnessConfig(root_dir=tmp_path / "r", model="gpt-5-mini")
    h = Harness(cfg)
    h.session.store.put({"sales": [120, 0, 210, 0, 95]}, source="seed", id="h1")
    result = h.solve(
        "Dataset handle 'h1' holds sales numbers. Total the valid (non-zero) sales, "
        "exclude any zero rows, and report the total with a note on what you excluded."
    )
    assert "425" in result.final_text
