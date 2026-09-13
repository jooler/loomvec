"""P0-INF-06 冒烟：MinerU 可解析样例 PDF（pipeline 后端，mineru-api /file_parse）。

用法：uv run python scripts/smoke/smoke_mineru.py [--base-url http://localhost:8000]
样例取自 vendored MinerU 源码 demo；首次调用可能触发模型加载，耐心等待。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import httpx

BASE_URL = "http://localhost:8000"
SAMPLE = pathlib.Path(__file__).resolve().parents[2] / "third_party/mineru/demo/pdfs"


def find_sample_pdf() -> pathlib.Path:
    pdfs = sorted(SAMPLE.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"未找到样例 PDF：{SAMPLE}")
    return pdfs[0]


def main(base_url: str) -> int:
    pdf = find_sample_pdf()
    with httpx.Client(timeout=600.0) as client, pdf.open("rb") as f:
        resp = client.post(
            f"{base_url}/file_parse",
            files={"files": (pdf.name, f, "application/pdf")},
            data={"backend": "pipeline", "return_md": "true"},
        )
    resp.raise_for_status()
    content = resp.text
    # mineru-api 按 return_md 返回 JSON（含 md_content 字段）
    if resp.headers.get("content-type", "").startswith("application/json"):
        payload = resp.json()
        docs = payload if isinstance(payload, list) else payload.get("results", [payload])
        content = str(docs)
    assert len(content) > 20, f"解析结果过短：{len(content)} 字符"
    print(f"[smoke:mineru] OK — 解析 {pdf.name} 成功，输出 {len(content)} 字符")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.base_url))
    except Exception as e:
        print(f"[smoke:mineru] FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
