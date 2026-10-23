# 후보 신호를 활용한 문맥 조사 (#71)

후보 탐지가 넘긴 사건을 조사할 때 후보 신호를 참고하고, 정책 대응표(#69, [`policy-mapping-69.md`](policy-mapping-69.md))의 판단 사실(대상·표현·문맥·정책)을 순서대로 확인하도록 조사기를 확장한다. 결과는 기존 출력 계약 그대로이며, 운영자용 설명은 `reason` 형식으로 정리한다.

## 변경

| 파일 | 내용 |
| --- | --- |
| `prompts/investigator-phase2.md` | Phase 2 공통 추가 지침(후보 힌트, 개인 공격, reason 형식). 사건 정책 버전의 기본 지침 뒤에 붙인다 |
| `prompts/investigator-phase2-v3.1.md`, `-v3.2.md` | 버전별 집단 혐오 절. v3.1은 CP-6.1 보류, v3.2는 CP-3.3 근거 HARASSMENT. 소급하지 않는다 |
| `app/phase2_investigation/investigator.py` | `Phase2Investigator`: #63 `Investigator` 인터페이스 구현. 기존 Harness 예산·안전 표시·출력 검증 사용 |
| `app/investigation/runner.py` | `run_investigation`에 `prompt`, `extra_input` 인자 추가. 기본값이면 Phase 1과 동일 |
| `app/investigation_eval/` | `--investigator phase2` 선택, 지문에 추가 파일 포함, `reason` 형식 준수 수 집계 |

`prompts/investigator.md`(Phase 1 지침)는 바꾸지 않았다. Phase 1 지침의 수정(예: #57 역할 해석)은 두 조사기에 함께 적용된다.

정책 v3.2(#106·#108) 대응: `system_prompt(version)`은 `prompt_for_policy(version)`(#106) 위에 공통 추가 지침과 버전별 절을 합친다. `runner.py`는 #106 버전을 그대로 쓴다. 이 브랜치는 #106 위에 쌓여 있으므로 #106 다음에 병합한다.

## 추가 지침 요약

- **후보 힌트:** 종류와 강도, 탐지기가 적은 부족한 사실만 전달한다. 탐지기의 이유 문장은 넘기지 않는다. 힌트는 확인 관점일 뿐 위반 근거가 아니다.
- **지역비하·집단 혐오 (버전별 절):** 지칭 집단과 정체성 속성 → 열등 단정·배제 주장인지(단순 언급, 정책 비판, 가벼운 고정관념은 아님) → 인용·반박·제지·자조인지 순서로 확인한다. v3.1은 CP-6.1 `NEEDS_REVIEW`(HARASSMENT로 바꾸지 않음), v3.2는 CP-3.3 근거 `VIOLATION` HARASSMENT. 조회는 판단에 필요한 조항 2개까지(EXP-02).
- **개인 공격·변형 욕설:** 대상이 문맥에서 식별되면 CP-3.2, 대상 없는 감탄은 CP-2.2, 식별 불가면 보류.
- **판정별 근거 조항 (EXP-03):** 실제로 원문을 받은 조항만 인용. 정상 판정은 CP-2.2와 TARGET 메시지, 위반은 유형의 금지 조항, 보류는 CP-2.2 보류 규칙 또는 CP-6.1. 정상으로 판단하려면 CP-2.2를 반드시 조회한다.
- **reason 형식:** `대상: … 표현: … 문맥: … 판단: …` 300자 이내.

## 평가

```bash
uv run --locked --env-file .env python -m app.investigation_eval \
  --cases tests/fixtures/phase2/investigation-eval-v2.json \
  --standard v3.2 --investigator phase2 --live --limit 20 --policy-version v3.2 \
  --out local-data/inv-eval-phase2-dev.json --details-out local-data/inv-eval-phase2-dev.details.json
```

- 개발 중에는 `development` 20건만 사용한다. `confirmation` 16건은 최종 비교에서 Phase 1 baseline과 함께 한 번 실행한다.
- 한 번에 지침 하나만 바꾸고, 바꿀 때마다 같은 사례로 다시 잰다. 결과가 나빠진 변경도 기록한다.

### 실험 기록 (개발용 20건, 정답 `AUTHOR_DRAFT`)

같은 모델·같은 사례로 실행했다. 충족 = 판정·유형·기대 조항 인용이 모두 맞은 수. 근거 없는 단정 = 근거 없는 위반 + 근거 없는 정상. 오류는 출력 검증 거부·실행 한도로, 판정으로 바꾸지 않고 따로 센다.

| 실행 | 바꾼 것 | 기준 | 충족 | 근거 없는 단정 | 오류 |
| --- | --- | --- | --- | --- | --- |
| Phase 1 (`phase1-end`, #103) | — | v3.1 | 17 | 1 | 1 |
| EXP-01 | Phase 2 추가 지침 첫 적용 | v3.1 | 13 | 2 | 5 |
| EXP-02 (2회) | 조항 조회를 2개로 제한 (같은 도구 3회 한도 초과 방지) | v3.1 | 14 · 15 | 1 · 0 | 3 · 2 |
| v3.2 통합 | 버전별 절, v3.2는 CP-3.3 근거 HARASSMENT | v3.2 | 15 | 0 | 2 |
| EXP-03 (v3.2 2회) | 판정별 근거 조항, 정상 판정 시 CP-2.2 조회 필수 | v3.2 | 16 · 16 | 0 · 0 | 3 · 3 |
| EXP-03 | 〃 | v3.1 | 17 | 0 | 3 |

- **EXP-01 → 02:** 조항을 하나씩 다 조회하다 같은 도구 3회 한도에 걸리는 실행 한도 오류가 줄었다.
- **v3.2 통합:** 집단 열등 단정 사례(D-P2-01·02)가 HARASSMENT·CP-3.3으로 정답 처리. 집단 혐오를 개인 괴롭힘 조항(CP-3.2)으로 위반 처리하던 Phase 1 오류가 없어졌다.
- **EXP-03:** 정상 판정이 CP-2.2 대신 CP-3.3·CP-6.1·CP-2.1만 인용하던 문제가 줄었다(인용 충족 v3.2 13/16 → 14/15·13/14, v3.1 8/11 → 12/12). 남은 인용 실패 1건(D-P2-12)은 CP-2.2를 조회하지 않고 정상으로 판정했다.
- **남은 문제:** 출력 검증 거부(`INVALID_EVIDENCE` 등)가 실행마다 2~3건. 발생 사례가 실행마다 달라 특정 사례 문제보다 제출 형식 문제로 보인다. 다음 실험에서 거부 조건(조회한 조항만, 확정 판정은 조항과 TARGET 메시지 필수, findings는 TARGET 메시지만)을 지침에 정확히 적는다.

Phase 1과의 비교는 정답 검수(`REVIEWED`) 후 확인용 16건을 포함해 최종 기록한다. 개발용 숫자는 지침 조정에 쓴 사례라 성능 주장의 근거로 쓰지 않는다.

## 머지 조건

Phase 1 baseline은 `phase1-end` 조사 코드로 측정했다(#103). 이 브랜치는 정책 v3.2(#106) 위에 쌓여 있으므로 #106 다음에 머지한다.
