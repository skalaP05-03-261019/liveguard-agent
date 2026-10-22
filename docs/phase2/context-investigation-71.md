# 후보 신호를 활용한 문맥 조사 (#71)

후보 탐지가 넘긴 사건을 조사할 때 후보 신호를 참고하고, 정책 대응표(#69, [`policy-mapping-69.md`](policy-mapping-69.md))의 판단 사실(대상·표현·문맥·정책)을 순서대로 확인하도록 조사기를 확장한다. 결과는 기존 출력 계약 그대로이며, 운영자용 설명은 `reason` 형식으로 정리한다.

## 변경

| 파일 | 내용 |
| --- | --- |
| `prompts/investigator-phase2.md` | Phase 2 추가 지침. Phase 1 지침 뒤에 붙인다 |
| `app/phase2_investigation/investigator.py` | `Phase2Investigator`: #63 `Investigator` 인터페이스 구현. 기존 Harness 예산·안전 표시·출력 검증 사용 |
| `app/investigation/runner.py` | `run_investigation`에 `prompt`, `extra_input` 인자 추가. 기본값이면 Phase 1과 동일 |
| `app/investigation_eval/` | `--investigator phase2` 선택, 지문에 추가 파일 포함, `reason` 형식 준수 수 집계 |

`prompts/investigator.md`(Phase 1 지침)는 바꾸지 않았다. Phase 1 지침의 수정(예: #57 역할 해석)은 두 조사기에 함께 적용된다.

## 추가 지침 요약

- **후보 힌트:** 종류와 강도, 탐지기가 적은 부족한 사실만 전달한다. 탐지기의 이유 문장은 넘기지 않는다. 힌트는 확인 관점일 뿐 위반 근거가 아니다.
- **지역비하·집단 혐오:** 지칭 집단과 정체성 속성 → 열등 단정·배제 주장인지(단순 언급, 정책 비판, 가벼운 고정관념은 아님) → 인용·반박·제지·자조인지 → 현재 정책 버전에 집단 혐오 조항이 있는지 순서로 확인한다. 조항과 허용 유형이 없으면 CP-6.1 `NEEDS_REVIEW`이며, missingFacts에 확인한 집단과 표현을 바탕으로 운영자가 결정할 사항을 적는다. HARASSMENT로 바꿔 판정하지 않는다.
- **개인 공격·변형 욕설:** 대상이 문맥에서 식별되면 CP-3.2, 대상 없는 감탄은 CP-2.2, 식별 불가면 보류.
- **reason 형식:** `대상: … 표현: … 문맥: … 판단: …` 300자 이내.

HATE 유형은 아직 출력 계약(`Category`)과 정책 원문에 없다. PM 정의 확정 후 정책 v3.2 색인과 유형 추가가 끝나면, 이 지침의 "조항과 허용 유형이 있으면" 경로로 위반을 제안할 수 있다. 지침은 다시 바꿀 필요가 없다.

## 평가

```bash
uv run --locked --env-file .env python -m app.investigation_eval \
  --cases tests/fixtures/phase2/investigation-eval-v1.json \
  --standard v3.1 --investigator phase2 --live --limit 20 --policy-version v3.1 \
  --out local-data/inv-eval-phase2-dev.json --details-out local-data/inv-eval-phase2-dev.details.json
```

- 개발 중에는 `development` 20건만 사용한다. `confirmation` 16건은 최종 비교에서 Phase 1 baseline과 함께 한 번 실행한다.
- 한 번에 지침 하나만 바꾸고, 바꿀 때마다 같은 사례로 다시 잰다. 결과가 나빠진 변경도 기록한다.
- 실제 모델 결과는 아직 없다. Phase 1 baseline(#70)을 먼저 잰 뒤 측정한다.

## 머지 조건

이 변경은 `runner.py`를 바꾸므로, Phase 1 baseline을 `phase1-end` 기준 조사 코드로 잰 뒤에 머지한다. #70 평가기(PR #87) 위에 쌓인 브랜치다.
