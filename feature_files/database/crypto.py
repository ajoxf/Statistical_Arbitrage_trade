"""
Credential Encryption Utility

Provides symmetric encryption for sensitive data like API keys.
Uses Fernet (AES-128-CBC) from the cryptography library.

Security notes:
- Encryption key is derived from a master password + salt
- Key file is stored separately from the database
- Without the key file, database credentials are unreadable
"""

import os
import base64
import hashlib
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Try to import cryptography, fall back to no encryption if unavailable
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    logger.warning("cryptography library not installed. Credentials will not be encrypted.")


class CredentialEncryptor:
    """
    Encrypts and decrypts sensitive credentials.

    Usage:
        encryptor = CredentialEncryptor()
        encrypted = encryptor.encrypt("my_api_key")
        decrypted = encryptor.decrypt(encrypted)
    """

    KEY_FILE = Path(__file__).parent.parent / ".encryption_key"
    SALT_FILE = Path(__file__).parent.parent / ".encryption_salt"

    def __init__(self, password: Optional[str] = None):
        """
        Initialize encryptor.

        Args:
            password: Master password for key derivation.
                     If None, uses environment variable ENCRYPTION_PASSWORD
                     or generates a random key on first use.
        """
        self._fernet: Optional['Fernet'] = None

        if not HAS_CRYPTO:
            return

        self._password = password or os.environ.get('ENCRYPTION_PASSWORD')
        self._initialize_encryption()

    def _initialize_encryption(self):
        """Initialize or load encryption key"""
        if not HAS_CRYPTO:
            return

        # Get or create salt
        if self.SALT_FILE.exists():
            salt = self.SALT_FILE.read_bytes()
        else:
            salt = os.urandom(16)
            self.SALT_FILE.write_bytes(salt)
            os.chmod(self.SALT_FILE, 0o600)  # Owner read/write only

        # If we have a password, derive key from it
        if self._password:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=480000,  # OWASP recommended minimum
            )
            key = base64.urlsafe_b64encode(kdf.derive(self._password.encode()))
            self._fernet = Fernet(key)
            return

        # Otherwise, use stored key or generate new one
        if self.KEY_FILE.exists():
            key = self.KEY_FILE.read_bytes()
        else:
            key = Fernet.generate_key()
            self.KEY_FILE.write_bytes(key)
            os.chmod(self.KEY_FILE, 0o600)  # Owner read/write only
            logger.info(f"Generated new encryption key at {self.KEY_FILE}")

        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a string value.

        Args:
            plaintext: The sensitive value to encrypt

        Returns:
            Base64-encoded encrypted string, or original if encryption unavailable
        """
        if not plaintext:
            return plaintext

        if not self._fernet:
            logger.debug("Encryption not available, storing plaintext")
            return plaintext

        try:
            encrypted = self._fernet.encrypt(plaintext.encode())
            # Prefix with 'enc:' to identify encrypted values
            return f"enc:{encrypted.decode()}"
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            return plaintext

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt a string value.

        Args:
            ciphertext: The encrypted value (with 'enc:' prefix)

        Returns:
            Decrypted plaintext, or original if not encrypted
        """
        if not ciphertext:
            return ciphertext

        # Check if value is encrypted (has prefix)
        if not ciphertext.startswith("enc:"):
            return ciphertext  # Not encrypted, return as-is

        if not self._fernet:
            logger.warning("Cannot decrypt: encryption not available")
            return ""  # Return empty rather than encrypted gibberish

        try:
            encrypted_data = ciphertext[4:].encode()  # Remove 'enc:' prefix
            decrypted = self._fernet.decrypt(encrypted_data)
            return decrypted.decode()
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            return ""

    def is_available(self) -> bool:
        """Check if encryption is available"""
        return self._fernet is not None


# Global instance for convenience
_encryptor: Optional[CredentialEncryptor] = None


def get_encryptor() -> CredentialEncryptor:
    """Get or create the global encryptor instance"""
    global _encryptor
    if _encryptor is None:
        _encryptor = CredentialEncryptor()
    return _encryptor


def encrypt_credential(value: str) -> str:
    """Convenience function to encrypt a credential"""
    return get_encryptor().encrypt(value)


def decrypt_credential(value: str) -> str:
    """Convenience function to decrypt a credential"""
    return get_encryptor().decrypt(value)
