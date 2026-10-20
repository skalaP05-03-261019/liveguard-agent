import os
import unittest
import uuid
from dataclasses import replace

import psycopg

from app.rag_mcp import store
from app.rag_mcp.config import DEFAULT_POLICY_VERSION, EMBEDDING_DIMENSIONS, POLICY_DIR, IndexSettings
from app.rag_mcp.index import build_index
from app.rag_mcp.policy import DOCUMENT_ID, parse_policy
from app.schemas import Category

POLICY_FILE = POLICY_DIR / f"policy-v{DEFAULT_POLICY_VERSION}.md"

EXPECTED_POLICY_IDS = [
    "CP-1.1", "CP-1.2", "CP-2.1", "CP-2.2", "CP-3.1", "CP-3.2", "CP-4.1",
    "CP-4.2", "CP-5.1", "CP-5.2", "SP-2.1", "SP-3.1", "CP-6.1", "MP-1.1",
    "MP-1.2", "MP-1.3", "MP-1.4", "MP-2.1", "MP-2.2", "MP-2.3", "MP-3.1",
    "MP-3.2", "MP-4.1", "MP-4.2", "MP-4.3", "SP-4.1", "MM-1.1", "MM-2.1",
    "CP-7.1",
]


class FakeEmbedder:
    model = "fake-embedding"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(i % 7) / 7] * EMBEDDING_DIMENSIONS for i, _ in enumerate(texts)]


class PolicyParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.chunks = parse_policy(
            POLICY_FILE.read_text(encoding="utf-8"), version=DEFAULT_POLICY_VERSION
        )
        self.by_id = {chunk.policy_id: chunk for chunk in self.chunks}

    def test_splits_every_policy_section_in_order(self) -> None:
        self.assertEqual([c.policy_id for c in self.chunks], EXPECTED_POLICY_IDS)

    def test_excludes_text_outside_policy_sections(self) -> None:
        for chunk in self.chunks:
            self.assertNotIn("# 제", chunk.text)
            self.assertNotIn("운영정책 v", chunk.text)
            self.assertFalse(chunk.text.startswith("#"))

    def test_keeps_section_body_and_title(self) -> None:
        profanity = self.by_id["CP-2.1"]
        self.assertEqual(profanity.title, "욕설 (PROFANITY)")
        self.assertIn("인용, 설명, 제지 목적이어도 위반", profanity.text)
        self.assertIn("5개다", self.by_id["CP-1.2"].text)
        self.assertIn("| `PHISHING` | HIDE + TIMEOUT 600초 | HIDE + TIMEOUT 600초 |",
                      self.by_id["MP-2.1"].text)
        self.assertIn("공식 사이트 링크도 예외가 아니다", self.by_id["CP-5.1"].text)

    def test_previous_version_source_still_parses(self) -> None:
        # 과거 판단의 근거를 다시 볼 수 있도록 v3.0 원문도 레포에 남긴다.
        old = parse_policy((POLICY_DIR / "policy-v3.0.md").read_text(encoding="utf-8"), version="3.0")
        self.assertEqual(len(old), 23)
        self.assertNotIn("CP-5.2", {chunk.policy_id for chunk in old})

    def test_maps_categories_and_leaves_common_sections_unmapped(self) -> None:
        self.assertEqual(self.by_id["CP-2.1"].category, Category.PROFANITY)
        self.assertEqual(self.by_id["CP-3.2"].category, Category.HARASSMENT)
        self.assertEqual(self.by_id["CP-4.1"].category, Category.FLOOD)
        self.assertEqual(self.by_id["CP-4.2"].category, Category.FLOOD)
        self.assertEqual(self.by_id["CP-5.2"].category, Category.PHISHING)
        self.assertEqual(self.by_id["SP-2.1"].category, Category.PHISHING)
        self.assertEqual(self.by_id["SP-3.1"].category, Category.PII_EXPOSURE)
        # 오탐 예외와 제재 조항은 모든 유형에 적용되는 공통 조항이다.
        self.assertIsNone(self.by_id["CP-2.2"].category)
        self.assertIsNone(self.by_id["MP-4.1"].category)

    def test_builds_resource_uri_and_embedding_input(self) -> None:
        chunk = self.by_id["MP-4.1"]
        self.assertEqual(chunk.resource_uri, f"policy://{DEFAULT_POLICY_VERSION}/{DOCUMENT_ID}/MP-4.1")
        self.assertTrue(chunk.embedding_input.startswith("MP-4.1 강퇴 심사\n"))

    def test_rejects_duplicate_and_empty_documents(self) -> None:
        duplicate = "## CP-1.1 a\n본문\n## CP-1.1 b\n본문"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_policy(duplicate, version="x")
        with self.assertRaisesRegex(ValueError, "no policy sections"):
            parse_policy("# 제목\n본문", version="x")
        with self.assertRaisesRegex(ValueError, "no body"):
            parse_policy("## CP-1.1 제목\n\n## CP-1.2 다음\n본문", version="x")


