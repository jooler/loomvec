"""解析器链路由（P1-WRK-02 接口抽象；03 文档 §一）。

不依赖真实 MinerU：用桩客户端验证 MIME → 解析器路由与产物结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loomvec.core.pipeline.parsers import (
    MineruParser,
    ParsedDocument,
    PlainTextParser,
    build_parser_chain,
    parse_document,
)


@dataclass
class _FakeMineruResult:
    markdown: str = "# 标题\n正文"
    content_list: list = field(default_factory=lambda: [{"page_idx": 0}, "junk"])


class _FakeMineruClient:
    async def parse(self, filename: str, data: bytes, mime: str) -> _FakeMineruResult:
        return _FakeMineruResult()


async def test_chain_routes_mineru_mime():
    doc = await parse_document(_FakeMineruClient(), "a.pdf", b"%PDF-1.4", "application/pdf")
    assert isinstance(doc, ParsedDocument)
    assert doc.parser_name == "mineru"
    # 字符串项被过滤，仅保留 dict 版面块
    assert doc.content_list == [{"page_idx": 0}]


async def test_chain_falls_back_to_plain_text():
    doc = await parse_document(_FakeMineruClient(), "a.txt", "你好".encode(), "text/plain")
    assert doc.parser_name == "plain_text"
    assert doc.markdown == "你好"


def test_chain_order_and_supports():
    chain = build_parser_chain(_FakeMineruClient())
    assert [p.name for p in chain] == ["mineru", "plain_text"]
    assert chain[0].supports("application/pdf")
    assert not chain[0].supports("text/plain")
    assert chain[1].supports("text/plain")  # 链尾全接受
    assert isinstance(chain[0], MineruParser) and isinstance(chain[1], PlainTextParser)
