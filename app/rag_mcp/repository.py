"""rag.policy_chunks 조회. 도구와 Resource가 같은 행을 읽어 근거 원문이 어긋나지 않게 한다."""

from collections.abc import Sequence
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row

from app.rag_mcp.store import to_vector_literal
from app.schemas import Category

_SECTION_COLUMNS = "policy_id, title, text, version, category, resource_uri"


class PolicyRepository(Protocol):
    async def list_versions(self) -> dict[str, str]:
        """색인된 policyVersion → embedding_model."""
        ...

    async def list_policy_ids(self, version: str) -> list[str]: ...

    async def search(
        self,
        query_vector: Sequence[float],
        *,
        version: str,
        category: Category | None,
        top_k: int,
    ) -> list[dict[str, Any]]: ...

    async def get_section(
        self, policy_id: str, version: str
    ) -> dict[str, Any] | None: ...


class PgPolicyRepository:
    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    async def _fetch(
        self, sql: str, params: Sequence[Any] | dict[str, Any]
    ) -> list[dict[str, Any]]:
        # 호출 빈도가 낮은 조사 도구라 요청마다 연결한다. 풀은 부하가 생기면 도입한다.
        async with await psycopg.AsyncConnection.connect(
            self._db_url, row_factory=dict_row
        ) as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()

    async def list_versions(self) -> dict[str, str]:
        rows = await self._fetch(
            """
            SELECT version, min(embedding_model) AS embedding_model
            FROM rag.policy_chunks GROUP BY version ORDER BY version
            """,
            (),
        )
        return {row["version"]: row["embedding_model"] for row in rows}

    async def list_policy_ids(self, version: str) -> list[str]:
        rows = await self._fetch(
            "SELECT policy_id FROM rag.policy_chunks WHERE version = %s ORDER BY policy_id",
            (version,),
        )
        return [row["policy_id"] for row in rows]

    async def search(
        self,
        query_vector: Sequence[float],
        *,
        version: str,
        category: Category | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        vector = to_vector_literal(query_vector)
        # 유형을 지정하면 해당 유형 조항과 공통 조항(category NULL)만 후보로 둔다.
        return await self._fetch(
            f"""
            SELECT {_SECTION_COLUMNS},
                   1 - (embedding <=> %(vector)s::vector) AS score
            FROM rag.policy_chunks
            WHERE version = %(version)s
              AND (%(category)s::text IS NULL OR category = %(category)s OR category IS NULL)
            ORDER BY embedding <=> %(vector)s::vector
            LIMIT %(top_k)s
            """,
            {
                "vector": vector,
                "version": version,
                "category": category.value if category else None,
                "top_k": top_k,
            },
        )

    async def get_section(self, policy_id: str, version: str) -> dict[str, Any] | None:
        rows = await self._fetch(
            f"SELECT {_SECTION_COLUMNS} FROM rag.policy_chunks"
            " WHERE version = %s AND policy_id = %s",
            (version, policy_id),
        )
        return rows[0] if rows else None
