"""rag 스키마의 정책 chunk 테이블. 업무 DB(platform/guard)와 분리해 RAG 쪽에서 직접 관리한다."""

from collections.abc import Sequence

import psycopg

from app.rag_mcp.config import EMBEDDING_DIMENSIONS
from app.rag_mcp.policy import PolicyChunk

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS rag;
CREATE TABLE IF NOT EXISTS rag.policy_chunks (
    policy_id       text        NOT NULL,
    version         text        NOT NULL,
    document_id     text        NOT NULL,
    section_id      text        NOT NULL,
    title           text        NOT NULL,
    text            text        NOT NULL,
    category        text,
    resource_uri    text        NOT NULL,
    embedding       vector({EMBEDDING_DIMENSIONS}) NOT NULL,
    embedding_model text        NOT NULL,
    indexed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version, policy_id)
);
"""


def to_vector_literal(vector: Sequence[float]) -> str:
    # pgvector 텍스트 형식. numpy 어댑터 없이 ::vector 캐스트로 넣는다.
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_SQL)


def replace_version(
    conn: psycopg.Connection,
    chunks: Sequence[PolicyChunk],
    vectors: Sequence[Sequence[float]],
    *,
    embedding_model: str,
) -> int:
    """같은 버전의 기존 행을 지우고 다시 넣는다. 한 트랜잭션이라 중간 실패 시 이전 색인이 남는다."""
    if len(chunks) != len(vectors):
        raise ValueError("chunks and vectors must have the same length")
    versions = {chunk.version for chunk in chunks}
    if len(versions) != 1:
        raise ValueError("all chunks must share one policy version")
    (version,) = versions

    with conn.transaction():
        conn.execute("DELETE FROM rag.policy_chunks WHERE version = %s", (version,))
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO rag.policy_chunks (
                    policy_id, version, document_id, section_id, title, text,
                    category, resource_uri, embedding, embedding_model
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                """,
                [
                    (
                        chunk.policy_id,
                        chunk.version,
                        chunk.document_id,
                        chunk.section_id,
                        chunk.title,
                        chunk.text,
                        chunk.category.value if chunk.category else None,
                        chunk.resource_uri,
                        to_vector_literal(vector),
                        embedding_model,
                    )
                    for chunk, vector in zip(chunks, vectors)
                ],
            )
    return len(chunks)


def count_version(conn: psycopg.Connection, version: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM rag.policy_chunks WHERE version = %s", (version,)
    ).fetchone()
    return int(row[0]) if row else 0
