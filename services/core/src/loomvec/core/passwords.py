"""本地账号密码哈希（stdlib scrypt，无第三方依赖）。

存储格式 `scrypt$<N>$<r>$<p>$<salt_b64>$<hash_b64>`：参数随哈希存储，
日后调高成本参数不影响已有哈希的校验。API 登录路由与 `scripts/set_password.py` 共用。
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets

_SCHEME = "scrypt"
_N = 2**15
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
# scrypt 内存 ≈ 128·N·r 字节（默认参数约 32MiB）；maxmem 留足余量
_MAXMEM = 128 * 1024 * 1024

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES, maxmem=_MAXMEM
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _N, _R, _P)
    b64 = base64.b64encode
    return f"{_SCHEME}${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验；哈希格式不识别返回 False（不抛）。"""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != _SCHEME:
            return False
        expected = base64.b64decode(digest_b64)
        actual = _derive(password, base64.b64decode(salt_b64), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@functools.cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def verify_dummy() -> None:
    """账号不存在 / 未设密码时也做一次等价校验，登录耗时不泄露账号是否存在。"""
    verify_password("x", _dummy_hash())


def validate_password_strength(password: str) -> str | None:
    """不合格返回原因，合格返回 None。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"密码至少 {MIN_PASSWORD_LENGTH} 位"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"密码最长 {MAX_PASSWORD_LENGTH} 位"
    return None
