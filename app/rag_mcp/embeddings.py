from typing import Protocol

from openai import AsyncOpenAI, OpenAI

from app.rag_mcp.config import EMBEDDING_DIMENSIONS


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class QueryEmbedder(Protocol):
    model: str

    async def embed_query(self, text: str) -> list[float]: ...


class AsyncOpenAIEmbedder:
    """검색 질의용. 색인과 같은 모델·차원을 써야 같은 벡터 공간에서 비교된다."""

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key)

    async def embed_query(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=self.model, input=[text], dimensions=EMBEDDING_DIMENSIONS
        )
        return response.data[0].embedding


class OpenAIEmbedder:
    """색인과 검색이 같은 모델·차원을 쓰도록 한 곳에서만 임베딩한다."""

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._client = OpenAI(api_key=api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.model, input=texts, dimensions=EMBEDDING_DIMENSIONS
        )
        vectors = [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding count does not match input count")
        return vectors
