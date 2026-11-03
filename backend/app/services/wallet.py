"""Wallet storage. The private key arrives once over the authenticated API, is
encrypted immediately and only ever decrypted in-process to build a signer."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass

from eth_account import Account
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.models import Wallet

log = logging.getLogger(__name__)


class WalletError(Exception):
    pass


@dataclass
class SignerMaterial:
    """Decrypted signing material, in memory only. Never serialise or log this."""
    private_key: str
    signer_address: str
    funder_address: str | None
    signature_type: int
    api_creds: dict | None
    source: str

    def __repr__(self) -> str:
        return f"SignerMaterial(signer={self.signer_address}, funder={self.funder_address}, source={self.source})"

    __str__ = __repr__


def derive_api_creds(private_key: str, funder: str | None, sig_type: int) -> dict:
    """Ask Polymarket for this key's L2 API creds (creates them on first use).
    This signs an auth message locally; the key itself is not transmitted."""
    from py_clob_client_v2 import ClobClient

    s = get_settings()
    client = ClobClient(s.clob_host, chain_id=137, key=private_key, signature_type=sig_type, funder=funder)
    creds = client.create_or_derive_api_key()
    return {"api_key": creds.api_key, "api_secret": creds.api_secret, "api_passphrase": creds.api_passphrase}


async def save_wallet(db: AsyncSession, user_id: uuid.UUID, private_key: str, funder: str | None,
                      sig_type: int, verify=derive_api_creds) -> Wallet:
    try:
        signer = Account.from_key(private_key).address
    except Exception as e:  # noqa: BLE001 - never echo the key in the message
        raise WalletError("that is not a valid private key") from e
    if sig_type in (1, 2) and not funder:
        raise WalletError("signature type 1/2 (Polymarket proxy wallet) needs your Polymarket address")
    if sig_type == 0:
        funder = None
    try:
        # sync HTTP inside the SDK: run in a thread so the market feeds keep flowing
        creds = await asyncio.to_thread(verify, private_key, funder, sig_type) if verify else None
    except Exception as e:  # noqa: BLE001
        raise WalletError(f"Polymarket rejected these credentials: {type(e).__name__}") from e

    await db.execute(update(Wallet).where(Wallet.user_id == user_id, Wallet.is_active).values(is_active=False))
    w = Wallet(user_id=user_id, signer_address=signer, funder_address=funder, signature_type=sig_type,
               encrypted_key=encrypt_secret(private_key),
               encrypted_api_creds=encrypt_secret(json.dumps(creds)) if creds else None,
               last_verified_at=dt.datetime.now(dt.UTC) if creds else None)
    db.add(w)
    await db.commit()
    log.info("wallet saved for user %s signer=%s", user_id, signer)
    return w


async def active_wallet(db: AsyncSession, user_id: uuid.UUID) -> Wallet | None:
    r = await db.execute(select(Wallet).where(Wallet.user_id == user_id, Wallet.is_active)
                         .order_by(Wallet.created_at.desc()).limit(1))
    return r.scalar_one_or_none()


async def remove_wallet(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Hard-delete stored ciphertext (not just deactivate)."""
    rows = (await db.execute(select(Wallet).where(Wallet.user_id == user_id))).scalars().all()
    for w in rows:
        await db.delete(w)
    await db.commit()
    return len(rows)


async def load_signer(db: AsyncSession, user_id: uuid.UUID) -> SignerMaterial | None:
    w = await active_wallet(db, user_id)
    if w:
        creds = json.loads(decrypt_secret(w.encrypted_api_creds)) if w.encrypted_api_creds else None
        return SignerMaterial(decrypt_secret(w.encrypted_key), w.signer_address, w.funder_address,
                              w.signature_type, creds, "app")
    s = get_settings()
    if s.private_key:
        return SignerMaterial(s.private_key, Account.from_key(s.private_key).address,
                              s.funder_address or None, s.signature_type, None, "env")
    return None
