"""设置 / 重置本地账号密码（写 users.password_hash）。

密码不走命令行参数（避免进 shell 历史 / ps）：默认交互输入两次；`--generate` 生成随机
强密码并打印一次。账号已设密码后，dev 免密签发对它失效，只能用密码登录。
目标库取当前 settings（.env / 环境变量），写前打印库地址与账号供确认。

用法：
    .venv/bin/python scripts/set_password.py alice              # 交互输入
    .venv/bin/python scripts/set_password.py alice --generate   # 生成随机密码
    .venv/bin/python scripts/set_password.py newuser --create   # 账号不存在时新建（挂种子租户）
    .venv/bin/python scripts/set_password.py alice --clear      # 清除密码（恢复 dev 免密）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
import uuid


async def main(args: argparse.Namespace) -> int:
    from sqlalchemy.engine import make_url

    from loomvec.core.config import get_settings
    from loomvec.core.constants import SEED_TENANT_ID
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.db.models import User
    from loomvec.core.db.repos import UserRepo
    from loomvec.core.passwords import hash_password, validate_password_strength

    settings = get_settings()
    target = make_url(settings.postgres.url)
    print(f"目标库：{target.host}:{target.port}/{target.database}  账号：{args.username}")

    password: str | None = None
    if not args.clear:
        if args.generate:
            password = secrets.token_urlsafe(15)
        else:
            password = getpass.getpass("新密码：")
            if password != getpass.getpass("再输一次："):
                print("两次输入不一致", file=sys.stderr)
                return 1
        reason = validate_password_strength(password)
        if reason:
            print(reason, file=sys.stderr)
            return 1

    engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    try:
        async with session_factory() as db:
            user = await UserRepo(db).get_by_username(args.username)
            if user is None:
                if not args.create:
                    print("账号不存在（新建请加 --create）", file=sys.stderr)
                    return 1
                user = User(
                    tenant_id=uuid.UUID(SEED_TENANT_ID),
                    username=args.username,
                    display_name=args.username,
                    auth_source="local",
                )
                db.add(user)
            user.password_hash = hash_password(password) if password else None
            await db.commit()
    finally:
        await engine.dispose()

    if args.clear:
        print("已清除密码（该账号恢复 dev 免密签发）")
    else:
        print("密码已设置" + (f"：{password}（仅显示这一次）" if args.generate else ""))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("username")
    parser.add_argument("--create", action="store_true", help="账号不存在时新建")
    parser.add_argument("--generate", action="store_true", help="生成随机密码并打印")
    parser.add_argument("--clear", action="store_true", help="清除密码")
    sys.exit(asyncio.run(main(parser.parse_args())))
