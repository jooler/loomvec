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
