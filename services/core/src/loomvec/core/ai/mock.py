"""mock 供方实现：确定性本地 AI 通道（ai.mock=true 时生效，生产禁止）。

定位：无云凭据环境（本地开发 / CI）下跑通全链路的替代实现，行为确定性、
跨进程一致。真实的云端供方见 gateway.py 的 HTTP 路径；新增本地供方
（如本地 ONNX 嵌入）时可参照 MockBackend 的接口另实现。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from typing import Any

from loomvec.core.config import AiSettings


class MockBackend:
    """确定性 mock：嵌入=sha256 种子伪随机球面向量；rerank=字符 bigram Jaccard；
    complete=按 LOOMVEC 控制标记生成确定性 JSON。"""

    def __init__(self, settings: AiSettings) -> None:
        self._settings = settings

    # ---------- embedding ----------

    def embed(self, texts: list[str]) -> list[list[float]]:
        dim = self._settings.embedding.dim
        vectors: list[list[float]] = []
        for text in texts:
            seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
            vec: list[float] = []
            state = seed or 1
            for _ in range(dim):
                # xorshift 伪随机，稳定且跨进程一致
                state ^= state << 13
                state &= (1 << 64) - 1
                state ^= state >> 7
                state ^= state << 17
                state &= (1 << 64) - 1
                vec.append((state / (1 << 64)) * 2 - 1)
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return vectors

    # ---------- rerank ----------

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        def bigrams(s: str) -> set[str]:
            s = re.sub(r"\s+", "", s.lower())
            return {s[i : i + 2] for i in range(len(s) - 1)} or {s}

        q = bigrams(query)
        return [len(q & bigrams(d)) / (len(q | bigrams(d)) or 1) for d in documents]

    # ---------- LLM ----------

    def complete(self, prompt: str) -> str:
        task = self._parse_tag(prompt, "LOOMVEC_TASK")
        start = int(self._parse_tag(prompt, "DOC_START_LINE") or "1")
        end = int(self._parse_tag(prompt, "DOC_END_LINE") or "1")
        if task == "chunk_markers":
            window = self._settings.mock_impl.chunk_window_lines
            return json.dumps(
                {"chunk_markers": self._window_markers(start, end, window)}, ensure_ascii=False
            )
        if task == "chunk_graph":
            # P3-CORE-02 合并抽取：markers + 实体/关系确定性生成（从正文中扫候选实体）
            window = self._settings.mock_impl.chunk_window_lines
            entities, relations = self._mock_extraction(prompt, start, end)
            return json.dumps(
                {
                    "chunk_markers": self._window_markers(start, end, window),
                    "entities": entities,
                    "relations": relations,
                },
                ensure_ascii=False,
            )
        if task == "qa_answer":
            # P3-CORE-05 问答：回显来源片段关键词，保证 mock 模式答案引用可对上
            sources = self._parse_tag(prompt, "LOOMVEC_SOURCE_COUNT") or "0"
            return f"（mock 回答）根据 {sources} 条检索来源归纳：该问题可由已提供的资料支持。"
        if task == "community_summary":
            return json.dumps(
                {"summary": "（mock 社区摘要）该社区实体围绕共同主题聚簇，关系密度中等。"},
                ensure_ascii=False,
            )
        if task == "paper_meta":
            # 自动改名只在真实 LLM 下生效，mock 模式一律视为非论文
            return json.dumps({"is_paper": False})
        if task == "image_caption":
            # 回显文件名/提示词中的关键词，使 mock 模式下"以文搜图"可命中
            subject = self._parse_tag(prompt, "LOOMVEC_IMAGE_NAME") or "未命名图片"
            return json.dumps(
                {"caption": f"图片内容：{subject} 的 mock 描述（确定性占位文案）"},
                ensure_ascii=False,
            )
        return json.dumps({"content": "（mock 输出）"}, ensure_ascii=False)

    # ---------- mock 抽取的确定性实现 ----------

    @classmethod
    def _window_markers(cls, start: int, end: int, window: int) -> list[dict[str, Any]]:
        markers: list[dict[str, Any]] = []
        cur = start
        while cur <= end:
            stop = min(cur + window - 1, end)
            markers.append(
                {"start_line": cur, "end_line": stop, "title": f"段落 {cur}-{stop}", "keywords": []}
            )
            cur = stop + 1
        return markers

    _CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,12}")
    _EN_RUN_RE = re.compile(r"[A-Z][A-Za-z0-9_]{1,23}(?:\s+[A-Z][A-Za-z0-9_]{1,23})*")
    _ORG_HINT_RE = re.compile(
        r"(公司|集团|银行|大学|学院|研究院|部门|委员会|政府|法院|中心|"
        r"Inc\b|Corp\b|Ltd\b|LLC\b|GmbH\b|University|Institute)",
        re.IGNORECASE,
    )

    @classmethod
    def _mock_extraction(
        cls, prompt: str, start: int, end: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """从带行号正文扫候选实体：CJK 连串 / 英文大写词序列；关系=相邻实体链。"""
        candidates: dict[str, dict[str, Any]] = {}
        for n in range(start, end + 1):
            m = re.search(rf"^L{n}: (.*)$", prompt, re.MULTILINE)
            if not m:
                continue
            text = m.group(1)
            for raw in cls._CJK_RUN_RE.findall(text) + cls._EN_RUN_RE.findall(text):
                name = raw.strip()
                if len(name) < 2 or name in candidates:
                    continue
                candidates[name] = {
                    "name": name,
                    "type": "Organization"
                    if cls._ORG_HINT_RE.search(name)
                    else ("Person" if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name) else "Concept"),
                    "description": f"{name}（mock 抽取，首次出现于 L{n}）",
                    "lines": [n],
                }
                if len(candidates) >= 8:
                    break
            if len(candidates) >= 8:
                break
        entities = list(candidates.values())
        relations = [
            {
                "head": a["name"],
                "tail": b["name"],
                "type": "RELATED_TO",
                "evidence_lines": a["lines"],
            }
            for a, b in itertools.pairwise(entities)
        ]
        return entities, relations

    # ---------- clip（图文向量，P2-CORE-05）----------

    _TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """英文整词 + 中文单字：文本与图片描述共享同一 token 空间。"""
        return cls._TOKEN_RE.findall(text.lower())

    def clip_text(self, texts: list[str]) -> list[list[float]]:
        """词袋哈希向量：token 哈希落位累加后归一化（文本与图片同空间）。"""
        dim = self._settings.clip.dim
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * dim
            for tok in self._tokenize(text):
                h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:8], "big")
                vec[h % dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vectors.append([x / norm for x in vec])
        return vectors

    def clip_image(self, images: list[bytes], captions: list[str | None]) -> list[list[float]]:
        """图片向量：优先按 caption 文本落向量（mock 无视觉能力，保证可检索性）。"""
        texts = [
            cap or f"image-{hashlib.sha256(img).hexdigest()[:12]}"
            for img, cap in zip(images, captions, strict=False)
        ]
        return self.clip_text(texts)

    @staticmethod
    def _parse_tag(text: str, tag: str) -> str | None:
        m = re.search(rf"\[{tag}=([^\]]+)\]", text)
        return m.group(1).strip() if m else None
