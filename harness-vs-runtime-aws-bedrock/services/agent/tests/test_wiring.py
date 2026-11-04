"""The real Strands objects, built without any network: model config, the MCP client factory, settings."""

from __future__ import annotations

from strands.tools.mcp import MCPClient

from app.agent import build_agent, build_mcp_client, build_model, usage_of
from app.settings import DEFAULT_SYSTEM_PROMPT, Settings


def test_model_matches_the_harness_config() -> None:
    model = build_model(Settings(model_id="us.amazon.nova-2-lite-v1:0", aws_region="us-east-1"))
    assert model.config["model_id"] == "us.amazon.nova-2-lite-v1:0"
    assert model.config["max_tokens"] == 4096 and model.config["temperature"] == 0.2
    assert "cache_config" not in model.config


def test_mcp_client_is_built_but_not_started() -> None:
    client = build_mcp_client(Settings(gateway_url="https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"))
    assert isinstance(client, MCPClient)
    assert not getattr(client, "_background_thread", None)  # connecting happens when the agent first loads tools


def test_agent_wiring_without_tools() -> None:
    settings = Settings(system_prompt="be brief")
    agent = build_agent(settings, tools=[], hooks=[])
    assert agent.system_prompt == "be brief"
    assert agent.callback_handler.__name__ == "null_callback_handler"  # nothing printed to the container's stdout
    assert agent.tool_names == []


def test_settings_from_env() -> None:
    settings = Settings.from_env(
        {"MODEL_ID": " us.amazon.nova-premier-v1:0 ", "AWS_REGION": "us-west-2", "GATEWAY_URL": "https://gw/mcp", "SYSTEM_PROMPT": "x"}
    )
    assert settings.model_id == "us.amazon.nova-premier-v1:0"
    assert settings.aws_region == "us-west-2"
    assert settings.gateway_url == "https://gw/mcp" and settings.search_enabled
    assert settings.system_prompt == "x"

    defaults = Settings.from_env({})
    assert defaults.system_prompt == DEFAULT_SYSTEM_PROMPT and not defaults.search_enabled


def test_system_prompt_arrives_base64_encoded_with_newlines_intact() -> None:
    import base64

    prompt = "Line one.\n\nLine two, with a tab\t and an accent \u00e9.\n"
    settings = Settings.from_env({"SYSTEM_PROMPT_B64": base64.b64encode(prompt.encode()).decode(), "SYSTEM_PROMPT": "ignored"})
    assert settings.system_prompt == prompt.strip()


def test_usage_of_handles_missing_metrics() -> None:
    class Result:
        metrics = None

    assert usage_of(Result()) is None
    assert usage_of(object()) is None
