import json
import os
import re
import unittest
import uuid
from dataclasses import replace
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server import MCPServer

from app.rag_mcp import store
from app.rag_mcp.config import DEFAULT_POLICY_VERSION, EMBEDDING_DIMENSIONS, POLICY_DIR, ServerSettings
from app.rag_mcp.main import create_app
from app.rag_mcp.policy import DOCUMENT_ID, PolicyChunk, parse_policy
from app.rag_mcp.repository import PgPolicyRepository
from app.rag_mcp.tools import register_tools, render_resource
from app.schemas import Category

TRACE_ID = "00000000-0000-4000-8000-000000000002"
VERSION = DEFAULT_POLICY_VERSION
CHUNKS = parse_policy(
    (POLICY_DIR / f"policy-v{VERSION}.md").read_text(encoding="utf-8"), version=VERSION
)


def _row(chunk: PolicyChunk) -> dict[str, Any]:
    return {
        "policy_id": chunk.policy_id,
        "title": chunk.title,
        "text": chunk.text,
        "version": chunk.version,
        "category": chunk.category.value if chunk.category else None,
        "resource_uri": chunk.resource_uri,
    }


class FakeEmbedder:
    model = "text-embedding-3-small"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.0] * EMBEDDING_DIMENSIONS


def _indexed_versions() -> dict[str, dict[str, dict[str, Any]]]:
    # 실제 DB처럼 레포의 모든 정책 버전(v3.0, v3.1 …)을 색인된 상태로 둔다.
    versions = {}
    for path in sorted(POLICY_DIR.glob("policy-v*.md")):
        version = path.stem.removeprefix("policy-v")
        chunks = parse_policy(path.read_text(encoding="utf-8"), version=version)
        versions[version] = {chunk.policy_id: _row(chunk) for chunk in chunks}
    return versions


class FakeRepository:
    """색인된 정책 버전을 메모리에 두고, preferred_id 조항을 1위로 돌려준다."""

    def __init__(self, embedding_model: str = "text-embedding-3-small") -> None:
        self.embedding_model = embedding_model
        self.by_version = _indexed_versions()
        self.rows = self.by_version[VERSION]
        self.last_search: dict[str, Any] = {}
        self.preferred_id = "CP-2.1"

    async def list_versions(self) -> dict[str, str]:
        return {version: self.embedding_model for version in self.by_version}

    async def list_policy_ids(self, version: str) -> list[str]:
        return sorted(self.by_version.get(version, {}))

    async def search(self, query_vector, *, version, category, top_k):
        self.last_search = {"version": version, "category": category, "top_k": top_k}
        candidates = [
            row
            for row in self.by_version.get(version, {}).values()
            if category is None or row["category"] in (category.value, None)
        ]
        candidates.sort(key=lambda row: row["policy_id"] != self.preferred_id)
        return [{**row, "score": 0.9 - i * 0.1} for i, row in enumerate(candidates[:top_k])]

    async def get_section(self, policy_id: str, version: str):
        return self.by_version.get(version, {}).get(policy_id)


class BrokenRepository(FakeRepository):
    async def list_versions(self) -> dict[str, str]:
        raise ConnectionError("database is down")


def _error(result) -> dict[str, Any]:
    # Agent 클라이언트(app/clients/business_mcp.py)와 같은 방식으로 SDK 접두어를 떼고 읽는다.
    text = re.sub(r"^Error executing tool [a-z_]+: ", "", result.content[0].text, count=1)
    return json.loads(text)


def _server(repository=None, embedder=None) -> MCPServer:
    server = MCPServer("test")
    repository = repository or FakeRepository()
    embedder = embedder or FakeEmbedder()
    register_tools(server, lambda: repository, lambda: embedder)
    return server


class RagMcpToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_read_only_tools_with_contract_schema(self) -> None:
        async with Client(_server()) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            templates = await client.list_resource_templates()

        self.assertEqual(set(tools), {"search_policy", "get_policy_section"})
        search = tools["search_policy"].input_schema
        self.assertEqual(set(search["required"]), {"query", "policyVersion"})
        self.assertEqual(search["properties"]["topK"]["maximum"], 4)
        self.assertIn("욕설", tools["search_policy"].description)
        for tool in tools.values():
            self.assertTrue(tool.annotations.read_only_hint)
        self.assertEqual(
            [t.uri_template for t in templates.resource_templates],
            ["policy://{version}/{document_id}/{section_id}"],
        )

    async def test_search_returns_matches_with_trace_envelope(self) -> None:
        embedder = FakeEmbedder()
        async with Client(_server(embedder=embedder)) as client:
            result = await client.call_tool(
                "search_policy",
                {"query": "욕설 키워드 처리 기준", "policyVersion": VERSION, "topK": 2, "traceId": TRACE_ID},
            )

        self.assertFalse(result.is_error)
        data = result.structured_content["data"]
        self.assertEqual(result.structured_content["traceId"], TRACE_ID)
        self.assertEqual(set(data), {"matches"})
        self.assertEqual(len(data["matches"]), 2)
        self.assertEqual({m["version"] for m in data["matches"]}, {VERSION})
        top = data["matches"][0]
        self.assertEqual(top["policyId"], "CP-2.1")
        self.assertEqual(top["category"], "PROFANITY")
        self.assertEqual(top["resourceUri"], f"policy://{VERSION}/{DOCUMENT_ID}/CP-2.1")
        self.assertEqual(top["score"], 0.9)
        self.assertEqual(embedder.queries, ["욕설 키워드 처리 기준"])

    async def test_category_filter_keeps_type_and_common_sections(self) -> None:
        repository = FakeRepository()
        repository.preferred_id = "CP-4.1"
        async with Client(_server(repository=repository)) as client:
            result = await client.call_tool(
                "search_policy",
                {"query": "같은 문장 반복", "policyVersion": VERSION, "category": "FLOOD"},
            )

        self.assertEqual(repository.last_search["category"], Category.FLOOD)
        categories = {m["category"] for m in result.structured_content["data"]["matches"]}
        self.assertLessEqual(categories, {"FLOOD", None})
        self.assertEqual(result.structured_content["data"]["matches"][0]["policyId"], "CP-4.1")

    async def test_rejects_invalid_input(self) -> None:
        async with Client(_server()) as client:
            too_many = await client.call_tool(
                "search_policy", {"query": "q", "policyVersion": VERSION, "topK": 5}
            )
            bad_category = await client.call_tool(
                "search_policy", {"query": "q", "policyVersion": VERSION, "category": "SPAM"}
            )
            bad_id = await client.call_tool(
                "get_policy_section", {"policyId": "POL-01", "policyVersion": VERSION}
            )
        self.assertTrue(too_many.is_error)
        self.assertTrue(bad_category.is_error)
        self.assertTrue(bad_id.is_error)

    async def test_unknown_version_lists_available_versions(self) -> None:
        async with Client(_server()) as client:
            result = await client.call_tool(
                "search_policy", {"query": "q", "policyVersion": "9.9", "traceId": TRACE_ID}
            )
        self.assertTrue(result.is_error)
        error = _error(result)
        self.assertEqual(error["error"]["code"], "POLICY_VERSION_NOT_FOUND")
        self.assertFalse(error["error"]["retryable"])
        self.assertIn(VERSION, error["error"]["message"])
        self.assertEqual(error["traceId"], TRACE_ID)

    async def test_embedding_model_mismatch_blocks_search(self) -> None:
        repository = FakeRepository(embedding_model="other-model")
        async with Client(_server(repository=repository)) as client:
            result = await client.call_tool("search_policy", {"query": "q", "policyVersion": VERSION})
        self.assertEqual(_error(result)["error"]["code"], "POLICY_INDEX_MISMATCH")

    async def test_storage_failure_is_retryable(self) -> None:
        async with Client(_server(repository=BrokenRepository())) as client:
            result = await client.call_tool("search_policy", {"query": "q", "policyVersion": VERSION})
        error = _error(result)
        self.assertEqual(error["error"]["code"], "TEMPORARY_UNAVAILABLE")
        self.assertTrue(error["error"]["retryable"])

    async def test_get_section_and_resource_share_the_same_text(self) -> None:
        async with Client(_server()) as client:
            result = await client.call_tool(
                "get_policy_section", {"policyId": "MP-4.1", "policyVersion": VERSION}
            )
            section = result.structured_content["data"]
            resource = await client.read_resource(section["resourceUri"])

        self.assertEqual(section["title"], "강퇴 심사")
        self.assertIn("24시간", section["text"])
        self.assertEqual(len(resource.contents), 1)
        self.assertEqual(str(resource.contents[0].uri), section["resourceUri"])
        self.assertEqual(resource.contents[0].mime_type, "text/markdown")
        # Agent 소비자는 Resource 본문과 조항 text가 정확히 같아야 근거로 받아들인다.
        self.assertEqual(resource.contents[0].text, section["text"])
        self.assertEqual(
            render_resource(_row(next(c for c in CHUNKS if c.policy_id == "MP-4.1"))),
            section["text"],
        )

    async def test_missing_section_lists_existing_ids(self) -> None:
        async with Client(_server()) as client:
            result = await client.call_tool(
                "get_policy_section", {"policyId": "CP-9.9", "policyVersion": VERSION}
            )
            with self.assertRaises(Exception):
                await client.read_resource(f"policy://{VERSION}/{DOCUMENT_ID}/CP-9.9")
            with self.assertRaises(Exception):
                await client.read_resource(f"policy://{VERSION}/other-doc/CP-2.1")
        error = _error(result)
        self.assertEqual(error["error"]["code"], "POLICY_SECTION_NOT_FOUND")
        self.assertIn("MP-4.1", error["error"]["message"])


