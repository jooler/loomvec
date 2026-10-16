# 经 acp-gateway 调 Claude 做分片 + 抽取：实测数据

从 `docs/03` 与 `config.py`（`pipeline.llm_concurrency` 注释）里挪出的研究结论，`docs/` 只保留现状。

## 单批耗时与并发

- 每批约 20~50 秒；一篇论文串行（`llm_concurrency=1`）近 10 分钟。
- 单篇论文的总耗时随 `llm_concurrency` 变化：并发 4 / 8 / 16 约 175 / 98 / 72 秒。
- 多篇同时入库时总并发 = 篇数 × `llm_concurrency`，须小于网关 `runtime.max_active_runtimes`（默认 32），
  否则超出的调用被网关拒绝（503），该批走结构化兜底，且 `extraction_failed_batches` 记一批。

## 固定 token 开销

- 网关 OpenAI 兼容接口为 claude 建会话时带 `tools: []`（关闭内置工具）：正文为空的调用输入 5236 token；
  未关闭工具时是 18480（见 acp-gateway `docs/api.md`）。
- 一批 6000 字的调用输入约 1 万 token。
