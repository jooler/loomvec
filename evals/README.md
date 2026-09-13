# evals 检索评测（P1-QA-01）

- `docs/`：评测语料（手册 / 年度报告 / 合同 / 规格表 / FAQ / 企业关系图谱语料，纯 Markdown 走 `POST /assets/text` 入库）
- `images/`：以文搜图图片语料（3 张，预签名直传入库）
- `build_golden.py`：生成金标 `golden.jsonl`（66 条，六类：语义相似 / 精确匹配 / 表格 / 多跳关系 / 全局总结 / 以文搜图）
- `run_evals.py`：全链路回归——语料入库（幂等）→ 等待 ready → 逐条检索 → hit-rate@k / MRR；
  多跳关系与全局总结类用例自动携带 `use_graph=true`

运行（需 compose 栈 + API + worker 在跑）：

```bash
uv run python evals/run_evals.py                 # hit-rate@10 ≥ 0.6 达标
uv run python evals/run_evals.py --no-rerank     # 关闭精排做 A/B 对比（P1 退出标准）
```

金标变更：编辑 `build_golden.py` 中的 GOLDEN 列表后重新生成并提交 diff。
CI：GitHub Actions `workflow_dispatch`（evals job）手动触发，可选。

## P3 图谱类用例（多跳关系 / 全局总结）

- 金标含 `多跳关系`（4 条）与 `全局总结`（2 条）两类，语料见 `docs/企业关系与组织信息.md`（母公司/法人/供应商关系链）；
- runner 对这两类用例自动携带 `use_graph=true`（图谱召回），其余用例不受影响。
