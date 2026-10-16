"""编辑器访问密码：密码哈希、失败锁定与内存 token。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING

from ..core.constants import LOCKOUT_SECONDS, MAX_FAILED_ATTEMPTS, TOKEN_TTL_SECONDS

if TYPE_CHECKING:
    from .. import MyWorldPlugin


# ======================================================================
# 编辑器访问密码（AstrBot Dashboard 之外的二次保护，可选）
# ======================================================================


class EditorAuth:
    """独立访问密码：密码哈希存 sqlite，token 只在内存里，含失败锁定。"""

    def __init__(self, plugin: "MyWorldPlugin", config_password: str = "") -> None:
        self.plugin = plugin
        self.config_password = (config_password or "").strip()
        self.tokens: dict[str, float] = {}
        self.failures: list[float] = []
        self.lock_until = 0.0

    # ---------------- 密码 ----------------

    def stored_hash(self) -> dict[str, str]:
        return self.plugin.db.raw.kv_get("editor_password", {}) or {}

    def has_password(self) -> bool:
        return bool(self.config_password or self.stored_hash().get("hash"))

    def set_password(self, raw: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            self.plugin.db.raw.kv_delete("editor_password")
            return
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt.encode("utf-8"), 100_000)
        self.plugin.db.raw.kv_set(
            "editor_password", {"salt": salt, "hash": digest.hex()}
        )

    def verify(self, raw: str) -> bool:
        raw = (raw or "").strip()
        if not raw:
            return False
        stored = self.stored_hash()
        if stored.get("hash"):
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                raw.encode("utf-8"),
                str(stored.get("salt", "")).encode("utf-8"),
                100_000,
            ).hex()
            if hmac.compare_digest(digest, str(stored["hash"])):
                return True
        if self.config_password:
            return hmac.compare_digest(raw, self.config_password)
        return False

    # ---------------- 锁定与 token ----------------

    def locked(self) -> int:
        if self.lock_until and time.time() < self.lock_until:
            return int(self.lock_until - time.time())
        return 0

    def note_failure(self) -> None:
        now = time.time()
        self.failures = [ts for ts in self.failures if now - ts < LOCKOUT_SECONDS]
        self.failures.append(now)
        if len(self.failures) >= MAX_FAILED_ATTEMPTS:
            self.lock_until = now + LOCKOUT_SECONDS
            self.failures.clear()

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(24)
        self.tokens[token] = time.time() + TOKEN_TTL_SECONDS
        self._prune()
        return token

    def revoke(self, token: str) -> None:
        self.tokens.pop(token, None)

    def check(self, token: str) -> bool:
        if not self.has_password():
            return True
        if not token:
            return False
        expires = self.tokens.get(token)
        if not expires or expires < time.time():
            self.tokens.pop(token, None)
            return False
        return True

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, exp in self.tokens.items() if exp < now]:
            self.tokens.pop(token, None)
