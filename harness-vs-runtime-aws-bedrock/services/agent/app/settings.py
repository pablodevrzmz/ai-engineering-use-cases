"""Configuration, read once from the environment.

Every value here is set by Terraform on the AgentCore runtime (infra/platform/runtime.tf). SYSTEM_PROMPT is the
same file the harness receives through `system_prompt.text`, which is what keeps the two flavors comparable.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass

# Only used when SYSTEM_PROMPT is unset (local runs, tests). Deployed containers always get prompts/system-prompt.md.
DEFAULT_SYSTEM_PROMPT = (
    "You are a research assistant with one tool, WebSearch. Use it for facts you may not know or that may have "
    "changed, answer in a few plain sentences, and end with a 'Sources:' line listing the URLs you relied on."
)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the agent container.

    Attributes:
        model_id: Bedrock model or inference-profile id for the Strands BedrockModel (same as the harness).
        aws_region: Region for Bedrock and the gateway; AgentCore injects AWS_REGION into the container.
        gateway_url: MCP endpoint of the AgentCore Gateway that fronts the web-search tool. Empty means no tool is
            offered to the model (local runs without a gateway).
        system_prompt: The agent's instructions; the deployed value is prompts/system-prompt.md. AgentCore rejects
            environment variables with control characters (newlines included), so Terraform ships the file
            base64-encoded as SYSTEM_PROMPT_B64 and the bytes arrive exactly as the harness received them. A plain
            SYSTEM_PROMPT is accepted too, for local runs.
    """

    model_id: str = "us.amazon.nova-2-lite-v1:0"
    aws_region: str = "us-east-1"
    gateway_url: str = ""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables, falling back to the defaults above."""
        env = os.environ if env is None else env
        defaults = cls()
        encoded = env.get("SYSTEM_PROMPT_B64", "").strip()
        prompt = base64.b64decode(encoded).decode("utf-8") if encoded else env.get("SYSTEM_PROMPT", "")
        return cls(
            model_id=env.get("MODEL_ID", defaults.model_id).strip() or defaults.model_id,
            aws_region=env.get("AWS_REGION", defaults.aws_region).strip() or defaults.aws_region,
            gateway_url=env.get("GATEWAY_URL", defaults.gateway_url).strip(),
            system_prompt=prompt.strip() or defaults.system_prompt,
        )

    @property
    def search_enabled(self) -> bool:
        """True when a gateway is configured, which is what gives the model its WebSearch tool."""
        return bool(self.gateway_url)
