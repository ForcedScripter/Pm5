"""Auth (dashboard login) and encryption of wallet secrets at rest.

Wallet key handling rules (enforced here and in api/wallet.py):
- The key is accepted only over the authenticated /api/wallet endpoint of YOUR backend.
- It is encrypted with MASTER_KEY (Fernet: AES-128-CBC + HMAC-SHA256) before it touches the DB.
- It is never returned by any endpoint, never logged, and never sent to any third party;
  it is only decrypted in-process to sign orders with the official Polymarket SDK.
"""
from __future__ import annotations

import datetime as dt

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class SecretError(Exception):
    pass


def _fernet() -> Fernet:
    key = get_settings().master_key
    if not key:
        raise SecretError("MASTER_KEY is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecretError("MASTER_KEY is not a valid Fernet key") from e


def encrypt_secret(plain: str) -> bytes:
    return _fernet().encrypt(plain.encode())


def decrypt_secret(blob: bytes) -> str:
    try:
        return _fernet().decrypt(blob).decode()
    except InvalidToken as e:
        raise SecretError("could not decrypt wallet secret (MASTER_KEY changed?)") from e


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def make_token(user_id: str) -> str:
    s = get_settings()
    now = dt.datetime.now(dt.UTC)
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + dt.timedelta(hours=s.jwt_ttl_hours)},
                      s.jwt_secret, algorithm="HS256")


def read_token(token: str) -> str | None:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        return None


def redact(text: str) -> str:
    """Strip anything that looks like a 32-byte hex secret from log text."""
    import re
    return re.sub(r"(0x)?[0-9a-fA-F]{64}", "[REDACTED]", text)
