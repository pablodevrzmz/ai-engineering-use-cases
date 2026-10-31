"""Configuration, read once from the environment.

Every value here is set by Terraform on the AgentCore runtime (see infra/platform/agent.tf) and shared with the
Session API, so the two containers agree on the bucket, the file TTL and the size cap. Keeping the names in one
place makes that contract visible: change a name here and you must change it in Terraform and the API too.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the agent container.

    Attributes:
        files_bucket: S3 bucket the publish_file tool writes to. Empty means publishing is off: the tool is not
            offered to the model and the system prompt does not mention files.
        files_ttl_days: How long a published file lives. Must match the bucket's lifecycle rule, because the agent
            only *announces* the expiry (in metadata and in the `file` event); the bucket enforces it.
        files_max_bytes: Largest file publish_file accepts. One readFiles call carries the whole file through an
            event stream, and botocore refuses event-stream messages above 24 MiB, so keep this well under that.
        model_id: Bedrock model or inference-profile id for the Strands BedrockModel.
        aws_region: Region for Bedrock, the code interpreter and S3.
        code_interpreter_session_seconds: Lifetime AWS gives each sandbox session. The service enforces it from the
            moment the session starts, not from the last call, so app/sandbox.py tracks it too.
    """

    files_bucket: str = ""
    files_ttl_days: int = 30
    files_max_bytes: int = 15 * 1024 * 1024
    model_id: str = "us.amazon.nova-pro-v1:0"
    aws_region: str = "us-east-1"
    code_interpreter_session_seconds: int = 3600

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables, falling back to the defaults above."""
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            files_bucket=env.get("FILES_BUCKET", defaults.files_bucket).strip(),
            files_ttl_days=int(env.get("FILES_TTL_DAYS", defaults.files_ttl_days)),
            files_max_bytes=int(env.get("FILES_MAX_BYTES", defaults.files_max_bytes)),
            model_id=env.get("MODEL_ID", defaults.model_id).strip() or defaults.model_id,
            aws_region=env.get("AWS_REGION", defaults.aws_region).strip() or defaults.aws_region,
            code_interpreter_session_seconds=int(
                env.get("CODE_INTERPRETER_SESSION_SECONDS", defaults.code_interpreter_session_seconds)
            ),
        )

    @property
    def publishing_enabled(self) -> bool:
        """True when a files bucket is configured, which is what turns the publish_file tool on."""
        return bool(self.files_bucket)
