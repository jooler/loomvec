# third_party 目录说明

本目录存放第三方上游项目（git submodule 形式）：

> **MinerU 不在本目录**：解析引擎自 4.0.10 起以 PyPI 固定版本依赖装入解析镜像
> （`deploy/compose/mineru/Dockerfile` 的 `MINERU_VERSION`，升级只改这一处）；
> 项目经 HTTP 调用 mineru-api，不 import 其源码，故不再 vendor。P1~P4 曾以
> vendor 拷贝引入（3.4.5 → 4.0.10），经核对无本地修改后移出入库。

| 目录 | 方式 | 说明 |
| --- | --- | --- |
| `deepseek-harness/` | **git submodule** | P5 起的 dsh 智能体运行时（协议参考与本地联调） |

## deepseek-harness（dsh）使用与更新

- **运行时依赖不来自本目录**：`services/agent` 经 PyPI 安装
  `deepseek-harness-sdk==<精确版本>`（uv.lock 锁定，wheel 自带 Node 运行时）。
- **本目录用途**：协议事实核对（session JSONL 格式、事件词汇表、approval 语义）
  与本地调试；不参与构建产物。
- **克隆**：`git submodule update --init --depth 1`。
- **升级步骤**（对齐 14 文档 §11 的独立变更单要求）：
  1. `cd third_party/deepseek-harness && git fetch --tags && git checkout dsh-vX.Y.Z`
  2. 更新 `services/agent/pyproject.toml` 中 `deepseek-harness-sdk` 精确版本并 `uv lock`
  3. `uv run python scripts/smoke/agent_protocol.py`（CI 亦会跑，全绿方可合并）
  4. 主仓提交 submodule 指针 + 依赖版本（同一变更单）
- submodule 固定与 SDK 同版本的 tag，避免「源码参考」与「实际运行时」漂移。