class RagMcpHttpTests(unittest.TestCase):
    def settings(self, token: str = "agent-token") -> ServerSettings:
        return ServerSettings(
            db_url="postgresql://unused",
            openai_api_key="unused",
            embedding_model="text-embedding-3-small",
            agent_token=token,
            host="127.0.0.1",
            port=8003,
        )

    def test_health_reports_indexed_versions_and_mcp_requires_token(self) -> None:
        app = create_app(self.settings(), repository=FakeRepository(), embedder=FakeEmbedder())
        with TestClient(app) as client:
            health = client.get("/health").json()
            unauthenticated = client.post("/mcp", json={})
            wrong = client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})

        self.assertEqual(
            health,
            {"status": "ready", "readyForTasks": True, "indexedPolicyVersions": ["3.0", VERSION]},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

    def test_unconfigured_token_rejects_mcp(self) -> None:
        app = create_app(self.settings(token=""), repository=FakeRepository(), embedder=FakeEmbedder())
        with TestClient(app) as client:
            self.assertFalse(client.get("/health").json()["readyForTasks"])
            self.assertEqual(client.post("/mcp", json={}).status_code, 503)


@unittest.skipUnless(
    os.getenv("RAG_TEST_DB_URL"), "RAG_TEST_DB_URL이 없으면 pgvector 통합 테스트를 건너뛴다"
)
class PgPolicyRepositoryTests(unittest.IsolatedAsyncioTestCase):
    """임시 버전에 조항마다 다른 벡터를 넣고 실제 SQL의 정렬·필터를 확인한다."""

    async def asyncSetUp(self) -> None:
        self.db_url = os.environ["RAG_TEST_DB_URL"]
        self.version = f"test-{uuid.uuid4().hex[:8]}"
        self.chunks = [replace(chunk, version=self.version) for chunk in CHUNKS]
        self.vectors = [self.one_hot(i) for i in range(len(self.chunks))]
        with psycopg.connect(self.db_url) as conn:
            store.ensure_schema(conn)
            store.replace_version(conn, self.chunks, self.vectors, embedding_model="fake")
        self.repository = PgPolicyRepository(self.db_url)

    async def asyncTearDown(self) -> None:
        with psycopg.connect(self.db_url) as conn:
            conn.execute("DELETE FROM rag.policy_chunks WHERE version = %s", (self.version,))

    @staticmethod
    def one_hot(index: int) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        vector[index] = 1.0
        return vector

    def vector_of(self, policy_id: str) -> list[float]:
        index = next(i for i, c in enumerate(self.chunks) if c.policy_id == policy_id)
        return self.vectors[index]

    async def test_orders_by_similarity_and_reports_versions(self) -> None:
        rows = await self.repository.search(
            self.vector_of("MP-4.1"), version=self.version, category=None, top_k=3
        )
        self.assertEqual(rows[0]["policy_id"], "MP-4.1")
        self.assertAlmostEqual(rows[0]["score"], 1.0, places=5)
        self.assertEqual(len(rows), 3)
        self.assertEqual((await self.repository.list_versions())[self.version], "fake")

    async def test_category_filter_excludes_other_types(self) -> None:
        # 도배 조항 벡터로 질의해도 PROFANITY 필터면 도배 조항은 후보에서 빠진다.
        rows = await self.repository.search(
            self.vector_of("CP-4.1"), version=self.version, category=Category.PROFANITY, top_k=4
        )
        self.assertNotIn("CP-4.1", [row["policy_id"] for row in rows])
        self.assertLessEqual({row["category"] for row in rows}, {"PROFANITY", None})

    async def test_get_section_by_id(self) -> None:
        row = await self.repository.get_section("SP-3.1", self.version)
        self.assertEqual(row["category"], "PII_EXPOSURE")
        self.assertIsNone(await self.repository.get_section("CP-9.9", self.version))


if __name__ == "__main__":
    unittest.main()
