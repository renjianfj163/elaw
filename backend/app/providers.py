class EmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        raise NotImplementedError


class NullEmbedding(EmbeddingProvider):
    """第一版不下载向量模型。需要检索时再接 BGE-M3。"""

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class RerankProvider:
    def rerank(self, query: str, passages: list[str]) -> list[int]:
        raise NotImplementedError


class NullRerank(RerankProvider):
    def rerank(self, query: str, passages: list[str]) -> list[int]:
        return list(range(len(passages)))
