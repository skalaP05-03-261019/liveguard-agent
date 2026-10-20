"""운영정책 Markdown을 조항(CP/SP/MP/MM) 단위 chunk로 나눈다."""

from dataclasses import dataclass
import re

from app.schemas import Category

DOCUMENT_ID = "operating-policy"

# 조항 ID 헤딩만 chunk 시작점이다. 장 제목(# 제1장)과 조항 밖 텍스트는 색인하지 않는다.
_SECTION_HEADING = re.compile(r"^##\s+((?:CP|SP|MP|MM)-\d+\.\d+)\s+(.+?)\s*$")
_ANY_HEADING = re.compile(r"^#{1,6}\s")

# 유형별 검색 필터에 쓰는 매핑. 여기 없는 조항(판단 원칙, 오탐 예외, 제재표 등)은 공통 조항이다.
CATEGORY_BY_POLICY_ID: dict[str, Category] = {
    "CP-2.1": Category.PROFANITY,
    "CP-3.1": Category.HARASSMENT,
    "CP-3.2": Category.HARASSMENT,
    "CP-4.1": Category.FLOOD,
    "CP-5.1": Category.PHISHING,
    "SP-2.1": Category.PHISHING,
    "SP-3.1": Category.PII_EXPOSURE,
}


@dataclass(frozen=True)
class PolicyChunk:
    policy_id: str
    document_id: str
    section_id: str
    title: str
    text: str
    version: str
    category: Category | None

    @property
    def resource_uri(self) -> str:
        return f"policy://{self.version}/{self.document_id}/{self.section_id}"

    @property
    def embedding_input(self) -> str:
        # 조항 ID와 제목을 함께 넣어 "욕설 기준" 같은 질의가 제목으로도 맞도록 한다.
        return f"{self.policy_id} {self.title}\n{self.text}"


def parse_policy(
    markdown: str, *, version: str, document_id: str = DOCUMENT_ID
) -> list[PolicyChunk]:
    chunks: list[PolicyChunk] = []
    current: tuple[str, str] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            return
        policy_id, title = current
        text = "\n".join(body).strip()
        if not text:
            raise ValueError(f"{policy_id} has no body text")
        chunks.append(
            PolicyChunk(
                policy_id=policy_id,
                document_id=document_id,
                section_id=policy_id,
                title=title,
                text=text,
                version=version,
                category=CATEGORY_BY_POLICY_ID.get(policy_id),
            )
        )

    for line in markdown.splitlines():
        match = _SECTION_HEADING.match(line)
        if match:
            flush()
            current = (match.group(1), match.group(2))
            body = []
        elif _ANY_HEADING.match(line):
            flush()
            current = None
            body = []
        elif current is not None:
            body.append(line)
    flush()

    seen: set[str] = set()
    for chunk in chunks:
        if chunk.policy_id in seen:
            raise ValueError(f"duplicate policy section {chunk.policy_id}")
        seen.add(chunk.policy_id)
    if not chunks:
        raise ValueError("no policy sections found")
    return chunks
