"""모델 입력 안전장치 (#7). 조사 루프(#9)의 present_data 연결 지점을 구현한다.

- 개인정보 마스킹: 본문 문자열만 가린다. ID·버전·URI 같은 식별자는 그대로 둬야 모델이
  올바른 messageId·policyId로 판정을 제출할 수 있다.
- 길이 제한: 문자열당 12,000자, 잘리면 [TRUNCATED] (조사 루프가 잘린 문맥의 확정 판정을 거부한다).
- 지시문 무력화: 채팅 속 채팅 템플릿 제어 토큰을 무력화한다. 데이터 속 지시를 따르지 않는 규칙은
  prompts/investigator.md와 UNTRUSTED_* 데이터 표시가 맡는다.
"""

import re
from typing import Any

MAX_TEXT_CHARS = 12_000
TRUNCATED = "[TRUNCATED]"

# 키 이름이 식별자를 뜻하면 값(문자열 또는 문자열 목록)을 가리지 않는다.
_IDENTIFIER_KEY = re.compile(r"(Id|Ids|Uri|Version|_id|_ids|_uri|_version)$")
_IDENTIFIER_KEYS = {"version", "dataKind", "verdict", "categories", "category", "role", "visibility"}

# 순서가 중요하다. 더 구체적인 형식을 먼저 가린다.
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)"), "[RRN]"),
    (re.compile(r"(?<!\d)\d{4}([- ])\d{4}\1\d{4}\1\d{4}(?!\d)"), "[CARD]"),
    (re.compile(r"(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)0\d{1,2}[- .]\d{3,4}[- .]\d{4}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)\d{2,6}-\d{2,6}-\d{2,8}(?!\d)"), "[ACCOUNT]"),
    (re.compile(r"(?<!\d)\d{10,16}(?!\d)"), "[ACCOUNT]"),
]
_CONTROL_TOKEN = re.compile(r"<\|[^|<>]{0,40}\|>")


def mask_text(value: str) -> str:
    for pattern, label in _PII_PATTERNS:
        value = pattern.sub(label, value)
    value = _CONTROL_TOKEN.sub("[CONTROL_TOKEN]", value)
    if len(value) > MAX_TEXT_CHARS:
        value = value[:MAX_TEXT_CHARS] + TRUNCATED
    return value


def _is_identifier(key: str | None) -> bool:
    return key is not None and (key in _IDENTIFIER_KEYS or bool(_IDENTIFIER_KEY.search(key)))


def safe_presentation(value: Any, *, _key: str | None = None) -> Any:
    """present_data 구현. 입력을 바꾸지 않고 새 값을 돌려준다."""
    if isinstance(value, dict):
        return {k: safe_presentation(v, _key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_presentation(v, _key=_key) for v in value]
    if isinstance(value, str):
        if _is_identifier(_key):
            # 식별자는 가리지 않되, 비정상적으로 긴 값은 잘라 맥락 폭주를 막는다.
            return value if len(value) <= 200 else value[:200] + TRUNCATED
        return mask_text(value)
    return value
