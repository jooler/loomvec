"""P0-INF-06 冒烟：MinerU 可解析样例 PDF（basic 档位，mineru-api 4.x V1 工作流）。

用法：uv run python scripts/smoke/smoke_mineru.py [--base-url http://localhost:38000]
样例为脚本内合成的单页文本 PDF（MinerU 源码已不入库，不再引用其 demo 文件）；
首次调用可能触发模型加载，耐心等待。解析经 loomvec 的 MineruClient
（V1：uploads → parse/jobs → files），与生产同一路径。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from loomvec.core.config import MineruSettings
from loomvec.core.mineru_client import MineruClient

BASE_URL = "http://localhost:38000"


def build_sample_pdf() -> bytes:
    """合成最小单页文本 PDF（Helvetica 内置字体，零依赖、离线可重复）。"""

    def text_line(y: int, text: str) -> bytes:
        return f"BT /F1 12 Tf 72 {y} Td ({text}) Tj ET".encode()

    content = b"\n".join(
        [
            text_line(720, "MinerU smoke parse sample."),
            text_line(696, "The quick brown fox jumps over the lazy dog."),
            text_line(672, "Document parsing should yield stable markdown lines."),
        ]
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref_pos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_pos,
    )
    return bytes(out)


async def run(base_url: str) -> None:
    pdf = build_sample_pdf()
    client = MineruClient(MineruSettings(base_url=base_url, timeout_seconds=600.0))
    result = await client.parse("smoke-sample.pdf", pdf, "application/pdf")
    assert len(result.markdown) > 20, f"解析结果过短：{len(result.markdown)} 字符"
    print(
        f"[smoke:mineru] OK — 解析 smoke-sample.pdf 成功，输出 {len(result.markdown)} 字符、"
        f"{len(result.content_list)} 版面块"
    )


def main(base_url: str) -> int:
    asyncio.run(run(base_url))
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
