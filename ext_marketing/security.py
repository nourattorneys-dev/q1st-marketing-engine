"""API keys, secret encryption, ids. Keys come from env (compose/.env), never code."""
from __future__ import annotations
import hashlib, hmac, os, secrets

from cryptography.fernet import Fernet, InvalidToken


def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"{name} is not set in compose/.env")
    return v


# ---------------------------------------------------------------- ids
def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# ---------------------------------------------------------------- API keys (shown once, stored as sha256)
def new_api_key() -> tuple[str, str, str]:
    raw = "q1k_" + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw), raw[:10]


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------- admin key (our staff portal -> this service)
def admin_key_ok(given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(given, _env("EXT_MKT_ADMIN_KEY"))


# ---------------------------------------------------------------- secret encryption (client Strapi / social tokens)
def _fernet() -> Fernet:
    return Fernet(_env("EXT_MKT_ENC_KEY").encode())


def encrypt(text: str) -> bytes:
    return _fernet().encrypt(text.encode())


def decrypt(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return _fernet().decrypt(bytes(blob)).decode()
    except InvalidToken:
        return None
