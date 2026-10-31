import time

import pytest

from app.auth import MAX_TOKEN_BYTES, Unauthorized


def test_valid_id_token_yields_caller(verifier, make_token):
    caller = verifier.verify(make_token())
    assert caller.user_id == "user-alice"
    assert caller.email == "alice@example.com"


def test_email_is_optional(verifier, make_token):
    assert verifier.verify(make_token(email=None)).email is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_use": "access"},  # an access token signed by the same pool
        {"aud": "another-app-client"},
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OtherPool"},
        {"exp": int(time.time()) - 7200, "iat": int(time.time()) - 8000},
        {"token_use": None},  # required claim missing
        {"sub": None},
    ],
)
def test_bad_tokens_are_rejected(verifier, make_token, overrides):
    with pytest.raises(Unauthorized):
        verifier.verify(make_token(**overrides))


def test_garbage_is_rejected(verifier):
    with pytest.raises(Unauthorized):
        verifier.verify("not.a.jwt")


def test_oversized_token_is_rejected_before_parsing(verifier, make_token):
    token = make_token(padding="x" * MAX_TOKEN_BYTES)
    assert len(token) > MAX_TOKEN_BYTES
    with pytest.raises(Unauthorized):
        verifier.verify(token)


def test_routes_require_a_bearer_id_token(client, make_token):
    assert client.get("/v1/threads").status_code == 401
    assert client.get("/v1/threads").json() == {"error": "unauthorized"}
    assert client.get("/v1/threads", headers={"Authorization": "Basic abc"}).status_code == 401
    access = make_token(token_use="access")
    assert client.get("/v1/threads", headers={"Authorization": f"Bearer {access}"}).status_code == 401
    assert client.get("/v1/threads", headers={"Authorization": f"Bearer {make_token()}"}).status_code == 200
