"""Cognito ID-token verification.

The browser signs in directly against Cognito (``InitiateAuth``) and sends the
resulting *ID token* as ``Authorization: Bearer ...``. The API never sees a
password; it only checks the token's signature and claims against the pool's
published JWKS. Verification is CPU work plus an occasional HTTPS fetch of the
JWKS, so the FastAPI dependency runs it in a thread pool to keep the event
loop free for the streaming responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from fastapi import Request
from fastapi.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

# A real Cognito ID token is ~1 KB. Refusing anything much larger before
# parsing keeps a hostile client from making us base64-decode megabytes.
MAX_TOKEN_BYTES = 4096


class Unauthorized(Exception):
    """Raised for any token problem; the app maps it to 401 ``unauthorized``.

    Deliberately one exception for every failure (expired, wrong audience,
    not an ID token...): the response must not tell an attacker which check
    failed, but the reason is logged for the operator.
    """


@dataclass(frozen=True)
class Caller:
    """The authenticated user, as the routes see it."""

    user_id: str
    email: str | None = None


class SigningKeyProvider(Protocol):
    """The slice of ``jwt.PyJWKClient`` we rely on, so tests can fake it."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class TokenVerifier:
    """Verifies Cognito ID tokens for one user pool and app client."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        jwk_client: SigningKeyProvider | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        if jwk_client is None:
            if not jwks_url:
                raise ValueError("jwks_url is required when no jwk_client is given")
            # cache_keys=True keeps parsed keys in memory; lifespan=3600 refreshes
            # the JWKS hourly, which also picks up a key rotation.
            jwk_client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        self._jwk_client = jwk_client

    def verify(self, token: str) -> Caller:
        """Return the caller for a valid ID token or raise ``Unauthorized``."""
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise Unauthorized("token too large")
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=60,  # tolerate small clock drift between Cognito and us
                options={"require": ["exp", "iat", "sub", "token_use"]},
            )
        except jwt.PyJWTError as exc:
            log.info("token rejected: %s", exc)
            raise Unauthorized(str(exc)) from exc
        except Exception as exc:  # JWKS unreachable, malformed key set...
            log.warning("token verification failed: %s", exc)
            raise Unauthorized("verification failed") from exc
        # Cognito also issues *access* tokens signed by the same keys. They carry
        # ``client_id`` instead of ``aud`` and no email; we only accept ID tokens
        # so the audience check above is meaningful.
        if claims.get("token_use") != "id":
            raise Unauthorized("not an id token")
        return Caller(user_id=claims["sub"], email=claims.get("email"))


async def require_caller(request: Request) -> Caller:
    """FastAPI dependency: the bearer token must be a valid Cognito ID token."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token")
    verifier: TokenVerifier = request.app.state.verifier
    return await run_in_threadpool(verifier.verify, token)
