"""Security regression tests for the bearer-JWT verifier configuration.

_build_auth() constructs JWTVerifier without an explicit algorithm, so these tests pin the
properties the gateway's auth depends on: the RS256 default, and rejection of algorithm
confusion, unsigned tokens, and wrong issuer/audience/expiry. FastMCP swapped its JWT backend
(PyJWT to joserfc) between 3.0.2 and 3.4.7 without an API change, so nothing else in the suite
would catch a regression here.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth.providers.jwt import JWTVerifier
from joserfc import jwk
from joserfc import jwt as jose_jwt

ISSUER = 'https://idp.example/application/o/jenkins-mcp/'
AUDIENCE = 'jenkins-mcp'


@pytest.fixture(scope='module')
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture(scope='module')
def verifier(rsa_keypair):
    _, public_pem = rsa_keypair
    # Mirrors _build_auth(): no explicit algorithm. public_key stands in for jwks_uri so the
    # tests need no network; the verification path after key selection is identical.
    return JWTVerifier(public_key=public_pem, issuer=ISSUER, audience=AUDIENCE)


def _claims(**overrides: object) -> dict:
    now = int(time.time())
    claims = {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'sub': 'test-user',
        'iat': now,
        'exp': now + 300,
        'groups': ['jenkins-mcp-writers'],
    }
    claims.update(overrides)
    return claims


def _sign_rs256(private_pem, claims):
    key = jwk.import_key(private_pem, 'RSA')
    return jose_jwt.encode({'alg': 'RS256'}, claims, key, algorithms=['RS256'])


def test_default_algorithm_is_rs256(verifier):
    assert verifier.algorithm == 'RS256'


@pytest.mark.asyncio
async def test_valid_rs256_token_accepted(rsa_keypair, verifier):
    private_pem, _ = rsa_keypair
    token = _sign_rs256(private_pem, _claims())

    result = await verifier.verify_token(token)

    assert result is not None


@pytest.mark.asyncio
async def test_hs256_signed_with_public_key_rejected(rsa_keypair, verifier):
    # Algorithm confusion: an attacker who has the public key mints an HS256 token using it as
    # the HMAC secret. A verifier that honours the token's alg header would accept it.
    import warnings

    private_pem, public_pem = rsa_keypair
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        hmac_key = jwk.import_key(public_pem.encode(), 'oct')
    token = jose_jwt.encode({'alg': 'HS256'}, _claims(), hmac_key, algorithms=['HS256'])

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_unsigned_alg_none_rejected(verifier):
    def b64(part):
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b'=').decode()

    token = f'{b64({"alg": "none", "typ": "JWT"})}.{b64(_claims())}.'

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_wrong_issuer_rejected(rsa_keypair, verifier):
    private_pem, _ = rsa_keypair
    token = _sign_rs256(private_pem, _claims(iss='https://evil.example/'))

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_wrong_audience_rejected(rsa_keypair, verifier):
    private_pem, _ = rsa_keypair
    token = _sign_rs256(private_pem, _claims(aud='someone-else'))

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_expired_token_rejected(rsa_keypair, verifier):
    private_pem, _ = rsa_keypair
    token = _sign_rs256(private_pem, _claims(exp=int(time.time()) - 10))

    assert await verifier.verify_token(token) is None
