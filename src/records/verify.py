"""Check a Shipshape certificate or judge's record offline.

    python src/records/verify.py record.json
    python src/records/verify.py record.json --key <the organizers' public key, hex>

Plain Python, no Django, no network: it rebuilds the signed bytes (the
record as JSON with sorted keys and no spaces, UTF-8) and checks the Ed25519
signature. Without --key it uses the key inside the file, which proves the
record wasn't changed but not who signed it; pass the key the organizers
published to prove that too. It can't know about revocation: for that, check
the code on the portal while it's running.

Exit status: 0 genuine, 1 not.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ed25519  # noqa: E402  (this folder, so the script runs on its own)


def check(document, key=None):
    try:
        record = document["record"]
        signature = bytes.fromhex(document["signature"])
        public = bytes.fromhex(key or document["public_key"])
    except (KeyError, TypeError, ValueError):
        return False, "That isn't a signed Shipshape record."
    if key and document.get("public_key", "").lower() != key.lower():
        return False, "It was signed with a different key from the one you gave."
    message = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not ed25519.verify(public, message, signature):
        return False, "The signature doesn't match: the record was changed after signing, or signed by another key."
    return True, f"Genuine: {record.get('recipient')}, {record.get('title', '').lower()}, {record.get('event', {}).get('name')}."


def main():
    parser = argparse.ArgumentParser(description="Check a Shipshape record's signature, offline.")
    parser.add_argument("file")
    parser.add_argument("--key", help="the public key you trust, as hex")
    args = parser.parse_args()
    ok, message = check(json.loads(Path(args.file).read_text(encoding="utf-8")), args.key)
    print(message)
    if ok and not args.key:
        print("(Checked with the key inside the file. Pass --key with the organizers' published key to prove who signed it.)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
