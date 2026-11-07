"""Session auth for Mission — stdlib only.

The dashboard reads $HERMES_HOME and may be reachable from the public internet,
so it must FAIL CLOSED: if no password is configured, every route is denied
rather than served.

Secrets live in $HERMES_HOME/.env, written by Gladwell:

    OPERATOR_AUTH_EMAIL=you@example.com
    OPERATOR_PASSWORD_HASH=pbkdf2_sha256:600000:<salt_hex>:<hash_hex>
    OPERATOR_SESSION_SECRET=<64 hex chars>     # optional; random per boot if unset

Generate the hash on the host, never over chat:

    python3 auth.py hash
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE = "oos_session"
TTL = 12 * 3600          # session lifetime
ITERATIONS = 600_000
_KEYS = ("OPERATOR_AUTH_EMAIL", "OPERATOR_PASSWORD_HASH", "OPERATOR_SESSION_SECRET")


def _env(hermes_home: Path) -> dict:
    out = {k: v for k, v in os.environ.items() if k in _KEYS and v}
    try:
        for line in (Path(hermes_home) / ".env").read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _KEYS and k not in out:
                out[k] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def make_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    # ":" separators, NOT "$" — a .env sourced by a shell (`set -a; . .env`)
    # expands "$600000" as a variable and silently corrupts the hash.
    return f"pbkdf2_sha256:{ITERATIONS}:{salt.hex()}:{dk.hex()}"


def _check_hash(stored: str, password: str) -> bool:
    try:
        algo, iters, salt_hex, want_hex = stored.split(":", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), want_hex)


class Auth:
    """Password check plus signed-cookie sessions. No server-side state."""

    def __init__(self, hermes_home):
        env = _env(hermes_home)
        self.email = (env.get("OPERATOR_AUTH_EMAIL") or "").strip().lower()
        self.hash = env.get("OPERATOR_PASSWORD_HASH") or ""
        secret = env.get("OPERATOR_SESSION_SECRET")
        # No configured secret: random per boot. Sessions then end on restart,
        # which is a safe default, not a bug.
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.ephemeral_secret = not secret

    @property
    def configured(self) -> bool:
        return bool(self.email and self.hash)

    # -- login ---------------------------------------------------------

    def verify(self, email: str, password: str) -> bool:
        if not self.configured:
            return False
        ok_user = hmac.compare_digest((email or "").strip().lower(), self.email)
        ok_pass = _check_hash(self.hash, password or "")
        # evaluate both regardless, so timing does not reveal which failed
        return ok_user and ok_pass

    # -- sessions ------------------------------------------------------

    def issue(self) -> str:
        exp = str(int(time.time()) + TTL)
        payload = base64.urlsafe_b64encode(self.email.encode()).decode().rstrip("=")
        sig = hmac.new(self.secret, f"{payload}.{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{exp}.{sig}"

    def validate(self, token: str) -> bool:
        if not token or not self.configured:
            return False
        try:
            payload, exp, sig = token.split(".", 2)
        except ValueError:
            return False
        want = hmac.new(self.secret, f"{payload}.{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return False
        try:
            if int(exp) < time.time():
                return False
        except ValueError:
            return False
        return True

    def cookie_from_header(self, raw: str | None) -> str:
        for part in (raw or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""


if __name__ == "__main__":  # python3 auth.py hash
    import getpass
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "hash":
        email = input("email: ").strip()
        p1 = getpass.getpass("password: ")
        p2 = getpass.getpass("again:    ")
        if p1 != p2 or not p1:
            raise SystemExit("passwords do not match")
        print("\nAdd these lines to $HERMES_HOME/.env then restart the service:\n")
        print(f"OPERATOR_AUTH_EMAIL={email}")
        print(f"OPERATOR_PASSWORD_HASH={make_hash(p1)}")
        print(f"OPERATOR_SESSION_SECRET={secrets.token_hex(32)}")
    else:
        raise SystemExit("usage: python3 auth.py hash")
