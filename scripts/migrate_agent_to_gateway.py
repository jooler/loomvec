"""智能体存量数据迁移到 acp-gateway（P2 合同 §6，L2）。

对每个 agent_environment：
1. 确保网关 workspace（external_ref = env_id；同时写 AGENTS.md），
   把 `{agent.storage_root}/envs/{env_id}/workspace/` 同步进网关 files/
   （保留相对路径；跳过符号链接、隐藏目录——`.loomvec/uploads` 除外——和根部 AGENTS.md）；
2. 对该 env 下尚未迁移的 dsh 旧会话：解析 JSONL → `sessions:import`，
   导入后回填 `agent_session.gateway_session_id`（agent 保持 dsh、mcp_token_exp 置空，
   首次继续对话时由适配层续签 MCP token）。

可重入：已有 gateway_session_id 的会话跳过；网关侧 import_key（= agent_session.id.hex）
同样幂等；网关上已存在的同名文件 / 已被改写的 instructions 默认**不覆盖**（`--overwrite` 才覆盖）。
迁移须在放开用户提问之前完成：未迁移的旧会话一旦被提问，适配层会为它新建空的网关会话，
旧历史随之不可见，脚本也会把它当成已迁移跳过。导入会话需要网关管理 token
（admin 设置 `agent.gateway.admin_token`）；文件与 workspace 用租户 API Key
（`agent.gateway.api_key`）。

用法：
    uv run python scripts/migrate_agent_to_gateway.py --dry-run          # 只统计，不写网关 / 库
    uv run python scripts/migrate_agent_to_gateway.py                    # 全量迁移
    uv run python scripts/migrate_agent_to_gateway.py --env-id <uuid>    # 只迁指定 env（可重复）
    uv run python scripts/migrate_agent_to_gateway.py --skip-files       # 只迁会话
    uv run python scripts/migrate_agent_to_gateway.py --skip-sessions    # 只同步文件
    uv run python scripts/migrate_agent_to_gateway.py --overwrite        # 覆盖网关上的同名内容
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CHUNK = 1024 * 1024
IMPORT_TIMEOUT_S = 300.0


@dataclass
class Stats:
    envs: int = 0
    files_uploaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    bytes_uploaded: int = 0
    sessions_imported: int = 0
    sessions_skipped: int = 0  # 已迁移
    sessions_empty: int = 0  # 无任何对话，留给适配层懒创建
    sessions_failed: int = 0
    turns: int = 0
    failures: list[str] = field(default_factory=list)

    def fail(self, what: str) -> None:
        self.failures.append(what)


def describe(exc: BaseException, *secrets: str) -> str:
    """异常的可打印描述：带类型名（有的异常 str 为空），并抹掉凭据明文。"""
    text = f"{type(exc).__name__}: {exc}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    from loomvec.agent.gateway.legacy_import import open_regular_nofollow

    with open_regular_nofollow(path) as f:
        while chunk := f.read(CHUNK):
            yield chunk


async def sync_files(
    client: Any,
    workspace_id: str,
    workspace: Path,
    stats: Stats,
    *,
    dry_run: bool,
    overwrite: bool = False,
):
    from loomvec.agent.gateway.errors import GatewayError
    from loomvec.agent.gateway.legacy_import import iter_workspace_files

    for lf in iter_workspace_files(workspace):
        if dry_run:
            stats.files_uploaded += 1
            stats.bytes_uploaded += lf.size
            continue
        try:
            if not overwrite:
                try:
                    meta = await client.stat_file(workspace_id, lf.rel)
                    if meta.get("type") == "file":  # 网关已有同名文件：不动（可能已被用户改过）
                        stats.files_skipped += 1
                        continue
                except GatewayError as e:
                    if e.status not in (400, 404):
                        raise
            # 空文件没有字节流可发：直接发空 bytes
            content = b"" if lf.size == 0 else _chunks(lf.path)
            await client.put_file(workspace_id, lf.rel, content, size=lf.size)
            stats.files_uploaded += 1
            stats.bytes_uploaded += lf.size
        except Exception as e:  # 单个文件（含非 UTF-8 文件名）失败不拖垮其余
            stats.files_failed += 1
            stats.fail(f"file {workspace_id}:{lf.rel!a}: {describe(e)}")


async def sync_user_agents_md(
    client: Any,
    workspace_id: str,
    workspace: Path,
    owner: Any,
    *,
    dry_run: bool,
    overwrite: bool = False,
) -> bool:
    """用户改写过的 workspace/AGENTS.md → 网关 instructions。返回是否（将）写入。

    - 仍是旧渲染（只比正文，租户 / 用户改名不影响）的由新模板接管，不搬运；
    - 搬运 = 新模板 + 用户原文附段（保住企业规范层），不带模板标记；
    - 网关上的 instructions 已被改写（无模板标记）时不覆盖，除非 `overwrite`。
    """
    from loomvec.agent.gateway import instructions as instr
    from loomvec.agent.gateway.legacy_import import (
        carried_instructions,
        is_unmodified_legacy_agents_md,
        open_regular_nofollow,
    )

    src = workspace / "AGENTS.md"
    if not src.is_file() or src.is_symlink():
        return False
    with open_regular_nofollow(src) as f:
        text = f.read().decode("utf-8", errors="replace")
    legacy = instr._legacy_workspace_render(owner.tenant_name, owner.user_display_name)
    if not text.strip() or is_unmodified_legacy_agents_md(text, legacy):
        return False
    if dry_run:
        return True
    existing = await client.get_instructions(workspace_id)
    managed = not (existing or "").strip() or bool(instr._MARKER_RE.match(existing or ""))
    if not managed and not overwrite:
        return False
    content = carried_instructions(
        instr.render_instructions(
            tenant_name=owner.tenant_name, user_display_name=owner.user_display_name
        ),
        text,
    )
    if existing == content:
        return False
    await client.put_instructions(workspace_id, content)
    return True


async def import_session(
    http: httpx.AsyncClient,
    admin_token: str,
    workspace_id: str,
    body: dict[str, Any],
) -> tuple[str, bool]:
    """POST sessions:import → (gateway session id, 是否新建)。"""
    resp = await http.post(
        f"/admin/v1/workspaces/{workspace_id}/sessions:import",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=IMPORT_TIMEOUT_S,
    )
    if resp.status_code not in (200, 201):
        # 不回显请求体（含对话内容）与凭据，只带状态与网关错误码
        try:
            err = resp.json().get("error", {})
            detail = f"{err.get('code')}: {str(err.get('message'))[:200]}"
        except ValueError:
            detail = "非 JSON 响应"
        raise RuntimeError(f"HTTP {resp.status_code} {detail}")
    return str(resp.json()["id"]), resp.status_code == 201


async def migrate(args: argparse.Namespace) -> int:
    from sqlalchemy import select, update

    from loomvec.agent.config import load_agent_config, load_gateway_connection
    from loomvec.agent.gateway.client import make_http_client
    from loomvec.agent.gateway.legacy_import import build_import_body, load_session_messages
    from loomvec.agent.gateway.service import GatewayService, OwnerContext
    from loomvec.api.services.admin_settings import effective_value
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.db.models import AgentEnvironment, AgentSession, Tenant, User

    cfg = load_agent_config()
    engine, session_factory = create_engine_and_sessionmaker(cfg.settings.postgres)
    stats = Stats()
    http = make_http_client(cfg.agent.gateway.url)
    try:
        async with session_factory() as db:
            conn = await load_gateway_connection(db, cfg.agent)
            admin_token = str(
                (await effective_value(db, "agent.gateway.admin_token")) or ""
            ).strip()
        if not args.dry_run:
            if not conn.api_key:
                print("错误：未配置 agent.gateway.api_key", file=sys.stderr)
                return 2
            if not args.skip_sessions and not admin_token:
                print("错误：未配置 agent.gateway.admin_token（导入会话需要）", file=sys.stderr)
                return 2
            if not await GatewayService(cfg, http).client_for(conn).healthz():
                print(f"错误：网关不可达 {cfg.agent.gateway.url}", file=sys.stderr)
                return 2

        service = GatewayService(cfg, http)
        client = service.client_for(conn)
        print(f"storage_root={cfg.storage_root}  gateway={cfg.agent.gateway.url}")

        async with session_factory() as db:
            stmt = select(AgentEnvironment).order_by(AgentEnvironment.created_at)
            if args.env_id:
                stmt = stmt.where(AgentEnvironment.id.in_([uuid.UUID(e) for e in args.env_id]))
            envs = list((await db.execute(stmt)).scalars())

        for env in envs:
            stats.envs += 1
            env_id = str(env.id)
            workspace = cfg.workspace(env_id)
            async with session_factory() as db:
                user = await db.get(User, env.owner_user_id) if env.owner_user_id else None
                tenant = await db.get(Tenant, env.tenant_id) if env.tenant_id else None
                owner = OwnerContext(
                    user_id=str(env.owner_user_id or ""),
                    tenant_id=str(env.tenant_id) if env.tenant_id else None,
                    tenant_name=tenant.name if tenant else "LoomVec",
                    user_display_name=(user.display_name if user else None) or "用户",
                )
                workspace_id = env.gateway_workspace_id
                if not args.dry_run:
                    try:
                        db_env = await db.get(AgentEnvironment, env.id)
                        assert db_env is not None
                        workspace_id = await service.ensure_workspace(client, db, db_env, owner)
                        await db.commit()
                    except Exception as e:
                        await db.rollback()
                        stats.fail(f"env {env_id}: workspace: {describe(e, conn.api_key or '')}")
                        continue
            print(f"[env {env_id}] workspace={workspace_id or '(dry-run: 将创建)'}")
            if not workspace.is_dir():
                print(
                    f"[env {env_id}] 警告：本地目录不存在 {workspace}"
                    "（storage_root 配错？新环境可忽略）",
                    file=sys.stderr,
                )

            if not args.skip_files:
                try:
                    if await sync_user_agents_md(
                        client,
                        workspace_id or "",
                        workspace,
                        owner,
                        dry_run=args.dry_run,
                        overwrite=args.overwrite,
                    ):
                        print(f"[env {env_id}] AGENTS.md → instructions")
                except Exception as e:
                    stats.fail(f"env {env_id}: AGENTS.md: {describe(e, conn.api_key or '')}")
                await sync_files(
                    client,
                    workspace_id or "",
                    workspace,
                    stats,
                    dry_run=args.dry_run,
                    overwrite=args.overwrite,
                )

            if args.skip_sessions:
                continue
            async with session_factory() as db:
                sstmt = select(AgentSession).where(
                    AgentSession.env_id == env.id,
                    AgentSession.gateway_session_id.is_(None),
                    AgentSession.agent == "dsh",
                )
                if not args.include_archived:
                    sstmt = sstmt.where(AgentSession.archived_at.is_(None))
                rows = list((await db.execute(sstmt.order_by(AgentSession.created_at))).scalars())
                skipped = (
                    await db.execute(
                        select(AgentSession.id).where(
                            AgentSession.env_id == env.id,
                            AgentSession.gateway_session_id.is_not(None),
                        )
                    )
                ).all()
            stats.sessions_skipped += len(skipped)

            for row in rows:
                try:
                    messages = load_session_messages(
                        cfg.sessions_root(env_id), workspace, row.id.hex
                    )
                    body = build_import_body(
                        messages,
                        import_key=row.id.hex,
                        title=row.title,
                        cwd=row.project_path,
                        model=conn.model_for("dsh"),
                        created_at=row.created_at,
                        metadata={"loomvec_session_id": row.id.hex, "loomvec_env_id": env_id},
                    )
                except Exception as e:  # 坏 JSONL / 读权限：记失败，继续下一个会话
                    stats.sessions_failed += 1
                    stats.fail(f"session {row.id.hex}: 解析失败 {describe(e)}")
                    continue
                if body is None:
                    if row.message_count > 0:  # 库里有消息却解析不出：多半是 storage_root 不对
                        stats.sessions_failed += 1
                        stats.fail(
                            f"session {row.id.hex}: 库中 message_count={row.message_count}"
                            " 但找不到可导入的对话（检查 storage_root）"
                        )
                    else:
                        stats.sessions_empty += 1
                    continue
                if args.dry_run:
                    stats.sessions_imported += 1
                    stats.turns += len(body["turns"])
                    continue
                try:
                    gw_id, created = await import_session(http, admin_token, workspace_id, body)
                except Exception as e:
                    stats.sessions_failed += 1
                    stats.fail(f"session {row.id.hex}: {describe(e, admin_token)}")
                    continue
                async with session_factory() as db:
                    res = await db.execute(
                        update(AgentSession)
                        .where(AgentSession.id == row.id, AgentSession.gateway_session_id.is_(None))
                        .values(
                            gateway_session_id=gw_id,
                            agent="dsh",
                            mcp_token_exp=None,
                            replay_pending=True,
                        )
                    )
                    await db.commit()
                if res.rowcount != 1:  # 导入期间用户已提问并建了新的网关会话
                    stats.sessions_failed += 1
                    stats.fail(
                        f"session {row.id.hex}: 已导入为 {gw_id}，但库里该会话已绑定别的网关会话"
                        "（导入期间被提问过？），需人工处理"
                    )
                    continue
                stats.sessions_imported += 1
                stats.turns += len(body["turns"])
                print(f"[session {row.id.hex}] → {gw_id} ({'新建' if created else '已存在'})")
    finally:
        await http.aclose()
        await engine.dispose()

    mode = "【dry-run，未写入】" if args.dry_run else ""
    print(
        f"\n{mode}envs={stats.envs}\n"
        f"  文件：上传 {stats.files_uploaded}（{stats.bytes_uploaded} 字节）"
        f"，跳过 {stats.files_skipped}，失败 {stats.files_failed}\n"
        f"  会话：导入 {stats.sessions_imported}（{stats.turns} 回合）"
        f"，已迁移 {stats.sessions_skipped}，空会话 {stats.sessions_empty}"
        f"，失败 {stats.sessions_failed}"
    )
    for line in stats.failures:
        print(f"  ! {line}", file=sys.stderr)
    return 1 if stats.failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写网关与数据库")
    parser.add_argument("--env-id", action="append", default=[], help="只迁指定 env（可重复）")
    parser.add_argument("--skip-files", action="store_true", help="不同步 workspace 文件")
    parser.add_argument("--skip-sessions", action="store_true", help="不导入会话")
    parser.add_argument("--include-archived", action="store_true", help="同时导入已归档的会话")
    parser.add_argument(
        "--overwrite", action="store_true", help="用旧文件 / 旧 AGENTS.md 覆盖网关上已有的同名内容"
    )
    args = parser.parse_args()
    try:
        for e in args.env_id:
            uuid.UUID(e)
    except ValueError:
        parser.error("--env-id 必须是 uuid")
    return asyncio.run(migrate(args))


if __name__ == "__main__":
    sys.exit(main())
