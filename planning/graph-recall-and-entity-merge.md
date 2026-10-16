# 图谱召回与实体合并：现状、已改、待办

2026-10-16 排查「开图谱召回的检索 100s+」时的发现与处理。写给正在做抽取领域配置 / 重抽 / 实体清理的会话：
重抽会重新触发实体合并，请先读完「重抽前必须知道的」。

## 已改（未提交；api 已热加载、worker 已重启，均已生效）

| 问题 | 原因 | 处理 |
|---|---|---|
| 开图谱召回单次检索 100s+ | `load_space_edges` / `delete_asset_edges` 用无向匹配，AGE 译成带 OR 的连接条件，退化为「全图边 × 空间实体」嵌套循环，且每条边返回两次 | 全空间扫边改有向 `(a)-[r]->(b)`（`graph/age.py`）；最大空间 100s+ → 48ms |
| 图谱召回从未生效 | `MilvusStore.search_entities` 经 `hybrid_search + RRFRanker` 返回排名分（恒约 0.016），与相似度阈值比较永远不命中 | 改单路 `client.search`，返回 IP 相似度（`retrieval/store.py`）；`graph.entity_link_threshold` 改为查询侧阈值，默认 0.90 → 0.65 |
| 入库侧向量链接 | 同上，从未生效；一旦修好分数，同家族基因（RUNX1/RUNX2、BRCA1/BRCA2 相似度 0.95+）会被入库时合并 | 删除 `graph_flow._link_vector`：入库只做规范名精确匹配，近似重复交给阶段二 |
| 阶段二自动合并约 1/3 错并 | 向量 ≥0.94 与名称编辑距离 ≤2 都会把「差一个字符的不同实体」合在一起；同空间内最近邻相似度 ≥0.95 的实体对大多是此类（RUNX1/RUNX2、BRCA1/BRCA2） | `graph/merge.py` 只收 `variant_key` 相同的同类型实体（规范名归一化之上，仅再容忍末尾复数 s 与括号里的大写缩写）；删除 `vector_candidates`、`_edit_distance`、`find_candidates`（无调用方）、配置项 `merge_threshold` / `merge_name_distance`、`run_space_merge` 的 `vector_fetch` 参数 |
| 图谱行形状 | `query_units` 返回扁平行，融合/重排读 `row["entity"]` → 图谱一有结果就 KeyError | `query_units` 返回与 `hybrid_search` 同形的 `{id, distance, entity}` |
| 图谱读取随机失败 | `AgeStore._prep` 按连接只设一次 `search_path`，只读请求回滚时被撤销，池连接复用后参数化 Cypher 报 `cannot cast agtype string to type boolean` | 改为每个事务重设（`SET LOCAL`，标记存 `session.info`）；`CREATE EXTENSION` 移到 `ensure_graph` |
| 图谱降级变 500 | 图读取失败被吞掉，但外层 PG 事务已 aborted，后续 `enrich_hits` 全部失败 | `recall` / `adjacency_neighbors` 的图读取包在 `session.begin_nested()` 保存点里 |

测试：`services/core/tests/test_entity_search.py`、`test_p3_graph.py`（合并候选，含线上错并样例）；
`services/api/tests/test_integration_age_store.py`（testcontainers，覆盖有向扫边、事务回滚后的连接复用、保存点降级）。

## 重抽前必须知道的

- 线上 celery worker 已于 2026-10-16 21:52 重启，新合并规则与入库路径已生效（此前每 30 分钟的合并任务按旧规则错并）。
- 已发生的错并（统计于 2026-10-16 21:50，新规则生效前）：`entity_merge_log` 里 `auto_vector` 1488 条、`auto_name` 10657 条，抽样约 1/3 错误（UBE2A/UBE2B、STAG1/STAG2、SEMA3A/SEMA5A、CD4+ T/CD8+ T、Th1/Th2、Conventional/Unconventional protein secretion、GK+/GK- Treg、RAC1/ERCC1、Wnt/AKT pathway……）。
- 已决定：**不逐条回滚，等重抽整体替换**。重抽按资产覆盖边，旧实体行 / AGE 孤立节点 / Milvus 向量由 `scripts/gc_entities.py` 在全部重抽完后清理；重抽完成前，存量错并实体仍在图里。

## 复审后追加（2026-10-16 22:18 worker 第二次重启后生效）

- 第一版 `variant_key` 的括号缩写规则过宽（任意括号内 2–8 位大写字母数字都剥离），把 `T cells (CD4)` / `(CD8)`、`E. coli BL21(DE3)` / `BL21`、`ESM-2` / `ESM-2 (3B)` 合在一起；复数规则会把型号后缀 `CytoFLEX S` 并进 `CytoFlex`。新规则生效到修正期间自动合并了约 430 条，按修正后规则判不成立的 80 条里大多仍是正确合并（单个词加它的缩写，如 `glioblastoma (GBM)` / `glioblastoma`），真正错的是少数。修正后只剥离「能由括号前各词首字母推出」的纯字母缩写，复数只认原写法里小写的词尾 s；代价是单词加缩写这类漏并。
- 这批错并同样不单独回滚，等重抽整体替换。
- `_prep` 的标记改记最内层（子）事务，保存点回滚后会重设 `search_path`（审查在 `folders.py` 删文件夹循环里复现过）。
- 无人工合并入口：`merge_pair` 只被 `run_space_merge` 调用，`MERGE_REASON_MANUAL` 仅测试使用。

