"""Encrypt saved database passwords (Fernet, key derived from settings.VAULT_KEY)."""
import base64
import hashlib

from django.conf import settings


class VaultError(Exception):
    pass


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise VaultError(
            "Saving passwords needs the 'cryptography' package. Install it with: pip install cryptography"
        ) from None
    digest = hashlib.sha256(f"dataloader-db-passwords:{settings.VAULT_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise VaultError(
            "The saved password can’t be decrypted, probably because the secret key changed. Enter it again."
        ) from None
