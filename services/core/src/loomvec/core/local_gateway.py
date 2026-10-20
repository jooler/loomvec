"""本机 acp-gateway（mode=local）的凭据文件回落。

本地模式的网关把租户 API Key 与管理 token 写在它的状态目录里（权限 600）。
loomvec 与网关同机同用户时，`agent.gateway.api_key` / `admin_token`
没有在 admin 里配置就读这些文件，免手工粘贴。

只对回环地址的网关生效（远程网关的凭据不会在本机文件里）；状态目录与文件必须属于当前用户、
组 / 其他人不可写，文件不能是符号链接，否则当作不存在——不信任别人能改写的东西。
"""

from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

API_KEY_FILE = "local-api-key"
ADMIN_TOKEN_FILE = "local-admin-token"
_MAX_SECRET_BYTES = 4096  # 密钥文件很小；防止被指到巨大文件


def default_state_dir() -> Path:
    """与网关同一约定：$XDG_DATA_HOME/acp-gateway，缺省 ~/.local/share/acp-gateway。

    XDG 规范要求忽略相对路径的 XDG_DATA_HOME（网关同样忽略），否则换个 cwd 就指到别的目录。
    """
    base = os.environ.get("XDG_DATA_HOME", "")
    if not base or not os.path.isabs(base):
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "acp-gateway"


def is_loopback_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _untrusted(st: os.stat_result) -> bool:
    """不属于当前用户，或组 / 其他人可写。"""
    return st.st_uid != os.getuid() or bool(st.st_mode & 0o022)


def local_gateway_secret(url: str, filename: str, state_dir: str | None = None) -> str | None:
    """读本机网关状态目录里的密钥文件；不适用 / 不存在 / 不安全 / 为空都返回 None。

    不安全 = 状态目录或文件不属于当前用户、组 / 其他人可写，或文件是符号链接
    （否则别人能把它指向当前用户的任意文件，内容会被当 Bearer 发出去）。
    """
    if not is_loopback_url(url):
        return None
    directory = Path(state_dir).expanduser() if state_dir else default_state_dir()
    try:
        if _untrusted(directory.stat()):
            return None
        # O_NOFOLLOW 拒绝符号链接；校验与读取用同一个 fd，没有先 stat 后 read 的时间差
        # O_NONBLOCK：目标是 FIFO 时不阻塞在 open 上
        fd = os.open(directory / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or _untrusted(st):
                return None
            data = os.read(fd, _MAX_SECRET_BYTES)
        finally:
            os.close(fd)
        return data.decode("utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None
