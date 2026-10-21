"""정책 색인 명령: uv run --env-file .env python -m app.rag_mcp.index"""

import psycopg

from app.rag_mcp.config import IndexSettings
from app.rag_mcp.embeddings import Embedder, OpenAIEmbedder
from app.rag_mcp.policy import parse_policy
from app.rag_mcp import store


def build_index(settings: IndexSettings, embedder: Embedder) -> int:
    markdown = settings.policy_file.read_text(encoding="utf-8")
    chunks = parse_policy(markdown, version=settings.policy_version)
    vectors = embedder.embed([chunk.embedding_input for chunk in chunks])
    with psycopg.connect(settings.db_url) as conn:
        store.ensure_schema(conn)
        store.replace_version(
            conn, chunks, vectors, embedding_model=embedder.model
        )
        return store.count_version(conn, settings.policy_version)


def main() -> None:
    settings = IndexSettings.from_env()
    embedder = OpenAIEmbedder(settings.openai_api_key, settings.embedding_model)
    count = build_index(settings, embedder)
    print(
        f"indexed policy {settings.policy_version}: {count} sections "
        f"({settings.embedding_model})"
    )


if __name__ == "__main__":
    main()
