from harness.config import FetchConfig, HarnessConfig, SandboxConfig


def test_defaults_are_sensible():
    cfg = HarnessConfig()
    assert cfg.model == "gpt-5-mini"
    assert cfg.spill_threshold_bytes == 8192
    assert isinstance(cfg.sandbox, SandboxConfig)
    assert isinstance(cfg.fetch, FetchConfig)
    assert cfg.sandbox.timeout_s == 30.0
    assert "pandas" in cfg.sandbox.preinstalled
    assert cfg.fetch.allowed_schemes == ("http", "https")


def test_nested_configs_are_independent_between_instances():
    a = HarnessConfig()
    b = HarnessConfig()
    assert a.sandbox is not b.sandbox  # field(default_factory=...) not shared
