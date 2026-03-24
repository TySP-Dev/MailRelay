"""TOTP code generation using pyotp."""

import pyotp


def generate_totp(secret: str) -> str:
    """Generate the current TOTP code from a base32 secret."""
    totp = pyotp.TOTP(secret)
    return totp.now()


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code against a secret (useful for debugging setup)."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code)
