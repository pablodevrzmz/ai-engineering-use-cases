"""Environment contract for the Session API.

Every name here is also set by Terraform on the ECS task definition. Keeping
the list in one dataclass (instead of ``os.environ`` reads sprinkled around)
makes the contract visible and lets tests build a ``Settings`` by hand.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, read once at start-up."""

    region: str
    user_pool_id: str
    client_id: str
    threads_table: str
    thread_ttl_days: int
    files_bucket: str
    files_ttl_days: int
    files_url_ttl_seconds: int
    agent_runtime_arn: str
    agent_runtime_qualifier: str = "live"
    # Seconds of upstream silence before the relay sends an SSE comment so the
    # load balancer (idle timeout 600 s) and the browser keep the stream open.
    heartbeat_seconds: float = 15.0

    @property
    def issuer(self) -> str:
        """The ``iss`` claim Cognito puts in every token of this pool."""
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_url(self) -> str:
        """Where the pool publishes the public keys that sign its tokens."""
        return f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from environment variables.

        Missing values default to empty strings rather than raising, so the
        module can be imported (and the app object created) without AWS
        configuration, e.g. by tests. ``missing()`` reports the gaps and the
        lifespan refuses to start real clients while any remain.
        """
        env = os.environ if env is None else env
        return cls(
            region=env.get("AWS_REGION", "us-east-1"),
            user_pool_id=env.get("COGNITO_USER_POOL_ID", ""),
            client_id=env.get("COGNITO_CLIENT_ID", ""),
            threads_table=env.get("THREADS_TABLE", ""),
            thread_ttl_days=int(env.get("THREAD_TTL_DAYS", "30")),
            files_bucket=env.get("FILES_BUCKET", ""),
            files_ttl_days=int(env.get("FILES_TTL_DAYS", "30")),
            files_url_ttl_seconds=int(env.get("FILES_URL_TTL_SECONDS", "300")),
            agent_runtime_arn=env.get("AGENT_RUNTIME_ARN", ""),
            agent_runtime_qualifier=env.get("AGENT_RUNTIME_QUALIFIER", "live"),
        )

    def missing(self) -> list[str]:
        """Names of required environment variables that are still blank."""
        required = {
            "COGNITO_USER_POOL_ID": self.user_pool_id,
            "COGNITO_CLIENT_ID": self.client_id,
            "THREADS_TABLE": self.threads_table,
            "FILES_BUCKET": self.files_bucket,
            "AGENT_RUNTIME_ARN": self.agent_runtime_arn,
        }
        return [name for name, value in required.items() if not value]
