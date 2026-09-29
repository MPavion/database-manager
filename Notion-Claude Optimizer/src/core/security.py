import os
from cryptography.fernet import Fernet
from src.core.config import KEY_PATH, logger

class SecurityManager:
    def __init__(self):
        self.key = self._load_or_generate_key()
        self.cipher = Fernet(self.key)

    def _load_or_generate_key(self) -> bytes:
        if KEY_PATH.exists():
            with open(KEY_PATH, "rb") as f:
                return f.read()
        else:
            new_key = Fernet.generate_key()
            with open(KEY_PATH, "wb") as f:
                f.write(new_key)
            logger.info("Generated new security key for token encryption.")
            return new_key

    def encrypt(self, data: str) -> str:
        if not data: return ""
        return self.cipher.encrypt(data.encode()).decode()

    def decrypt(self, encrypted_data: str) -> str:
        if not encrypted_data: return ""
        try:
            return self.cipher.decrypt(encrypted_data.encode()).decode()
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            return ""
