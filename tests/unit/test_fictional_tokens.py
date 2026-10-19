# SPDX-License-Identifier: MIT-0
"""Fictional JWT fixtures are readable and are refused by a real verifier.

`fictional_tokens.fictional_jwt` spells the committed test tokens from JSON parts, so no encoded
literal sits in a tracked file. These cases prove two things with real cryptography and a key
generated here: the parts decode to exactly what the fixture says, and the fixture's own signature
is refused by PyJWT, by the parser for today's bytes and by the verifier for a well-formed
signature. The same header signed with the test's key is accepted, so each refusal comes from the
fixture's signature, not from the key, header or claims.
"""

from __future__ import annotations

import base64
import json

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fictional_tokens import b64url, b64url_json, fictional_jwt

pytestmark = pytest.mark.unit

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _decoded(segment: str) -> object:
    """Decode one base64url JSON segment by hand; never through PyJWT's header parser."""
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def test_the_parts_decode_to_exactly_the_written_header_and_claims() -> None:
    parts = fictional_jwt().split(".")

    assert len(parts) == 3
    assert _decoded(parts[0]) == {"alg": "RS256"}
    assert _decoded(parts[1]) == {"sub": "fictional"}
    assert fictional_jwt("claims", signature="signature").split(".")[1:] == [
        "claims",
        "signature",
    ]


def test_todays_fixture_bytes_are_refused_by_the_parser() -> None:
    with pytest.raises(jwt.DecodeError) as excinfo:
        jwt.decode(fictional_jwt(), key=_KEY.public_key(), algorithms=["RS256"])

    # InvalidSignatureError subclasses DecodeError, so the exact type is the refusal signal.
    assert type(excinfo.value) is jwt.DecodeError
    assert str(excinfo.value) == "Invalid crypto padding"


def test_a_well_formed_fictional_signature_is_refused_by_the_verifier() -> None:
    with pytest.raises(jwt.InvalidSignatureError) as excinfo:
        jwt.decode(
            fictional_jwt(signature=b64url(b"\0" * 256)),
            key=_KEY.public_key(),
            algorithms=["RS256"],
        )

    assert type(excinfo.value) is jwt.InvalidSignatureError
    assert str(excinfo.value) == "Signature verification failed"


def test_the_same_header_and_claims_signed_by_the_test_key_are_accepted() -> None:
    signing_input = f"{b64url_json({'alg': 'RS256'})}.{b64url_json({'sub': 'fictional'})}"
    signature = _KEY.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())

    claims = jwt.decode(
        fictional_jwt(signature=b64url(signature)),
        key=_KEY.public_key(),
        algorithms=["RS256"],
    )

    assert claims == {"sub": "fictional"}
