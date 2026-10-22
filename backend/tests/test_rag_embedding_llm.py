"""Embedding 与 LLM 客户端测试：真实 HTTP 语义（MockTransport），不伪造成功结果。

覆盖：
- 向量维度校验、L2 归一化、批量数量一致；
- 5xx / 429 重试，4xx 立即失败，超时与不可达的错误分类；
- LLM 超时 / 限流 / 不可达 / 空响应；
- 重试次数与错误上下文可观测。
"""

from __future__ import annotations

import httpx
import pytest

from app.rag import http as rag_http
from app.rag.embedding.ollama import OllamaEmbeddingProvider
from app.rag.errors import (
    EmbeddingDimensionMismatch,
    EmbeddingError,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from app.rag.llm.base import ChatMessage
from app.rag.llm.ollama import OllamaChatModel


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """重试等待改为 0，避免测试变慢（不改变重试逻辑本身）。"""
    monkeypatch.setattr(rag_http, "_sleep", lambda *args, **kwargs: None)


def _embedding_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def test_embedding_success_normalizes_and_reports(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        assert payload["model"] == "bge-m3"
        vectors = [[3.0, 4.0] + [0.0] * 1022 for _ in payload["input"]]
        return httpx.Response(200, json={"embeddings": vectors, "prompt_eval_count": 7})

    provider = OllamaEmbeddingProvider(
        base_url="http://fake",
        model="bge-m3",
        dim=1024,
        version="test-v1",
        batch_size=2,
        client=_embedding_client(handler),
    )
    batch = provider.embed_documents(["a", "b", "c"])
    assert batch.texts == 3
    assert len(batch.vectors) == 3
    assert batch.dim == 1024
    assert abs(sum(v * v for v in batch.vectors[0]) - 1.0) < 1e-9
    assert batch.prompt_tokens == 14  # 2 个批次 × 每批 7


def test_embedding_dimension_mismatch_is_explicit():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.1] * 768]})

    provider = OllamaEmbeddingProvider(
        base_url="http://fake", model="bge-m3", dim=1024, version="test-v1",
        client=_embedding_client(handler),
    )
    with pytest.raises(EmbeddingDimensionMismatch) as excinfo:
        provider.embed_query("dimension check")
    assert excinfo.value.details["actual_dim"] == 768
    assert excinfo.value.details["configured_dim"] == 1024


def test_embedding_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * 1023]})

    provider = OllamaEmbeddingProvider(
        base_url="http://fake", model="bge-m3", dim=1024, version="test-v1",
        max_retries=3, client=_embedding_client(handler),
    )
    batch = provider.embed_query("retry")
    assert calls["n"] == 3
    assert batch.attempts == 3


def test_embedding_rate_limit_honours_retry_after():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"embeddings": [[0.5] + [0.0] * 1023]})

    provider = OllamaEmbeddingProvider(
        base_url="http://fake", model="bge-m3", dim=1024, version="test-v1",
        max_retries=2, client=_embedding_client(handler),
    )
    assert provider.embed_query("rate limited").texts == 1
    assert calls["n"] == 2


def test_embedding_4xx_fails_fast_without_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="model not found")

    provider = OllamaEmbeddingProvider(
        base_url="http://fake", model="missing", dim=1024, version="test-v1",
        max_retries=5, client=_embedding_client(handler),
    )
    with pytest.raises(EmbeddingError) as excinfo:
        provider.embed_query("bad request")
    assert calls["n"] == 1
    assert excinfo.value.details["status_code"] == 400


def test_embedding_timeout_is_reported():
    def handler(request):
        raise httpx.TimeoutException("timed out")

    provider = OllamaEmbeddingProvider(
        base_url="http://fake", model="bge-m3", dim=1024, version="test-v1",
        max_retries=1, timeout_s=0.01, client=_embedding_client(handler),
    )
    with pytest.raises(EmbeddingError) as excinfo:
        provider.embed_query("slow")
    assert "超时" in excinfo.value.message
    assert excinfo.value.details["attempts"] == 2


def test_embedding_response_count_mismatch_is_rejected():
    def handler(request):
        return httpx.Response(200, json={"embeddings": [[0.1] * 1024]})

    provider = OllamaEmbeddingProvider(
        base_url="http://fake", model="bge-m3", dim=1024, version="test-v1",
        client=_embedding_client(handler),
    )
    with pytest.raises(EmbeddingError):
        provider.embed_documents(["one", "two"])


def _chat_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def _model(handler, **kwargs) -> OllamaChatModel:
    return OllamaChatModel(
        base_url="http://fake", model="qwen-test", max_retries=kwargs.pop("max_retries", 1),
        client=_chat_client(handler), **kwargs,
    )


def test_chat_success_returns_text_and_tokens():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "答案 [C1]"},
                "prompt_eval_count": 12,
                "eval_count": 5,
                "done_reason": "stop",
            },
        )

    response = _model(handler).chat([ChatMessage(role="user", content="q")], json_mode=True)
    assert response.text == "答案 [C1]"
    assert response.prompt_tokens == 12 and response.completion_tokens == 5


def test_chat_timeout_maps_to_llm_timeout():
    def handler(request):
        raise httpx.TimeoutException("timed out")

    with pytest.raises(LLMTimeout):
        _model(handler, timeout_s=0.01).chat([ChatMessage(role="user", content="q")])


def test_chat_rate_limit_maps_to_llm_rate_limited():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"}, text="limit")

    with pytest.raises(LLMRateLimited):
        _model(handler, max_retries=1).chat([ChatMessage(role="user", content="q")])


def test_chat_unreachable_maps_to_llm_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(LLMUnavailable):
        _model(handler).chat([ChatMessage(role="user", content="q")])


def test_chat_empty_content_is_error():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "   "}})

    with pytest.raises(LLMError):
        _model(handler).chat([ChatMessage(role="user", content="q")])
