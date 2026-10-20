"""#12 consumer → real #13 MCP app/tools/Resources.

The model-free contract tests use controlled storage/embeddings. The optional DB
test uses actual pgvector SQL plus deterministic embeddings, never OpenAI keys.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
import os
from unittest.mock import patch
import unittest
from uuid import uuid4

import httpx2
import psycopg

from app.clients.business_mcp import AgentToolError
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.policy_evidence import retrieve_policy_evidence
from app.rag_mcp.config import EMBEDDING_DIMENSIONS, POLICY_DIR, ServerSettings
from app.rag_mcp.main import create_app
from app.rag_mcp.policy import parse_policy
from app.rag_mcp.repository import PgPolicyRepository
from app.rag_mcp import store
from test_rag_mcp import FakeEmbedder, FakeRepository, BrokenRepository
from test_rag_read import tcp_fixture


TRACE = "00000000-0000-4000-8000-000000000013"
TOKEN = "consumer-provider-contract-test-token"


@asynccontextmanager
async def provided_server(repository, embedder=None):
    """Use production create_app/register_tools, not a hand-written MCP substitute."""
    app = create_app(
        ServerSettings("postgresql://unused", "unused-test-key", "text-embedding-3-small",
                       TOKEN, "127.0.0.1", 8003),
        repository=repository, embedder=embedder or FakeEmbedder(),
    )
    if os.getenv("LIVEGUARD_TEST_TCP") == "1":
        with tcp_fixture(app) as url:
            yield RagMcpReadClient(RagMcpSettings(url, TOKEN))
        return

    def factory(*, headers):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)
    async with app.router.lifespan_context(app):
        with patch("app.clients.rag_mcp.create_mcp_http_client", side_effect=factory):
            yield RagMcpReadClient(RagMcpSettings("http://127.0.0.1:8003/mcp", TOKEN))


class ProducerConsumerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_server_and_consumer_agree_on_all_risk_types(self):
        for category, policy_id in [
            ("PROFANITY", "CP-2.1"), ("HARASSMENT", "CP-3.1"),
            ("FLOOD", "CP-4.1"), ("PHISHING", "CP-5.1"),
            ("PII_EXPOSURE", "SP-3.1"),
        ]:
            repository = FakeRepository()
            repository.preferred_id = policy_id
            async with provided_server(repository) as client:
                result = await retrieve_policy_evidence(
                    {"policy_version": "3.0", "trace_id": TRACE},
                    query="유형별 정책 계약 검사", category=category, tools=client,
                )
            with self.subTest(category=category):
                evidence = result["policy_evidence"]
                self.assertEqual(evidence[0]["policyId"], policy_id)
                self.assertEqual(evidence[0]["category"], category)
                self.assertTrue(all(row["version"] == "3.0" for row in evidence))
                self.assertTrue(all(row["resourceUri"].startswith("policy://3.0/operating-policy/")
                                    for row in evidence))

    async def test_common_exception_is_preserved_as_null_category(self):
        repository = FakeRepository()
        repository.preferred_id = "CP-2.2"
        async with provided_server(repository) as client:
            result = await retrieve_policy_evidence(
                {"policy_version": "3.0", "trace_id": TRACE},
                query="인용과 제지의 정상 예외", category="PROFANITY", tools=client,
            )
        self.assertEqual(result["policy_evidence"][0]["policyId"], "CP-2.2")
        self.assertIsNone(result["policy_evidence"][0]["category"])

    async def test_actual_provider_errors_remain_explicit_without_fallback(self):
        async with provided_server(FakeRepository()) as client:
            with self.assertRaises(AgentToolError) as missing_version:
                await client.call("search_policy", {"query": "test", "policyVersion": "9.9", "traceId": TRACE})
            self.assertEqual(missing_version.exception.code, "POLICY_VERSION_NOT_FOUND")
            with self.assertRaises(AgentToolError) as missing_section:
                await client.call("get_policy_section", {"policyId": "CP-9.9", "policyVersion": "3.0", "traceId": TRACE})
            self.assertEqual(missing_section.exception.code, "POLICY_SECTION_NOT_FOUND")
        async with provided_server(FakeRepository(embedding_model="other-model")) as client:
            with self.assertRaises(AgentToolError) as mismatch:
                await client.call("search_policy", {"query": "test", "policyVersion": "3.0", "traceId": TRACE})
            self.assertEqual(mismatch.exception.code, "POLICY_INDEX_MISMATCH")
            self.assertFalse(mismatch.exception.retryable)

    async def test_actual_provider_storage_failure_is_retryable(self):
        async with provided_server(BrokenRepository()) as client:
            with self.assertRaises(AgentToolError) as raised:
                await client.call("search_policy", {"query": "test", "policyVersion": "3.0", "traceId": TRACE})
        self.assertEqual(raised.exception.code, "TEMPORARY_UNAVAILABLE")
        self.assertTrue(raised.exception.retryable)


@unittest.skipUnless(os.getenv("RAG_TEST_DB_URL"), "RAG_TEST_DB_URL required for real pgvector consumer integration")
class PgvectorConsumerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sql_search_and_original_resource_reach_consumer_state(self):
        db_url = os.environ["RAG_TEST_DB_URL"]
        version = "consumer-test-" + uuid4().hex[:8]
        chunks = [replace(c, version=version) for c in parse_policy(
            (POLICY_DIR / "policy-v3.0.md").read_text(), version=version,
        )]
        target_index = next(i for i,c in enumerate(chunks) if c.policy_id == "CP-2.1")
        vectors = []
        for i in range(len(chunks)):
            vector = [0.0] * EMBEDDING_DIMENSIONS
            vector[i] = 1.0
            vectors.append(vector)

        class DeterministicEmbedder:
            model = "controlled-consumer-sql-test"
            async def embed_query(self, text):
                return vectors[target_index]

        with psycopg.connect(db_url) as conn:
            store.ensure_schema(conn)
            store.replace_version(conn, chunks, vectors, embedding_model=DeterministicEmbedder.model)
        try:
            async with provided_server(PgPolicyRepository(db_url), DeterministicEmbedder()) as client:
                result = await retrieve_policy_evidence(
                    {"policy_version": version, "trace_id": TRACE},
                    query="정책 출처 SQL 연결 검사", category="PROFANITY", top_k=1, tools=client,
                )
            evidence = result["policy_evidence"]
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["policyId"], "CP-2.1")
            self.assertEqual(evidence[0]["text"], chunks[target_index].text)
            self.assertEqual(evidence[0]["resourceUri"], chunks[target_index].resource_uri)
        finally:
            with psycopg.connect(db_url) as conn:
                conn.execute("DELETE FROM rag.policy_chunks WHERE version = %s", (version,))


if __name__ == "__main__":
    unittest.main()
