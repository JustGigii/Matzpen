import hashlib
import hmac


class OpenWASignatureVerifier:
    """Isolated HMAC verifier; its header name is configuration, not domain logic."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("OpenWA webhook secret cannot be empty")
        self._secret = secret.encode()

    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def verify(self, body: bytes, signature: str | None) -> bool:
        if signature is None:
            return False
        candidate = signature.removeprefix("sha256=")
        return hmac.compare_digest(self.sign(body), candidate)
