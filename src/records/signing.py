"""The portal's signing key, and signing and checking records.

The key is 32 random bytes made on first use and kept in DATA_DIR, like the
site's secret key: never in the image, gone with `docker compose down -v`.
RECORD_SIGNING_KEY (64 hex characters) pins it, so a re-install can keep
signing with the key the organizers already published.

A signed record is plain JSON:

    {"record": {...}, "signature": "<hex>", "public_key": "<hex>", "algorithm": "Ed25519"}

The signature covers the record encoded canonically: keys sorted, no spaces,
UTF-8. Any language can rebuild those bytes and check them, and so can
src/records/verify.py, with no Django and no network.
"""

import hashlib
import json
import os
import secrets

from django.conf import settings

from . import ed25519

_cache = {}


def _load_secret():
    pinned = os.environ.get("RECORD_SIGNING_KEY", "").strip()
    if pinned:
        return bytes.fromhex(pinned)
    path = settings.DATA_DIR / "record_signing_key"
    if path.exists():
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    secret = secrets.token_bytes(32)
    path.write_text(secret.hex(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def keys():
    """(secret, public) for this install. Deriving the public key costs a few
    milliseconds, so it's worked out once per process."""
    if "keys" not in _cache:
        secret = _load_secret()
        _cache["keys"] = (secret, ed25519.public_key(secret))
    return _cache["keys"]


def key_id(public):
    return hashlib.sha256(public).hexdigest()[:16]


def public_key_hex():
    return keys()[1].hex()


def canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(record):
    secret, public = keys()
    return ed25519.sign(secret, canonical(record)).hex(), key_id(public)


def envelope(record, signature):
    return {"record": record, "signature": signature, "public_key": public_key_hex(), "algorithm": "Ed25519",
            "signed_bytes": "the record as JSON with sorted keys, no spaces, UTF-8"}


def check(document, trusted_key=None):
    """Check a signed record. Returns (ok, reasons). trusted_key (hex) is the
    public key you already trust; without it, the key inside the document is
    used, which proves integrity but not who signed."""
    reasons = []
    try:
        record, signature = document["record"], bytes.fromhex(document["signature"])
        public = bytes.fromhex(trusted_key or document["public_key"])
    except (KeyError, TypeError, ValueError):
        return False, ["That isn't a signed Shipshape record."]
    if document.get("public_key") and trusted_key and document["public_key"].lower() != trusted_key.lower():
        reasons.append("It names a different signing key from the one you trust.")
    if not ed25519.verify(public, canonical(record), signature):
        reasons.append("The signature doesn't match: the record was changed after signing, or signed by another key.")
    return not reasons, reasons
