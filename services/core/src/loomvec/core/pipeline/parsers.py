"""解析器接口与注册表（03 文档 §一：解析器接口抽象，可整体切换 Docling）。

- `Parser` Protocol：`supports(mime)` + `async parse()` → `ParsedDocument`；
- 内置链：MineruParser（MinerU HTTP，主力）→ PlainTextParser（UTF-8 直读，链尾兜底，
  保证"任何资产至少可被文件名/元数据检索"）；
- 新增解析器（Docling/PaddleOCR 等）实现 Protocol 后在 `build_parser_chain` 登记
  即可，管线步骤零改动；License 风险切换（MinerU → Docling）因此是单文件增量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from loomvec.core.mineru_client import MineruClient
from loomvec.core.pipeline.mime import is_mineru_mime


@dataclass
class ParsedDocument:
    """解析产物：Markdown（行号稳定）+ 版面块（页码/坐标）+ 解析器名。"""

    markdown: str
    content_list: list[dict] = field(default_factory=list)
    parser_name: str = "plain_text"


class Parser(Protocol):
    """文档解析器契约：按 MIME 声明能力，产出统一结构。"""

    name: str

    def supports(self, mime: str) -> bool: ...

    async def parse(self, filename: str, data: bytes, mime: str) -> ParsedDocument: ...


class MineruParser:
    """MinerU HTTP 解析（PDF/Office/图片型文档主力，含内置 OCR）。"""

    name = "mineru"

    def __init__(self, client: MineruClient) -> None:
        self._client = client

    def supports(self, mime: str) -> bool:
        return is_mineru_mime(mime)

    async def parse(self, filename: str, data: bytes, mime: str) -> ParsedDocument:
        result = await self._client.parse(filename, data, mime)
        # content_list 兼容处理：可能混入字符串项，版面块只取 dict
        return ParsedDocument(
            markdown=result.markdown,
            content_list=[b for b in result.content_list if isinstance(b, dict)],
            parser_name=self.name,
        )


class PlainTextParser:
    """兜底解析：任何字节流按 UTF-8 直读为 Markdown（无版面坐标）。"""

    name = "plain_text"

    def supports(self, mime: str) -> bool:
        return True  # 链尾全接受

    async def parse(self, filename: str, data: bytes, mime: str) -> ParsedDocument:
        return ParsedDocument(markdown=data.decode("utf-8", errors="replace"))


def build_parser_chain(client: MineruClient) -> list[Parser]:
    """解析器优先级链：新解析器在此登记（靠前优先，链尾必须全接受）。"""
    return [MineruParser(client), PlainTextParser()]


async def parse_document(
    client: MineruClient, filename: str, data: bytes, mime: str
) -> ParsedDocument:
    """按 MIME 解析到第一个支持的解析器（管线步骤的唯一入口）。"""
    for parser in build_parser_chain(client):
        if parser.supports(mime):
            return await parser.parse(filename, data, mime)
    return await PlainTextParser().parse(filename, data, mime)
