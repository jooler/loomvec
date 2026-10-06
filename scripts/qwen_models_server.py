"""Qwen3 embedding/reranking adapter with OpenAI-compatible endpoints.

The Qwen3 reranker is a yes/no causal-language-model scorer rather than an
Infinity-compatible cross-encoder.  This small adapter keeps the existing
LoomVec gateway contract while applying Qwen's chat template and retrieval
instruction locally.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import threading
import time
from typing import Any

import torch
import torch.nn.functional as F
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer
from transformers import AutoModelForCausalLM, AutoTokenizer

EMBED_MODEL = "qwen3-embedding-0.6b"
RERANK_MODEL = "qwen3-reranker-0.6b"
DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
# 同卡还有 MinerU 等进程：缓存的空闲显存超过该值才归还驱动，避免长期停在峰值。
IDLE_CACHE_RELEASE_BYTES = 1 << 30


def _release_cuda_cache(*, after_oom: bool) -> None:
    if after_oom:
        gc.collect()
    elif torch.cuda.memory_reserved() - torch.cuda.memory_allocated() <= IDLE_CACHE_RELEASE_BYTES:
        return
    torch.cuda.empty_cache()


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    input_type: str = "document"


class RerankRequest(BaseModel):
    model: str
    query: str
    documents: list[str]
    top_n: int | None = Field(default=None, ge=1)


class QwenService:
    def __init__(self, embedding_path: str | None, reranker_path: str | None) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("Qwen 本地服务需要 CUDA；未检测到可用 GPU")
        if embedding_path is None and reranker_path is None:
            raise RuntimeError("至少需要启用 embedding 或 rerank 模型")
        self.device = torch.device("cuda")
        self.embedding = (
            SentenceTransformer(
                embedding_path,
                device=str(self.device),
                model_kwargs={"torch_dtype": torch.float16},
            )
            if embedding_path
            else None
        )
        self.rerank_tokenizer = (
            AutoTokenizer.from_pretrained(reranker_path, padding_side="left")
            if reranker_path
            else None
        )
        self.reranker = (
            AutoModelForCausalLM.from_pretrained(reranker_path, dtype=torch.float16)
            .to(self.device)
            .eval()
            if reranker_path
            else None
        )
        self.false_id = (
            self.rerank_tokenizer.convert_tokens_to_ids("no") if self.rerank_tokenizer else None
        )
        self.true_id = (
            self.rerank_tokenizer.convert_tokens_to_ids("yes") if self.rerank_tokenizer else None
        )
        self.max_length = 8192
        prefix = (
            "<|im_start|>system\nJudge whether the Document meets the requirements "
            "based on the Query and the Instruct provided. Note that the answer can "
            'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
        )
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self.prefix_tokens = (
            self.rerank_tokenizer.encode(prefix, add_special_tokens=False)
            if self.rerank_tokenizer
            else []
        )
        self.suffix_tokens = (
            self.rerank_tokenizer.encode(suffix, add_special_tokens=False)
            if self.rerank_tokenizer
            else []
        )
        self.instruction = DEFAULT_INSTRUCTION
        self.lock = threading.Lock()

    @staticmethod
    def _texts(value: str | list[str]) -> list[str]:
        texts = [value] if isinstance(value, str) else value
        if not texts or not all(isinstance(text, str) for text in texts):
            raise HTTPException(status_code=400, detail="input 必须是字符串或字符串数组")
        return texts

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if self.embedding is None:
            raise HTTPException(status_code=503, detail="embedding 模型未启用")
        kwargs: dict[str, Any] = {"normalize_embeddings": True}
        if input_type == "query":
            kwargs["prompt_name"] = "query"
        vectors: list[list[float]] | None = None
        with self.lock:
            with contextlib.suppress(torch.OutOfMemoryError), torch.inference_mode():
                vectors = self.embedding.encode(texts, **kwargs).tolist()
            # 必须等异常被吞掉之后再回收：异常存活期间其 traceback 仍引用着 forward
            # 各层帧，其中的激活张量要等异常对象释放后才能真正归还。
            _release_cuda_cache(after_oom=vectors is None)
        if vectors is None:
            raise HTTPException(status_code=503, detail="embedding 显存不足，请减小批次后重试")
        return vectors

    def _rerank_scores(self, pairs: list[str]) -> list[float]:
        with torch.inference_mode():
            inputs = self.rerank_tokenizer(
                pairs,
                padding=False,
                truncation="longest_first",
                return_attention_mask=False,
                max_length=self.max_length - len(self.prefix_tokens) - len(self.suffix_tokens),
            )
            for index, token_ids in enumerate(inputs["input_ids"]):
                inputs["input_ids"][index] = self.prefix_tokens + token_ids + self.suffix_tokens
            padded = self.rerank_tokenizer.pad(inputs, padding=True, return_tensors="pt").to(
                self.device
            )
            # 只需最后一个位置的 yes/no logits：全序列 [batch, seq, vocab] 的 logits 在长文档时
            # 一次要近 10GiB，单次前向打分也用不到 KV cache。
            logits = self.reranker(**padded, use_cache=False, logits_to_keep=1).logits[:, -1, :]
            yes_no = torch.stack([logits[:, self.false_id], logits[:, self.true_id]], dim=1)
            return F.log_softmax(yes_no, dim=1)[:, 1].exp().tolist()

    def rerank(self, query: str, documents: list[str], top_n: int | None) -> list[dict[str, Any]]:
        if self.reranker is None or self.rerank_tokenizer is None:
            raise HTTPException(status_code=503, detail="rerank 模型未启用")
        pairs = [
            f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: {document}"
            for document in documents
        ]
        scores: list[float] | None = None
        with self.lock:
            with contextlib.suppress(torch.OutOfMemoryError):
                scores = self._rerank_scores(pairs)
            # 同 embed：异常释放后再回收，异常帧里的激活张量才能归还。
            _release_cuda_cache(after_oom=scores is None)
        if scores is None:
            raise HTTPException(
                status_code=503, detail="rerank 显存不足，请减少候选文档数量或长度后重试"
            )
        results = [
            {"index": index, "relevance_score": score, "document": documents[index]}
            for index, score in enumerate(scores)
        ]
        results.sort(key=lambda item: item["relevance_score"], reverse=True)
        return results[:top_n] if top_n is not None else results


def create_app(service: QwenService) -> FastAPI:
    app = FastAPI(title="LoomVec Qwen3 local models")

    @app.get("/health")
    def health() -> dict[str, float]:
        return {"unix": time.time()}

    @app.get("/models")
    def models() -> dict[str, Any]:
        data = []
        if service.embedding is not None:
            data.append({"id": EMBED_MODEL, "object": "model", "owned_by": "local"})
        if service.reranker is not None:
            data.append({"id": RERANK_MODEL, "object": "model", "owned_by": "local"})
        return {
            "object": "list",
            "data": data,
        }

    @app.post("/embeddings")
    def embeddings(request: EmbeddingRequest) -> dict[str, Any]:
        if request.model != EMBED_MODEL:
            raise HTTPException(status_code=400, detail=f"不支持的 embedding 模型：{request.model}")
        texts = service._texts(request.input)
        vectors = service.embed(texts, request.input_type)
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "embedding": vector, "index": index}
                for index, vector in enumerate(vectors)
            ],
            "model": request.model,
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    @app.post("/rerank")
    def rerank(request: RerankRequest) -> dict[str, Any]:
        if request.model != RERANK_MODEL:
            raise HTTPException(status_code=400, detail=f"不支持的 rerank 模型：{request.model}")
        results = service.rerank(request.query, request.documents, request.top_n)
        return {"object": "rerank", "results": results, "model": request.model}

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedding-model")
    parser.add_argument("--reranker-model")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=38011)
    args = parser.parse_args()
    app = create_app(QwenService(args.embedding_model, args.reranker_model))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
