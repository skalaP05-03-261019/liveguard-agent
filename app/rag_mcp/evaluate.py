"""정책 검색 정확도 평가 (#28): uv run --env-file .env python -m app.rag_mcp.evaluate

고정 평가셋의 질문을 실제 검색 경로(질의 임베딩 → pgvector search)로 돌려,
정답 조항이 몇 번째에 나오는지 잰다. 유형 미지정 검색과 유형 지정 검색을 모두 잰다.
search_policy 도구와 같은 repository.search를 쓰므로 도구가 돌려줄 결과와 같다.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from app.schemas import Category

EVALSET_DIR = Path(__file__).parent / "evalsets"
DEFAULT_TOP_K = 4  # search_policy 기본값과 같다.
MODES = ("unfiltered", "category")

Embed = Callable[[str], Awaitable[list[float]]]
Search = Callable[..., Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class EvalCase:
    id: str
    query: str
    category: Category | None
    expected: tuple[str, ...]


def load_evalset(path: Path) -> tuple[str, list[EvalCase]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        EvalCase(
            id=item["id"],
            query=item["query"],
            category=Category(item["category"]) if item.get("category") else None,
            expected=tuple(item["expected"]),
        )
        for item in raw["cases"]
    ]
    return raw["policyVersion"], cases


def first_hit_rank(returned: Sequence[str], expected: Sequence[str]) -> int | None:
    """정답으로 인정하는 조항 중 가장 먼저 나온 순위(1부터). 없으면 None."""
    for rank, policy_id in enumerate(returned, start=1):
        if policy_id in expected:
            return rank
    return None


def summarize(ranks: Sequence[int | None]) -> dict[str, float]:
    total = len(ranks)
    if total == 0:
        return {"cases": 0, "hitAt1": 0.0, "hitAtK": 0.0, "mrr": 0.0}
    return {
        "cases": total,
        "hitAt1": round(sum(rank == 1 for rank in ranks) / total, 3),
        "hitAtK": round(sum(rank is not None for rank in ranks) / total, 3),
        "mrr": round(sum(1 / rank for rank in ranks if rank) / total, 3),
    }


async def evaluate(
    cases: Sequence[EvalCase], *, version: str, embed: Embed, search: Search,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    results = []
    for case in cases:
        vector = await embed(case.query)  # 질문당 임베딩 1회, 두 방식에 같이 쓴다.
        record: dict[str, Any] = {
            "id": case.id, "query": case.query,
            "category": case.category.value if case.category else None,
            "expected": list(case.expected),
        }
        for mode in MODES:
            category = case.category if mode == "category" else None
            rows = await search(vector, version=version, category=category, top_k=top_k)
            returned = [row["policy_id"] for row in rows]
            record[mode] = {
                "returned": returned,
                "scores": [round(float(row["score"]), 4) for row in rows],
                "rank": first_hit_rank(returned, case.expected),
            }
        results.append(record)
    metrics = {mode: summarize([r[mode]["rank"] for r in results]) for mode in MODES}
    misses = {
        mode: [r["id"] for r in results if r[mode]["rank"] is None] for mode in MODES
    }
    return {"topK": top_k, "metrics": metrics, "misses": misses, "results": results}


def _print_report(report: dict[str, Any]) -> None:
    print(f"policy {report['policyVersion']} · {report['embeddingModel']} · "
          f"{report['metrics']['unfiltered']['cases']} questions · topK {report['topK']}")
    print(f"{'mode':<12}{'hit@1':>8}{'hit@' + str(report['topK']):>8}{'MRR':>8}")
    for mode in MODES:
        m = report["metrics"][mode]
        print(f"{mode:<12}{m['hitAt1']:>8.0%}{m['hitAtK']:>8.0%}{m['mrr']:>8.3f}")
    for record in report["results"]:
        marks = " ".join(
            f"{mode[:4]}={record[mode]['rank'] or '-'}" for mode in MODES
        )
        print(f"  {record['id']} {marks:<16} expected {'/'.join(record['expected']):<14}"
              f" got {', '.join(record['unfiltered']['returned'])}")


async def _run(evalset: Path, out: Path | None) -> dict[str, Any]:
    from app.rag_mcp.config import IndexSettings
    from app.rag_mcp.embeddings import AsyncOpenAIEmbedder
    from app.rag_mcp.repository import PgPolicyRepository

    settings = IndexSettings.from_env()
    version, cases = load_evalset(evalset)
    embedder = AsyncOpenAIEmbedder(settings.openai_api_key, settings.embedding_model)
    repository = PgPolicyRepository(settings.db_url)
    indexed = await repository.list_versions()
    if version not in indexed:
        raise SystemExit(f"policy {version} is not indexed (indexed: {sorted(indexed)})")
    report = await evaluate(cases, version=version, embed=embedder.embed_query,
                            search=repository.search)
    report = {
        "policyVersion": version,
        "embeddingModel": indexed[version],
        "evaluatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evalset": evalset.name,
        **report,
    }
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 정책 검색 정확도 평가")
    parser.add_argument("--evalset", type=Path, default=EVALSET_DIR / "policy-v3.1.json")
    parser.add_argument("--out", type=Path, default=None, help="결과 JSON 저장 경로")
    args = parser.parse_args()
    _print_report(asyncio.run(_run(args.evalset, args.out)))


if __name__ == "__main__":
    main()