class VectorLiteralTest(unittest.TestCase):
    def test_formats_pgvector_text(self) -> None:
        self.assertEqual(store.to_vector_literal([0.5, 1, -2.25]), "[0.5,1.0,-2.25]")


@unittest.skipUnless(
    os.getenv("RAG_TEST_DB_URL"), "RAG_TEST_DB_URL이 없으면 pgvector 통합 테스트를 건너뛴다"
)
class IndexDatabaseTest(unittest.TestCase):
    """실제 pgvector에 임시 버전으로 색인하고 지운다. 실제 정책 색인은 건드리지 않는다."""

    def setUp(self) -> None:
        self.db_url = os.environ["RAG_TEST_DB_URL"]
        self.version = f"test-{uuid.uuid4().hex[:8]}"
        self.settings = IndexSettings(
            db_url=self.db_url,
            openai_api_key="unused",
            embedding_model="fake-embedding",
            policy_version=DEFAULT_POLICY_VERSION,
        )

    def tearDown(self) -> None:
        with psycopg.connect(self.db_url) as conn:
            conn.execute("DELETE FROM rag.policy_chunks WHERE version = %s", (self.version,))

    def index_as_test_version(self, embedder: FakeEmbedder) -> int:
        chunks = [
            replace(chunk, version=self.version)
            for chunk in parse_policy(
                POLICY_FILE.read_text(encoding="utf-8"), version=self.version
            )
        ]
        vectors = embedder.embed([chunk.embedding_input for chunk in chunks])
        with psycopg.connect(self.db_url) as conn:
            store.ensure_schema(conn)
            store.replace_version(conn, chunks, vectors, embedding_model=embedder.model)
            return store.count_version(conn, self.version)

    def test_reindexing_same_version_replaces_rows(self) -> None:
        embedder = FakeEmbedder()
        self.assertEqual(self.index_as_test_version(embedder), len(EXPECTED_POLICY_IDS))
        self.assertEqual(self.index_as_test_version(embedder), len(EXPECTED_POLICY_IDS))

        with psycopg.connect(self.db_url) as conn:
            row = conn.execute(
                """
                SELECT category, resource_uri, embedding_model, vector_dims(embedding)
                FROM rag.policy_chunks WHERE version = %s AND policy_id = 'SP-3.1'
                """,
                (self.version,),
            ).fetchone()
        self.assertEqual(
            row,
            (
                "PII_EXPOSURE",
                f"policy://{self.version}/{DOCUMENT_ID}/SP-3.1",
                "fake-embedding",
                EMBEDDING_DIMENSIONS,
            ),
        )

    def test_build_index_reads_policy_file_and_reports_count(self) -> None:
        # build_index는 기본 버전으로 색인하므로, 결과 확인 후 같은 방식으로 정리한다.
        embedder = FakeEmbedder()
        self.version = DEFAULT_POLICY_VERSION
        with psycopg.connect(self.db_url) as conn:
            store.ensure_schema(conn)
            had_real_index = store.count_version(conn, DEFAULT_POLICY_VERSION) > 0
        if had_real_index:
            self.version = "unused"
            self.skipTest("실제 기본 버전 색인이 있어 덮어쓰지 않는다")
        self.assertEqual(build_index(self.settings, embedder), len(EXPECTED_POLICY_IDS))
        self.assertEqual(len(embedder.calls), 1)


if __name__ == "__main__":
    unittest.main()
