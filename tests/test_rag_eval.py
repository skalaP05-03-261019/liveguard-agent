"""#28 정책 검색 평가: 평가셋이 실제 원문과 맞는지, 지표 계산이 맞는지 네트워크 없이 확인한다."""

import unittest

from app.rag_mcp.config import POLICY_DIR
from app.rag_mcp.evaluate import (
    EVALSET_DIR, EvalCase, evaluate, first_hit_rank, load_evalset, summarize,
)
from app.rag_mcp.policy import CATEGORY_BY_POLICY_ID, parse_policy
from app.schemas import Category


class EvalsetTests(unittest.TestCase):
    def test_expected_sections_exist_in_policy_text(self) -> None:
        version, cases = load_evalset(EVALSET_DIR / "policy-v3.1.json")
        markdown = (POLICY_DIR / f"policy-v{version}.md").read_text(encoding="utf-8")
        section_ids = {chunk.policy_id for chunk in parse_policy(markdown, version=version)}
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(len({case.id for case in cases}), len(cases))
        for case in cases:
            with self.subTest(case=case.id):
                self.assertTrue(case.expected)
                self.assertLessEqual(set(case.expected), section_ids)

    def test_category_filter_keeps_primary_answer_searchable(self) -> None:
        # 유형을 지정해도 주 정답이 검색 후보(해당 유형 또는 공통 조항)에 남아야 공정한 평가다.
        _, cases = load_evalset(EVALSET_DIR / "policy-v3.1.json")
        for case in cases:
            if case.category is None:
                continue
            with self.subTest(case=case.id):
                primary_category = CATEGORY_BY_POLICY_ID.get(case.expected[0])
                self.assertIn(primary_category, {case.category, None})


class MetricTests(unittest.IsolatedAsyncioTestCase):
    def test_rank_and_summary(self) -> None:
        self.assertEqual(first_hit_rank(["A", "B", "C"], ["C", "B"]), 2)
        self.assertIsNone(first_hit_rank(["A"], ["Z"]))
        self.assertEqual(summarize([1, 2, None, 4]),
                         {"cases": 4, "hitAt1": 0.25, "hitAtK": 0.75, "mrr": 0.438})

    async def test_evaluate_runs_both_modes_with_one_embedding_per_question(self) -> None:
        embedded: list[str] = []
        searched: list[tuple[str | None, int]] = []

        async def embed(text: str) -> list[float]:
            embedded.append(text)
            return [0.0]

        async def search(vector, *, version, category, top_k):
            searched.append((category.value if category else None, top_k))
            ids = ["CP-3.2", "CP-2.1"] if category else ["CP-2.1", "CP-3.2"]
            return [{"policy_id": pid, "score": 0.9 - i / 10} for i, pid in enumerate(ids)]

        cases = [EvalCase("Q1", "조롱", Category.HARASSMENT, ("CP-3.2",))]
        report = await evaluate(cases, version="3.1", embed=embed, search=search)
        self.assertEqual(embedded, ["조롱"])
        self.assertEqual(searched, [(None, 4), ("HARASSMENT", 4)])
        self.assertEqual(report["results"][0]["unfiltered"]["rank"], 2)
        self.assertEqual(report["results"][0]["category"]["rank"], 1)
        self.assertEqual(report["metrics"]["category"]["hitAt1"], 1.0)
        self.assertEqual(report["misses"], {"unfiltered": [], "category": []})


if __name__ == "__main__":
    unittest.main()