## 已决定

- 检索侧 `entity_link_threshold` 保持 0.65，不调整。

## 待评估

- 图谱对最终结果的增益：4 个关系型问题里只有 1 个让 top 8 多出一条，且那条关系证据（MIBI-COMPARED_WITH->CODEX）与问题相关性一般。图谱质量（噪音实体、泛化关系）可能比召回机制更限制效果，建议重抽 + 实体过滤落地后再评估。

## 遗留

- `MilvusStore.fetch_entity_vectors` / `async_fetch_entity_vectors` 已无调用方（原用于合并的向量候选），可删。
- 检索质量评测集缺失：无法量化图谱开/关的差异，上面第 3 点只是少量手测。

## 交接（2026-10-17 00:30）

### 当前线上状态

- 代码：本地 `main` 已有 5 个相关提交（`771053e` MCP PAT、`9288e1a` 图谱召回/合并修复、`1423688` 抽取领域配置、`628ac89` 实体链接超取、`e6df266` 社区时间修复，另有一个 docs 提交），**均未推送远程**。api 热加载、worker 已于 00:14 重启，线上跑的就是这些代码。
- 重抽进度（只做了试跑，**没有整批开跑**）：
  - 「单细胞文章」（1 篇）：已重抽并已 `gc_entities --apply`；
  - 「最新文章」（54 篇）：5 篇已是 `chunk-graph-v5`，49 篇仍是 `chunk-graph-v4`，全部 `ready`、无在途。**不要对这个空间跑 `gc_entities --apply`**，要等 54 篇全部重抽完；
  - 「虚拟细胞」68、「合成致死」67、「论文自动入库」约 240 篇 ready + 250 在途，均未动。
- 试跑结果（单细胞文章，1 篇）：v4→v5，实体 88→78、关系 87→81，166 秒，无失败批次；新抽取无人名/机构类噪音，恢复了 ProtBAG/MetBAG/MRIBAG 等核心概念；也丢了 GDF15、brain age 等孤立实体（v5 只保留参与关系的实体）。重抽 + gc 后，问「血浆蛋白组学衰老」的结果带上了关系证据（`biological aging clock → EVALUATED_ON → FinnGen`）；但 4 个关系型问题里图谱对排序的增益仍有限，需要更多空间重抽后再评估。
- 线上测试残留已清理（9 个测试租户、14 个 epf 用户、7 个 epf 空间、`search.rrf_k=42`、`ai.embedding.api_key=sk-secret-123` 两个旧测试值）；`rrf_k` 已回到默认 60。

### 明天接着做

1. 决定是否继续重抽，以及范围。命令（默认 dry-run，`--apply` 才执行；同一命令可续跑）：
   `.venv/bin/python scripts/reextract_graph.py --space <uuid>`（先看列表）
   `.venv/bin/python scripts/reextract_graph.py --space <uuid> --apply --limit N --inflight 2`
   并发 `--inflight × llm_concurrency` 上限 16；约 170 秒/篇，`--inflight 2` 时 54 篇约 75 分钟。重抽期间该论文处于「处理中」，暂时搜不到。
2. 某个空间**全部**重抽完、且没有失败批次后，再 `.venv/bin/python scripts/gc_entities.py --space <uuid>`（dry-run）→ `--apply`。
3. 用 `scripts/audit_entities.py` 复核噪音命中率，用 dev 用户 JWT 调 `/api/v1/search`（`use_graph` true/false）对比增益。
4. 历史错并（`auto_vector` 1488 + `auto_name` 约 11000 条）不回滚，靠重抽整体替换；`LLM分片与图谱试验` 空间里还有旧实体向量，重抽/清理该空间后才会消失（检索侧已对残留向量容错）。

### 注意事项

- **跑 api / worker 测试前必须覆盖数据库地址**：`export LOOMVEC_POSTGRES__URL=postgresql+asyncpg://x:x@127.0.0.1:1/none`；`env -u` 无效（`.env` 会重新加载）。api 测试已加硬防护，worker 和 agent 的测试没有。
- 重启 worker：用 `tmp/dev.pids` 里记录的 pid 发 `kill -TERM`（温和重启，在跑的任务做完、未开始的放回队列），再用 `.venv/bin/celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low` 起新的。**不要用 `pgrep -f`/`pkill -f` 找 pid**：会匹配到自己的 shell 命令行而被误杀。
- 「派致_售前」空间被设成了 biomedical 配置（会过滤员工、供应商），用户决定不改；若它其实是售前资料，重抽它之前要先确认。
- 已知遗留（与这批改动无关）：`test_create_unit_sync_embed` 在提交前就失败；`wizardly_khayyam` 是个不明来源的 Docker 测试容器；ACP 网关会话的 18 个改动仍未提交（`docs/19`、`services/agent/**`、`local_gateway.py` 等）。
