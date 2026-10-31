"""Shared fixtures: fake AWS (moto), a generated RSA key pair for tokens, a fake
agent runtime and an app wired with all of them. No test touches the network."""

import os

# Fake credentials *before* boto3 is imported anywhere, so neither moto nor a
# stray real client can pick up a developer's profile.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.pop("AWS_PROFILE", None)

import time  # noqa: E402

import boto3  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

from app.auth import TokenVerifier  # noqa: E402
from app.files import make_s3_client  # noqa: E402
from app.main import create_app  # noqa: E402
from app.runtime import sse  # noqa: E402
from app.settings import Settings  # noqa: E402

REGION = "us-east-1"
TABLE = "threads-test"
BUCKET = "files-test"
ALICE = "user-alice"
BOB = "user-bob"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        region=REGION,
        user_pool_id="us-east-1_TestPool1",
        client_id="1h57kf5cpq17m0eml12example",
        threads_table=TABLE,
        thread_ttl_days=30,
        files_bucket=BUCKET,
        files_ttl_days=30,
        files_url_ttl_seconds=300,
        agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/demo-abc123",
    )


# ---- tokens -----------------------------------------------------------------


@pytest.fixture(scope="session")
def rsa_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


class FakeJwkClient:
    """Stands in for ``jwt.PyJWKClient``: always returns our public key."""

    def __init__(self, public_key):
        self.key = public_key  # same attribute a real ``PyJWK`` exposes

    def get_signing_key_from_jwt(self, token):
        return self


@pytest.fixture(scope="session")
def verifier(settings, rsa_keys) -> TokenVerifier:
    return TokenVerifier(issuer=settings.issuer, audience=settings.client_id, jwk_client=FakeJwkClient(rsa_keys[1]))


@pytest.fixture(scope="session")
def make_token(settings, rsa_keys):
    """Mint a Cognito-shaped token; pass ``claim=None`` to drop a claim."""
    private, _ = rsa_keys

    def _make(sub=ALICE, email="alice@example.com", token_use="id", **overrides):
        now = int(time.time())
        claims = {
            "sub": sub,
            "email": email,
            "token_use": token_use,
            "aud": settings.client_id,
            "iss": settings.issuer,
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test-key"})

    return _make


@pytest.fixture
def auth(make_token):
    return {"Authorization": "Bearer " + make_token()}


@pytest.fixture
def other_auth(make_token):
    return {"Authorization": "Bearer " + make_token(sub=BOB, email="bob@example.com")}


# ---- AWS --------------------------------------------------------------------


@pytest.fixture
def aws():
    """A moto sandbox with the threads table and the files bucket created."""
    with mock_aws():
        table = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "thread_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "thread_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = make_s3_client(REGION)
        s3.create_bucket(Bucket=BUCKET)
        yield {"table": table, "s3": s3}


@pytest.fixture
def ddb_table(aws):
    return aws["table"]


@pytest.fixture
def s3(aws):
    return aws["s3"]


# ---- fake agent runtime -----------------------------------------------------


class FakeRawStream:
    """Mimics urllib3's response: ``read1`` returns one queued chunk per call."""

    def __init__(self, chunks, *, fail_after=None, delay=0.0):
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self._delay = delay
        self.reads = 0

    def read1(self, size=-1):
        if self._delay:
            time.sleep(self._delay)
        if self._fail_after is not None and self.reads >= self._fail_after:
            raise OSError("connection reset by peer")
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""


class FakeBody:
    """Mimics botocore's ``StreamingBody``: a ``_raw_stream`` plus ``close()``."""

    def __init__(self, raw):
        self._raw_stream = raw
        self.closed = False

    def close(self):
        self.closed = True


class FakeRuntime:
    def __init__(self):
        self.chunks = [sse({"type": "text", "delta": "Hello"}), sse({"type": "done", "runId": "x", "stopReason": "end_turn"})]
        self.error = None
        self.calls = []

    def invoke(self, runtime_session_id, user_id, payload):
        self.calls.append({"runtime_session_id": runtime_session_id, "user_id": user_id, "payload": payload})
        if self.error is not None:
            raise self.error
        return FakeBody(FakeRawStream(self.chunks))


@pytest.fixture
def fake_runtime():
    return FakeRuntime()


@pytest.fixture
def client(settings, verifier, ddb_table, s3, fake_runtime):
    app = create_app(settings, verifier=verifier, ddb_table=ddb_table, s3=s3, runtime=fake_runtime)
    with TestClient(app) as test_client:  # the context manager runs the lifespan
        yield test_client
