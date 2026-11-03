"""Local key-format validation only; never submits or signs a transaction."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def solana_public_key(value):
    try:
        if not isinstance(value, str) or not 32 <= len(value) <= 100:
            raise ValueError()
        n = 0
        for ch in value:
            n = n * 58 + ALPHABET.index(ch)
        raw = b'\0' * (len(value) - len(value.lstrip('1'))) + n.to_bytes((n.bit_length() + 7)//8, 'big')
        if len(raw) not in (32, 64):
            raise ValueError()
        pub = Ed25519PrivateKey.from_private_bytes(raw[:32]).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if len(raw) == 64 and raw[32:] != pub:
            raise ValueError()
        number, encoded = int.from_bytes(pub, 'big'), ''
        while number:
            number, remainder = divmod(number, 58)
            encoded = ALPHABET[remainder] + encoded
        return '1' * (len(pub)-len(pub.lstrip(b'\0'))) + encoded
    except (ValueError, TypeError):
        raise ValueError('GRID_CREDENTIAL_FORMAT_INVALID') from None
