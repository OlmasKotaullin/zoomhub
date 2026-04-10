"""Symmetric encryption for sensitive DB fields (tokens, API keys).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with SECRET_KEY as seed.
EncryptedString — SQLAlchemy TypeDecorator for transparent encrypt/decrypt.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, Text, TypeDecorator


def _get_fernet() -> Fernet:
    from app.config import SECRET_KEY
    key = hashlib.sha256(SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str | None) -> str | None:
    if not value:
        return value
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        # Fallback: value was stored before encryption — return as-is
        return value


class EncryptedText(TypeDecorator):
    """Transparently encrypts/decrypts Text columns via Fernet."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)


class EncryptedString(TypeDecorator):
    """Transparently encrypts/decrypts String columns via Fernet."""

    impl = String(500)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
