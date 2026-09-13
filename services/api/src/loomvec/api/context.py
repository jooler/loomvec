"""请求上下文：身份载体（请求 ID 单源于 core.logging.new_request_id）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    """请求身份（dev JWT 与 OIDC 登录签发的平台 JWT 同构）。"""

    user_id: str
    username: str
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_platform_admin(self) -> bool:
        return "super_admin" in self.roles or "operator" in self.roles
