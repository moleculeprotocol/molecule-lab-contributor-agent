"""AES-256-GCM envelope crypto for Molecule Labs data-room files.

A byte-for-byte port of the Labs client's `encryptFileWithKms` / `decryptFileWithKms`.
Whatever this writes, the Labs web app can open; whatever the app writes, this can open.
The format is not negotiable:

    AES-256-GCM
    random 12-byte IV          (NOT 16 — a 16-byte IV produces a file the app cannot read)
    128-bit auth tag APPENDED to the ciphertext, never a separate field
    no AAD                     (the client calls WebCrypto without `additionalData`)
    DEK         = standard padded base64 of raw 32 bytes  (not base64url)
    iv          = standard padded base64 of the raw 12 bytes
    contentHash = lowercase hex SHA-256 of the PLAINTEXT, no prefix

The `contentHash` rule is the one people get wrong. The GraphQL schema's own description
for the field says "Hash of the encrypted content" — that description is wrong, and
nothing on the server checks the value, so a mistake here is silent. The client hashes
the plaintext. Match the client.

This module imports nothing that can open a socket. That is deliberate: it is the part a
reviewer most needs to be able to trust in isolation.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TAG_BYTES = 16  # AES-GCM 128-bit tag, matching WebCrypto's `tagLength: 128`
IV_BYTES = 12  # matching `crypto.getRandomValues(new Uint8Array(12))`


@dataclass(frozen=True)
class EncryptResult:
    """ciphertext — the bytes to PUT to S3, laid out as ciphertext||tag.

    cipher_bytes — len(plaintext) + 16. This is the contentLength you declare to
        initiateCreateOrUpdateFile, NOT the plaintext length.
    content_hash — hex SHA-256 of the plaintext, for encryptionMetadata.contentHash.
    iv — base64, for encryptionMetadata.iv.
    """

    ciphertext: bytes
    cipher_bytes: int
    content_hash: str
    iv: str


def _dek_to_key(plaintext_dek: str) -> bytes:
    key = base64.b64decode(plaintext_dek)
    if len(key) != 32:
        raise ValueError(f"DEK must decode to 32 bytes (AES-256), got {len(key)}")
    return key


def encrypt(plaintext: bytes, plaintext_dek: str, *, iv: bytes | None = None) -> EncryptResult:
    """Envelope-encrypt `plaintext` under the base64 DEK from generateDataEncryptionKey.

    `iv` exists only so the known-answer test can reproduce a frozen ciphertext. Never
    pass it in production: reusing an IV under one key breaks GCM catastrophically,
    leaking the authentication key and the XOR of the two plaintexts.
    """
    key = _dek_to_key(plaintext_dek)
    if iv is None:
        iv = secrets.token_bytes(IV_BYTES)
    elif len(iv) != IV_BYTES:
        raise ValueError(f"iv must be exactly {IV_BYTES} bytes, got {len(iv)}")
    # AESGCM.encrypt appends the 16-byte tag, exactly matching the WebCrypto layout.
    # The final `None` is the AAD — it must stay None.
    out = AESGCM(key).encrypt(iv, plaintext, None)
    return EncryptResult(
        ciphertext=out,
        cipher_bytes=len(out),
        content_hash=hashlib.sha256(plaintext).hexdigest(),
        iv=base64.b64encode(iv).decode(),
    )


def decrypt(ciphertext: bytes, iv: str, plaintext_dek: str) -> bytes:
    """Open a ciphertext||tag blob. Raises if the key, iv or tag is wrong."""
    key = _dek_to_key(plaintext_dek)
    if len(ciphertext) < TAG_BYTES:
        raise ValueError("Ciphertext is shorter than the GCM auth tag")
    return AESGCM(key).decrypt(base64.b64decode(iv), bytes(ciphertext), None)


# --------------------------------------------------------------------------
# PUBLISHED TEST VECTOR — NOT A SECRET, NOT A KEY, NOTHING TO ROTATE.
#
# This is a known-answer test in the sense NIST and the RFCs use the term: a fixed
# input/output pair, published on purpose, so any implementation can prove it computes the
# same bytes as everyone else. Every cryptographic library ships these.
#
# Specifically, so nobody has to take that on trust:
#   * the key below is the literal ASCII string "MoleculeLabsKATkey_32_bytes_ok!!",
#     typed out in full and base64-encoded at import. It is not random key material, it
#     was never issued by KMS, and it has never encrypted anything but the sentence below.
#   * the plaintext is that sentence, in the clear, four lines down.
#   * the ciphertext decrypts to exactly that sentence. Anyone can run it.
#   * the hash is the SHA-256 of that same public sentence.
#
# It is deliberately written as ASCII rather than an opaque base64 blob so that a reader —
# or a secret scanner — can see at a glance that it is a fixture. Real data encryption keys
# are 32 random bytes issued per file by generateDataEncryptionKey, live only in memory for
# the length of one upload, and never appear in this repository.
# --------------------------------------------------------------------------

# The whole "key": readable, hand-typed, exactly 32 bytes.
_VECTOR_KEY_ASCII = b"MoleculeLabsKATkey_32_bytes_ok!!"  # pragma: allowlist secret
assert len(_VECTOR_KEY_ASCII) == 32, "the test vector's key must stay this literal string"

_VECTOR_KEY_B64 = base64.b64encode(_VECTOR_KEY_ASCII).decode()  # gitleaks:allow
_VECTOR_IV_B64 = "e71RgwVrwJ6FdGPi"  # 12 random bytes, fixed so the output is reproducible
_VECTOR_PLAINTEXT = b"Molecule Labs known-answer vector v1\n"
_VECTOR_CIPHERTEXT_B64 = (
    "yHtJFd0iCFu0PNRG5tb3QEd7/A9+tLrWkXrDC/6Uub4RodaeWMnycX92m0g5kIOC4ZsRFt4="
)
_VECTOR_CONTENT_HASH = "dae211aadb1b92bc3c72c1efccde592d377df4e59be8a63481851c2b681a6a49"


def self_test() -> bool:
    """Prove this build of the envelope matches the Labs client. Microseconds, no network.

    Called automatically before the first confidential upload — a confidential file is
    not the place to discover that the crypto drifted.
    """
    ct = base64.b64decode(_VECTOR_CIPHERTEXT_B64)

    opened = decrypt(ct, _VECTOR_IV_B64, _VECTOR_KEY_B64)
    if opened != _VECTOR_PLAINTEXT:
        raise RuntimeError("envelope self-test: could not decrypt the known-answer vector")

    again = encrypt(_VECTOR_PLAINTEXT, _VECTOR_KEY_B64, iv=base64.b64decode(_VECTOR_IV_B64))
    if again.ciphertext != ct:
        raise RuntimeError("envelope self-test: re-encryption did not reproduce the vector")
    if again.content_hash != _VECTOR_CONTENT_HASH:
        raise RuntimeError("envelope self-test: contentHash is not the SHA-256 of the plaintext")
    if again.cipher_bytes != len(_VECTOR_PLAINTEXT) + TAG_BYTES:
        raise RuntimeError("envelope self-test: the auth tag is not appended")

    return True
