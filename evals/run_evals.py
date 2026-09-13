#!/usr/bin/env python3
"""P1-QA-01 检索评测回归：金标集 → hit-rate@k / MRR。

用法（需全栈与 worker 运行中，AI 网关可用或 mock）：
    uv run python evals/run_evals.py [--api-url http://localhost:8080] \
        [--top-k 10] [--threshold 0.6] [--rerank/--no-rerank]

判定：命中 = top-k 内存在 asset 匹配且正文含 must_contain 的语义单元。
退出码：0 达标 / 1 未达标 / 2 环境不可用。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent

HTTP = json  # 便于阅读


def http_json(
    method: str, url: str, *, token: str | None = None, body: dict | None = None
) -> tuple[int, dict | list]:
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://localhost:8080")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--rerank", dest="rerank", action="store_true", default=True)
    parser.add_argument("--no-rerank", dest="rerank", action="store_true")
    parser.add_argument("--timeout-ready", type=int, default=600)
    args = parser.parse_args()
    base = args.api_url.rstrip("/")

    golden = [
        json.loads(line)
        for line in (ROOT / "golden.jsonl").read_text().splitlines()
        if line.strip()
    ]

    # 1) 登录（dev token）
    status, resp = http_json(
        "POST", f"{base}/api/v1/auth/dev/token", body={"username": "eval-runner"}
    )
    if status != 200:
        print(f"环境不可用：dev token 签发失败 {status}", file=sys.stderr)
        return 2
    token = resp["access_token"]

    # 2) 语料入库（按文件名幂等：已存在则复用；md 走文本直读，png 走图片管线）
    status, existing = http_json("GET", f"{base}/api/v1/assets?limit=100", token=token)
    if status != 200:
        print(f"环境不可用：资产列表失败 {status}", file=sys.stderr)
        return 2
    text_paths = sorted((ROOT / "docs").glob("*.md"))
    image_paths = sorted((ROOT / "images").glob("*.png"))
    corpus_names = {p.name for p in text_paths} | {p.name for p in image_paths}
    by_name = {a["name"]: a for a in existing["items"] if a["name"] in corpus_names}
    for path in text_paths:
        if path.name in by_name:
            continue
        status, created = http_json(
            "POST",
            f"{base}/api/v1/assets/text",
            token=token,
            body={"name": path.name, "content": path.read_text(encoding="utf-8")},
        )
        if status != 201:
            print(f"语料入库失败：{path.name} → {created}", file=sys.stderr)
            return 2
        by_name[path.name] = created
    for path in image_paths:  # P2-QA-03 以文搜图语料
        if path.name in by_name:
            continue
        data = path.read_bytes()
        status, up = http_json(
            "POST",
            f"{base}/api/v1/uploads",
            token=token,
            body={
                "filename": path.name,
                "size": len(data),
                "content_type": "image/png",
            },
        )
        if status != 200:
            print(f"图片预签名失败：{path.name} → {up}", file=sys.stderr)
            return 2
        req = urllib.request.Request(up["upload_url"], method="PUT", data=data)
        req.add_header("Content-Type", "image/png")
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                print(f"图片直传失败：{path.name}", file=sys.stderr)
                return 2
        status, created = http_json(
            "POST",
            f"{base}/api/v1/assets",
            token=token,
            body={
                "key": up["key"],
                "filename": path.name,
                "size": len(data),
                "content_type": "image/png",
            },
        )
        if status != 201:
            print(f"图片登记失败：{path.name} → {created}", file=sys.stderr)
            return 2
        by_name[path.name] = created

    # 3) 等待全部语料 ready
    deadline = time.time() + args.timeout_ready
    pending = corpus_names
    while pending and time.time() < deadline:
        status, page = http_json("GET", f"{base}/api/v1/assets?limit=100", token=token)
        by_status = {a["name"]: a["status"] for a in page["items"] if a["name"] in corpus_names}
        failed = {n for n, st in by_status.items() if st == "failed"}
        if failed:
            print(f"语料处理失败：{failed}", file=sys.stderr)
            return 2
        ready = {n for n, st in by_status.items() if st == "ready"}
        pending = corpus_names - ready
        if pending:
            print(f"等待管线处理：{len(pending)} 篇…")
            time.sleep(5)
    if pending:
        print(f"超时未就绪：{pending}", file=sys.stderr)
        return 2

    # 4) 逐条检索评测
    hits = 0
    rr_sum = 0.0
    by_category: dict[str, list[tuple[bool, float]]] = {}
    for case in golden:
        body: dict = {"query": case["query"], "top_k": args.top_k, "rerank": args.rerank}
        if case.get("type") == "image":  # 以文搜图：限定图片单元
            body["unit_types"] = ["image"]
        if case.get("category") in ("多跳关系", "全局总结"):  # P3-QA-02：图谱召回通道
            body["use_graph"] = True
        status, resp = http_json("POST", f"{base}/api/v1/search", token=token, body=body)
        if status != 200:
            print(f"检索失败：{case['id']} → {resp}", file=sys.stderr)
            return 2
        rank = 0
        for i, hit in enumerate(resp["items"], 1):
            if hit["asset_name"] == case["asset"] and case["must_contain"] in hit["text"]:
                rank = i
                break
        hit_ok = rank > 0
        hits += hit_ok
        rr_sum += 1.0 / rank if rank else 0.0
        by_category.setdefault(case["category"], []).append((hit_ok, 1.0 / rank if rank else 0.0))
        mark = "✅" if hit_ok else "❌"
        print(f"  {mark} {case['id']} [{case['category']}] rank={rank or '-'} {case['query']}")

    total = len(golden)
    hit_rate = hits / total
    mrr = rr_sum / total
    print("=" * 64)
    print(f"hit-rate@{args.top_k} = {hit_rate:.3f}（阈值 {args.threshold}）   MRR = {mrr:.3f}")
    for cat, rs in sorted(by_category.items()):
        hr = sum(1 for ok, _ in rs if ok) / len(rs)
        print(f"  [{cat}] hit-rate={hr:.3f} mrr={sum(r for _, r in rs) / len(rs):.3f} n={len(rs)}")
    return 0 if hit_rate >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
