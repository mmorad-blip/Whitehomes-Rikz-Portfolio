"""Access: two private links, each behind its own access key.

* Shareholder link  <PUBLIC_URL>/v/<RIKZ_VIEW_TOKEN>   + shareholder access key
* Admin link        <PUBLIC_URL>/a/<RIKZ_ADMIN_TOKEN>  + admin access key

Link tokens and key hashes come from environment variables only. Keys are
stored as scrypt hashes; a signed, HttpOnly cookie keeps the session."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

ROLES = ("shareholder", "admin")
SESSION_HOURS = {"shareholder": 24 * 7, "admin": 8}


def hash_key(key: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(key.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_key(key: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    got = hashlib.scrypt(key.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), digest)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_key() -> str:
    # Groups of 4 from an unambiguous alphabet; ~100 bits.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(20))
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


@dataclass(frozen=True)
class AccessConfig:
    secret: bytes
    tokens: dict[str, str]  # role -> link token
    key_hashes: dict[str, str]  # role -> scrypt hash
    public_url: str
    secure_cookies: bool = True

    @classmethod
    def from_env(cls) -> "AccessConfig":
        missing = [v for v in ("RIKZ_SECRET_KEY", "RIKZ_VIEW_TOKEN", "RIKZ_ADMIN_TOKEN", "RIKZ_VIEW_KEY_HASH",
                               "RIKZ_ADMIN_KEY_HASH") if not os.environ.get(v)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)} (run `rikz make-keys`)")
        cfg = cls(
            secret=os.environ["RIKZ_SECRET_KEY"].encode(),
            tokens={"shareholder": os.environ["RIKZ_VIEW_TOKEN"], "admin": os.environ["RIKZ_ADMIN_TOKEN"]},
            key_hashes={"shareholder": os.environ["RIKZ_VIEW_KEY_HASH"], "admin": os.environ["RIKZ_ADMIN_KEY_HASH"]},
            public_url=os.environ.get("RIKZ_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
            secure_cookies=os.environ.get("RIKZ_INSECURE_COOKIES") != "1",
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if len(self.secret) < 32:
            raise RuntimeError("RIKZ_SECRET_KEY must be at least 32 characters")
        for role, tok in self.tokens.items():
            if len(tok) < 20:
                raise RuntimeError(f"the {role} link token must be at least 20 characters")
        if self.tokens["shareholder"] == self.tokens["admin"]:
            raise RuntimeError("the shareholder and admin link tokens must differ")

    def link(self, role: str) -> str:
        return f"{self.public_url}/{'v' if role == 'shareholder' else 'a'}/{self.tokens[role]}/"

    def role_for(self, area: str, token: str) -> str | None:
        role = "shareholder" if area == "v" else "admin" if area == "a" else None
        if role and hmac.compare_digest(token.encode(), self.tokens[role].encode()):
            return role
        return None

    # -- signed session cookie ---------------------------------------------
    def _sign(self, payload: bytes) -> str:
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def make_session(self, role: str) -> str:
        body = base64.urlsafe_b64encode(json.dumps({"r": role, "e": int(time.time()) + SESSION_HOURS[role] * 3600,
                                                    "c": secrets.token_hex(16)}).encode())
        return f"{body.decode()}.{self._sign(body)}"

    def read_session(self, value: str | None, role: str) -> dict | None:
        if not value or "." not in value:
            return None
        body, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(body.encode())):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(body.encode()))
        except ValueError:
            return None
        if data.get("r") != role or data.get("e", 0) < time.time():
            return None
        return data

    def csrf_token(self, session: dict) -> str:
        return self._sign(f"csrf:{session['c']}".encode())

    def client_id(self, ip: str) -> str:
        """Keyed hash of the IP address: lets the view log count visitors
        without storing addresses."""
        return self._sign(f"ip:{ip}".encode())[:16]
