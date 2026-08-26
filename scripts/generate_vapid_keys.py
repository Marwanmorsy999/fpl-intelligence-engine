"""Phase 23 Gate 1 (L2) — generate the VAPID keypair for self-hosted web push.

Run ONCE, then put the values in your deployment env (Vercel project env vars):

    python scripts/generate_vapid_keys.py

* ``VAPID_PUBLIC_KEY``   — base64url (the browser asks for this),
* ``VAPID_PRIVATE_KEY``  — base64url raw scalar (server secret),
* ``VAPID_SUBJECT``      — a mailto: contact (any string like
                           "mailto:you@example.com").
"""

from __future__ import annotations

import base64
import os
import sys


def main() -> int:
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        print("cryptography is required: pip install cryptography", file=sys.stderr)
        return 1

    private_key = ec.generate_private_key(ec.SECP256R1())
    raw_private = private_key.private_numbers().private_value.to_bytes(32, "big")
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    raw_public = private_key.public_key().public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint,
    )

    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731

    print("Add these to your deployment environment:")
    print()
    print(f"VAPID_PUBLIC_KEY={b64(raw_public)}")
    print(f"VAPID_PRIVATE_KEY={b64(raw_private)}")
    print('VAPID_SUBJECT="mailto:you@example.com"')
    print()
    if not os.environ.get("VAPID_PUBLIC_KEY"):
        print("(No VAPID_PUBLIC_KEY currently set in this shell.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
