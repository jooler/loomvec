"""本地账号密码哈希（core/passwords.py）。"""

from __future__ import annotations

from loomvec.core.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    validate_password_strength,
    verify_password,
)


def test_hash_roundtrip_and_salting():
    h1, h2 = hash_password("correct horse"), hash_password("correct horse")
    assert h1 != h2  # 随机盐
    assert h1.startswith("scrypt$")
    assert verify_password("correct horse", h1)
    assert verify_password("correct horse", h2)
    assert not verify_password("correct horsf", h1)


def test_verify_rejects_malformed_hash():
    for stored in ("", "plaintext", "bcrypt$1$2$3$a$b", "scrypt$x$8$1$AA==$AA==", "scrypt$1$2"):
        assert verify_password("anything", stored) is False


def test_unicode_password():
    h = hash_password("密码🔑パスワード")
    assert verify_password("密码🔑パスワード", h)
    assert not verify_password("密码", h)


def test_strength_bounds():
    assert validate_password_strength("x" * (MIN_PASSWORD_LENGTH - 1)) is not None
    assert validate_password_strength("x" * MIN_PASSWORD_LENGTH) is None
    assert validate_password_strength("x" * MAX_PASSWORD_LENGTH) is None
    assert validate_password_strength("x" * (MAX_PASSWORD_LENGTH + 1)) is not None
