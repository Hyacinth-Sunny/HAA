"""Tests for config loading (default file + custom override + env var)."""

from pathlib import Path

import pytest

from haa import config as cfgmod
from haa.config import (
    BudgetConfig,
    Config,
    LLMConfig,
    PipelineConfig,
    ServerConfig,
    StorageConfig,
    TimeoutsConfig,
    load_config,
)


def test_default_file_has_task_values():
    """The bundled default.yaml must reflect the task's requested defaults."""
    c = load_config()  # picks up config/default.yaml at the project root
    assert c.llm.model == "deepseek/deepseek-v4-pro"
    assert c.budget.per_campaign == pytest.approx(10.0)
    assert c.budget.global_limit == pytest.approx(100.0)
    assert c.timeouts.llm == 300
    assert c.timeouts.stage == 3600  # v1.0.2: 600→3600（smoke4 单阶段合法墙钟可超 20min）
    assert c.timeouts.llm_wall == 1800
    assert c.pipeline.max_design_rounds == 3
    assert c.pipeline.max_review_rounds == 3
    assert c.server.host == "0.0.0.0"
    assert c.server.port == 8420


def test_built_in_defaults_when_file_missing(tmp_path):
    """A missing file falls back to the dataclass defaults, not a crash."""
    c = load_config(tmp_path / "does-not-exist.yaml")
    assert isinstance(c, Config)
    assert c.llm.model == LLMConfig.model


def test_custom_yaml_override(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(
        """
llm:
  model: "openai/glm-4"
  max_retries: 5
budget:
  per_campaign: 3.5
  global_limit: 50.0
timeouts:
  llm: 120
  stage: 200
server:
  port: 9999
""",
        encoding="utf-8",
    )
    c = load_config(f)
    assert c.llm.model == "openai/glm-4"
    assert c.llm.max_retries == 5
    assert c.budget.per_campaign == pytest.approx(3.5)
    assert c.budget.global_limit == pytest.approx(50.0)
    # defaults preserved where not overridden
    assert c.pipeline.max_design_rounds == 3
    assert c.server.host == "0.0.0.0"  # default host kept
    assert c.server.port == 9999
    assert c.timeouts.llm == 120
    assert c.timeouts.stage == 200


def test_env_var_overrides_default(monkeypatch, tmp_path):
    f = tmp_path / "env.yaml"
    f.write_text("llm:\n  model: 'from-env'\n", encoding="utf-8")
    monkeypatch.setenv("HAA_CONFIG", str(f))
    assert cfgmod.resolve_config_path() == f
    c = load_config()
    assert c.llm.model == "from-env"


def test_explicit_path_beats_env(monkeypatch, tmp_path):
    env_file = tmp_path / "env.yaml"
    env_file.write_text("llm:\n  model: 'env'\n", encoding="utf-8")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("llm:\n  model: 'explicit'\n", encoding="utf-8")
    monkeypatch.setenv("HAA_CONFIG", str(env_file))
    c = load_config(explicit)
    assert c.llm.model == "explicit"


def test_storage_path_resolution():
    s = StorageConfig(db_path="data/haa.db", campaigns_dir="data/campaigns")
    db = s.resolved_db_path()
    assert db.is_absolute()
    assert db.name == "haa.db"
    # Absolute paths pass through untouched.
    abs_cfg = StorageConfig(db_path="/var/haa/x.db", campaigns_dir="data/campaigns")
    assert abs_cfg.resolved_db_path() == Path("/var/haa/x.db")


def test_unknown_keys_ignored(tmp_path):
    """A stray top-level key must not brick the loader."""
    f = tmp_path / "c.yaml"
    f.write_text(
        "llm:\n  model: 'glm-5.2'\nunknown_section:\n  whatever: true\n",
        encoding="utf-8",
    )
    c = load_config(f)
    assert c.llm.model == "glm-5.2"


def test_provider_options_parsed(tmp_path):
    """llm.provider_options（GLM tool_stream 等）逐字进入 LLMConfig。"""
    f = tmp_path / "glm.yaml"
    f.write_text(
        """
llm:
  model: "openai/glm-5.3-flash"
  api_base: "https://open.bigmodel.cn/api/paas/v4"
  api_key_env: "ZHIPU_API_KEY"
  provider_options:
    tool_stream: true
    thinking: {type: enabled}
    reasoning_effort: medium
""",
        encoding="utf-8",
    )
    c = load_config(f)
    assert c.llm.provider_options["tool_stream"] is True
    assert c.llm.provider_options["thinking"] == {"type": "enabled"}
    assert c.llm.provider_options["reasoning_effort"] == "medium"


def test_glm_flash_config_file_loads():
    """config/glm-flash.yaml 整文件可加载且关键字段在位。"""
    from haa.config import DEFAULT_CONFIG_PATH

    c = load_config(DEFAULT_CONFIG_PATH.parent / "glm-flash.yaml")
    assert c.llm.model == "openai/glm-5.3-flash"
    assert c.llm.api_key_env == "ZHIPU_API_KEY"
    assert c.llm.provider_options.get("tool_stream") is True
    assert c.llm.pricing["input_per_m"] == 0.15
    assert c.llm.pricing["output_per_m"] == 0.50
    assert c.vision.model == "glm-5.3-flash"
