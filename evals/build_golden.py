#!/usr/bin/env python3
"""P1-QA-01 金标集构建：从 evals/docs 语料生成 查询-期望资产(+定位) 对。

金标条目（golden.jsonl）：
  {"id", "query", "asset", "must_contain", "category"}
- category：语义相似 / 精确匹配 / 表格 / 定位 四类（对齐 03 文档 §四评测口径）。
重新生成：uv run python evals/build_golden.py（输出 evals/golden.jsonl，覆盖前 diff 审阅）。
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent

GOLDEN: list[dict] = [
    # ---- 员工差旅报销手册（语义相似 + 精确匹配）----
    {
        "query": "出差坐高铁能不能坐一等座",
        "asset": "员工差旅报销手册.md",
        "must_contain": "二等座",
        "category": "语义相似",
    },
    {
        "query": "去北京出差住酒店最多能报多少钱一晚",
        "asset": "员工差旅报销手册.md",
        "must_contain": "600 元",
        "category": "语义相似",
    },
    {
        "query": "报销单要在行程结束后多少天内交",
        "asset": "员工差旅报销手册.md",
        "must_contain": "15 个工作日",
        "category": "语义相似",
    },
    {
        "query": "哪些行为属于报销违规",
        "asset": "员工差旅报销手册.md",
        "must_contain": "虚报行程",
        "category": "语义相似",
    },
    {
        "query": "出差吃饭有补贴吗",
        "asset": "员工差旅报销手册.md",
        "must_contain": "50 元包干",
        "category": "语义相似",
    },
    {
        "query": "机票可以买公务舱吗",
        "asset": "员工差旅报销手册.md",
        "must_contain": "6 小时",
        "category": "语义相似",
    },
    {
        "query": "发票丢了怎么报销",
        "asset": "员工差旅报销手册.md",
        "must_contain": "承诺书",
        "category": "语义相似",
    },
    {
        "query": "报销金额多少需要财务总监审批",
        "asset": "员工差旅报销手册.md",
        "must_contain": "20000",
        "category": "精确匹配",
    },
    {
        "query": "发票抬头写什么",
        "asset": "员工差旅报销手册.md",
        "must_contain": "云枢科技",
        "category": "精确匹配",
    },
    {
        "query": "市内打车每天报销上限",
        "asset": "员工差旅报销手册.md",
        "must_contain": "200 元",
        "category": "语义相似",
    },
    {
        "query": "实习生能用差旅报销吗",
        "asset": "员工差旅报销手册.md",
        "must_contain": "实习生",
        "category": "语义相似",
    },
    {
        "query": "协议酒店可以住贵一点的吗",
        "asset": "员工差旅报销手册.md",
        "must_contain": "上浮 10%",
        "category": "语义相似",
    },
    # ---- 2025 年度经营报告 ----
    {
        "query": "公司去年赚了多少钱",
        "asset": "2025年度经营报告.md",
        "must_contain": "1.4 亿",
        "category": "语义相似",
    },
    {
        "query": "2025 年研发投入占营收比例",
        "asset": "2025年度经营报告.md",
        "must_contain": "16.7%",
        "category": "精确匹配",
    },
    {
        "query": "海外业务收入有多少",
        "asset": "2025年度经营报告.md",
        "must_contain": "1.2 亿",
        "category": "语义相似",
    },
    {
        "query": "客户集中度有变化吗",
        "asset": "2025年度经营报告.md",
        "must_contain": "38%",
        "category": "语义相似",
    },
    {
        "query": "公司的 NPS 是多少",
        "asset": "2025年度经营报告.md",
        "must_contain": "62 分",
        "category": "精确匹配",
    },
    {
        "query": "去年申请了多少专利",
        "asset": "2025年度经营报告.md",
        "must_contain": "87 项",
        "category": "精确匹配",
    },
    {
        "query": "2026 年营收目标是多少",
        "asset": "2025年度经营报告.md",
        "must_contain": "16 亿",
        "category": "语义相似",
    },
    {
        "query": "经营有什么风险",
        "asset": "2025年度经营报告.md",
        "must_contain": "竞争加剧",
        "category": "语义相似",
    },
    {
        "query": "应收账款周转天数",
        "asset": "2025年度经营报告.md",
        "must_contain": "96 天",
        "category": "精确匹配",
    },
    {
        "query": "推理成本降了多少",
        "asset": "2025年度经营报告.md",
        "must_contain": "41%",
        "category": "精确匹配",
    },
    {
        "query": "计划在哪里建海外研发中心",
        "asset": "2025年度经营报告.md",
        "must_contain": "马来西亚",
        "category": "语义相似",
    },
    # ---- 软件许可与服务合同 ----
    {
        "query": "系统可用性承诺是多少",
        "asset": "软件许可与服务合同.md",
        "must_contain": "99.9%",
        "category": "精确匹配",
    },
    {
        "query": "出了故障多久响应",
        "asset": "软件许可与服务合同.md",
        "must_contain": "15 分钟",
        "category": "语义相似",
    },
    {
        "query": "服务费怎么付",
        "asset": "软件许可与服务合同.md",
        "must_contain": "季度",
        "category": "语义相似",
    },
    {
        "query": "逾期付款有违约金吗",
        "asset": "软件许可与服务合同.md",
        "must_contain": "万分之五",
        "category": "精确匹配",
    },
    {
        "query": "乙方可以把我们的数据拿去做别的吗",
        "asset": "软件许可与服务合同.md",
        "must_contain": "不得将甲方业务数据",
        "category": "语义相似",
    },
    {
        "query": "数据泄露要多久通知",
        "asset": "软件许可与服务合同.md",
        "must_contain": "24 小时",
        "category": "语义相似",
    },
    {
        "query": "保密义务持续多久",
        "asset": "软件许可与服务合同.md",
        "must_contain": "三年",
        "category": "语义相似",
    },
    {
        "query": "合同可以提前终止吗",
        "asset": "软件许可与服务合同.md",
        "must_contain": "90 日",
        "category": "语义相似",
    },
    {
        "query": "有纠纷去哪里解决",
        "asset": "软件许可与服务合同.md",
        "must_contain": "仲裁",
        "category": "语义相似",
    },
    {
        "query": "合同编号是多少",
        "asset": "软件许可与服务合同.md",
        "must_contain": "LX-2026-0117",
        "category": "精确匹配",
    },
    {
        "query": "授权多少个节点",
        "asset": "软件许可与服务合同.md",
        "must_contain": "50 节点",
        "category": "精确匹配",
    },
    {
        "query": "维护窗口有什么限制",
        "asset": "软件许可与服务合同.md",
        "must_contain": "36 小时",
        "category": "精确匹配",
    },
    # ---- 产品规格与选型表（表格类）----
    {
        "query": "RS-500 用什么 GPU",
        "asset": "产品规格与选型表.md",
        "must_contain": "L20",
        "category": "表格",
    },
    {
        "query": "检索一体机多少钱",
        "asset": "产品规格与选型表.md",
        "must_contain": "38.6 万",
        "category": "表格",
    },
    {
        "query": "边缘网关能耐受多高温度",
        "asset": "产品规格与选型表.md",
        "must_contain": "70°C",
        "category": "表格",
    },
    {
        "query": "HD-900 最大容量多少",
        "asset": "产品规格与选型表.md",
        "must_contain": "737TB",
        "category": "表格",
    },
    {
        "query": "存储节点网络配置是什么",
        "asset": "产品规格与选型表.md",
        "must_contain": "100GbE",
        "category": "表格",
    },
    {
        "query": "EG-200 的防护等级",
        "asset": "产品规格与选型表.md",
        "must_contain": "IP65",
        "category": "表格",
    },
    {
        "query": "批量采购有折扣吗",
        "asset": "产品规格与选型表.md",
        "must_contain": "92 折",
        "category": "语义相似",
    },
    {
        "query": "旧款 RS-300 还能升级吗",
        "asset": "产品规格与选型表.md",
        "must_contain": "RS-300",
        "category": "精确匹配",
    },
    {
        "query": "RS-500 需要什么平台版本",
        "asset": "产品规格与选型表.md",
        "must_contain": "V3.2",
        "category": "精确匹配",
    },
    {
        "query": "延保一年多少钱",
        "asset": "产品规格与选型表.md",
        "must_contain": "6%",
        "category": "表格",
    },
    # ---- 平台使用常见问题 ----
    {
        "query": "密码忘了怎么办",
        "asset": "平台使用常见问题.md",
        "must_contain": "自助重置",
        "category": "语义相似",
    },
    {
        "query": "账号被锁了多久解锁",
        "asset": "平台使用常见问题.md",
        "must_contain": "15 分钟",
        "category": "精确匹配",
    },
    {
        "query": "支持单点登录吗",
        "asset": "平台使用常见问题.md",
        "must_contain": "OIDC",
        "category": "精确匹配",
    },
    {
        "query": "最大能上传多大的文件",
        "asset": "平台使用常见问题.md",
        "must_contain": "200MB",
        "category": "精确匹配",
    },
    {
        "query": "为什么我的 PDF 解析失败",
        "asset": "平台使用常见问题.md",
        "must_contain": "加密 PDF",
        "category": "语义相似",
    },
    {
        "query": "怎么精确搜索一个短语",
        "asset": "平台使用常见问题.md",
        "must_contain": "引号",
        "category": "语义相似",
    },
    {
        "query": "分享链接有效期多久",
        "asset": "平台使用常见问题.md",
        "must_contain": "7 天",
        "category": "精确匹配",
    },
    {
        "query": "免费版有多少检索额度",
        "asset": "平台使用常见问题.md",
        "must_contain": "1000 次",
        "category": "精确匹配",
    },
    {
        "query": "viewer 能上传文件吗",
        "asset": "平台使用常见问题.md",
        "must_contain": "仅可检索",
        "category": "语义相似",
    },
    {
        "query": "删除资产后向量会清理吗",
        "asset": "平台使用常见问题.md",
        "must_contain": "清理向量",
        "category": "语义相似",
    },
    {
        "query": "API 调用怎么计费",
        "asset": "平台使用常见问题.md",
        "must_contain": "0.002 元",
        "category": "精确匹配",
    },
    {
        "query": "扫描件支持识别吗",
        "asset": "平台使用常见问题.md",
        "must_contain": "OCR",
        "category": "语义相似",
    },
]


def main() -> None:
    out = ROOT / "golden.jsonl"
    lines = []
    for i, item in enumerate(GOLDEN, 1):
        entry = {"id": f"q{i:03d}", **item}
        lines.append(json.dumps(entry, ensure_ascii=False))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"金标集已写入 {out}：{len(GOLDEN)} 条")
    # 校验 must_contain 均在语料中出现（构建期断言）
    for item in GOLDEN:
        doc = (ROOT / "docs" / item["asset"]).read_text(encoding="utf-8")
        assert item["must_contain"] in doc, (
            f"金标短语缺失：{item['asset']} / {item['must_contain']}"
        )
    print("全部 must_contain 已在语料中校验通过 ✅")


if __name__ == "__main__":
    main()
