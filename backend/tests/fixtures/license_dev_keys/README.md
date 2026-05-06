# Dev license keypair (NOT a production secret)

This directory contains the Ed25519 keypair used by the test suite to sign
test licenses end-to-end. It is committed to the repo deliberately:

- `private.key` — 32 raw bytes (mode 0600)
- `public.key` — 32 raw bytes

## Why this isn't a security risk

The matching public key is hardcoded as `_DEV_PUBLIC_KEY_HEX` in
[`backend/services/license_public_key.py`](../../../services/license_public_key.py)
as the **dev fallback only**. Production builds inject a different public
key via `JARVIS_LICENSE_PUBKEY_HEX` at build time (per ADR 019 §"Build-time
injection mechanism"), which replaces the dev fallback through the
`_license_pubkey_baked.py` module.

Therefore: a license signed with this dev private key

- **verifies** in dev / pytest (matches the dev fallback constant)
- **fails verification** in production builds (different embedded public key)

## Usage in tests

```python
from pathlib import Path
DEV_KEYS = Path(__file__).parent / "fixtures" / "license_dev_keys"
priv_bytes = DEV_KEYS.joinpath("private.key").read_bytes()
pub_bytes  = DEV_KEYS.joinpath("public.key").read_bytes()
```

The matching constant in `services/license_public_key.py` is asserted by
`tests/test_license_public_key.py` to ensure the keypair stays in sync if
either side is regenerated.

## If you need to rotate the dev keypair

1. Generate a fresh keypair:
   ```bash
   cd backend && .venv/bin/python -m scripts.sign_license generate-keypair --out-dir /tmp/devk --overwrite
   ```
2. Replace both files in this directory.
3. Update `_DEV_PUBLIC_KEY_HEX` in `services/license_public_key.py` to match
   the new `public.key` (run `xxd -p < public.key | tr -d '\n'`).
4. Run the test suite — `tests/test_license_public_key.py::test_dev_constant_matches_fixture`
   pins the constant-vs-file consistency.
