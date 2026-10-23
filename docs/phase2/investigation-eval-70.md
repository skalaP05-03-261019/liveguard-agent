# 조사 품질 평가기와 평가 사례 (#70)

후보로 넘어온 같은 사례를 Phase 1 조사와 Phase 2 개선 조사(#71)에 똑같이 넣고 결과를 비교하기 위한 평가기다. 조사 코드를 바꾸기 전에 먼저 들어가야 같은 기준으로 개선 전후를 비교할 수 있다. 후보 탐지 평가(#61, 태관)와는 별개이며, 같은 대상 문장(P2-01~20)을 공유한다.

## 구성

| 파일 | 내용 |
| --- | --- |
| `app/investigation_eval/cases.py` | 사례 형식, 사례 → `InvestigationHandoff` 변환, BE 조회 대역 |
| `app/investigation_eval/evaluate.py` | 실행, 채점, 집계, 조사 코드 지문 |
| `app/investigation_eval/__main__.py` | CLI |
| `tests/fixtures/phase2/investigation-eval-v1.json` | 평가 사례 36건 (`AUTHOR_DRAFT`) |
| `tests/fixtures/phase2/investigation-eval-v2.json` | 같은 36건. 기준 `v3.1`(v1과 동일) + `v3.2`(집단 열등 단정·배제 = HARASSMENT, CP-3.3). v1의 `v3.2-draft`(HATE)는 팀 결정으로 미채택 |
| `tests/fixtures/phase2/investigation-eval-v3.json` | v2에 PM 검수(2026-10-23, v3.2 정답) 반영, `REVIEWED`. D-P2-08·C02 보류, C05 정상(D-P2-01은 CP-3.3 기준 정리로 위반 유지), C11·C12는 BE 확정 욕설 규칙 대상이라 제외(34건). v3.1 정답은 초안 유지 |
| `tests/test_investigation_eval.py` | 통제 모델·대역 테스트 12개 |

실행 경로는 #63 인계 경로와 같다. 사례마다 결정적 ID로 BE 형태의 작업·사건·문맥·후보 결과를 만들고, `run_readonly_investigation`으로 조사기를 실행한 뒤 기존 출력 검증(`validate_analysis`)을 통과한 결과만 채점한다. Phase 1 조사는 `ExistingInvestigator`(기존 `guarded_investigate`, Harness 예산 포함)로 실행한다. #71의 조사기는 같은 자리에 꽂는다.

## 평가 사례

| 묶음 | 건수 | 용도 |
| --- | --- | --- |
| `development` | 20 | P2-01~20과 같은 대상 문장. 대상 식별이 필요한 2건(P2-06, 07)에만 문맥을 붙였다. #71 개발 중 실패 분석에 사용 |
| `confirmation` | 16 | 새로 작성. #71 개발 중에는 실행·열람하지 않고 최종 비교에만 사용 |

그룹: `HATE`(열등 단정·배제), `STEREOTYPE`(가벼운 고정관념), `ATTACK`(개인 공격), `VARIANT`(변형 욕설), `NORMAL`, `COUNTER`(반박·제지), `SELF`(자조), `UNCLEAR`(대상 불명), `INJECTION`(채팅 속 지시).

정상 사례도 조사기에 도달한 경우를 가정해 후보 힌트를 붙였다. 탐지기가 잘못 넘긴 후보를 조사가 정상으로 돌려놓는지 확인하기 위해서다.

### 기준(standard)

사례마다 기준별 정답을 둔다. 실행 시 `--standard`로 하나를 고른다.

| 기준 | 내용 |
| --- | --- |
| `v3.1` | 운영정책 v3.1 원문. 집단 혐오는 CP-6.1에 따라 `NEEDS_REVIEW` |
| `v3.2-draft` | 2026-10-22 팀 결정(HATE 신설, 법 수준). 열등 단정·배제 주장 = `VIOLATION` HATE, 근거 CP-3.3 초안([`policy-mapping-69.md`](policy-mapping-69.md)) |

정답은 작성자 초안(`AUTHOR_DRAFT`)이다. PM의 HATE 정의서와 정책·QA 검수가 끝나면 `REVIEWED`로 바꾸고 데이터셋 버전을 올린다. 정답을 바꾸면 baseline부터 다시 잰다. 한 사례에 여러 정답을 허용할 수 있다(예: 가벼운 고정관념은 정상 또는 보류). 첫 번째가 혼동행렬의 기준 정답이다.

## 채점

| 결과 | 뜻 |
| --- | --- |
| `MATCH` | 허용 판정과 일치 |
| `MISSED_VIOLATION` | 위반이 정답인데 정상으로 판정 |
| `HELD_VIOLATION` | 위반이 정답인데 보류 (안전하지만 개선 대상) |
| `FALSE_VIOLATION` | 정상·보류가 정답인데 위반으로 판정 |
| `UNSUPPORTED_NORMAL` | 보류가 정답(대상 불명 등)인데 정상으로 단정 |
| `OVER_HOLD` | 정상이 정답인데 보류 |

- **충족(satisfied):** 판정 일치 + 위반이면 유형 일치 + 확정 판정(위반·정상)이면 기대 조항 중 하나 이상 인용. 보류는 조항 인용을 요구하지 않는다.
- **근거 없는 단정(unsupportedAssertions):** `FALSE_VIOLATION` + `UNSUPPORTED_NORMAL`.
- 오류는 판정으로 바꾸지 않고 `ERROR`와 오류 코드로 남긴다. 커버리지와 함께 본다.
- 보고서에는 채팅 원문, 모델 이유, 정책 원문을 넣지 않는다. 실패 분석용 이유·부족한 사실은 `--details-out` 로컬 파일(권한 0600)에만 쓴다.

## 실행

외부 호출 없는 재생(예측 파일이 있을 때):

```bash
uv run --locked python -m app.investigation_eval \
  --cases tests/fixtures/phase2/investigation-eval-v1.json \
  --standard v3.2-draft --predictions local-data/predictions.json \
  --policy-version v3.1 --out local-data/inv-eval-replay.json
```

실제 모델 + RAG MCP (API 비용 발생):

```bash
# .env: MODEL_PROVIDER, MODEL_NAME, MODEL_API_KEY 또는 OPENAI_API_KEY,
#       RAG_MCP_URL, RAG_MCP_AGENT_TOKEN (RAG MCP에 해당 정책 버전 색인 필요)
uv run --locked --env-file .env python -m app.investigation_eval \
  --cases tests/fixtures/phase2/investigation-eval-v1.json \
  --standard v3.2-draft --live --limit 20 --policy-version v3.1 \
  --out local-data/inv-eval-baseline-dev.json --details-out local-data/inv-eval-baseline-dev.details.json
```

- 기본 묶음은 `development`다. 확인용은 `--split confirmation`을 명시해야 실행된다.
- `--live`는 `--limit`(1~200)이 필수다. 출력 파일은 새로 만들 때만 쓰고 덮어쓰지 않는다.
- 같은 결과를 두 기준으로 보려면 `--details-out` 파일의 행(`verdict`, `categories`, `policyIds`, `missingFacts`)을 `{id: 행}` 예측 파일로 바꿔 다른 `--standard`로 재생한다(`reason`은 무시하도록 빼고 넣는다). 모델을 다시 호출하지 않는다. 실행 중 오류가 난 사례는 details에 `{"status": "ERROR", "errorCode": …}`로 남고, 재생 때도 같은 오류로 집계된다(판정을 만들어 채우지 않는다).

## Phase 1 baseline 절차

1. 팀이 `phase1-end` 태그를 정한다.
2. 조사 코드가 태그와 같은지 확인한다. 보고서의 `execution.files`는 조사기를 이루는 파일별 SHA-256이다. 아래 명령의 출력이 비어 있어야 Phase 1 조사다.

   ```bash
   git diff phase1-end -- prompts/investigator.md app/investigation app/nodes.py app/policy_evidence.py app/harness/execution.py app/harness/safety.py
   ```

3. `--split development`와 `--split confirmation`을 각각 실행해 따로 기록한다. 두 결과를 합쳐 하나의 정확도로 말하지 않는다.
4. #71 이후 같은 사례·같은 기준·같은 모델로 다시 실행해 비교한다.

## 한계

- 평가 사례는 작성자 초안이며, 확인용도 같은 작성자가 만들었다. 독립 확인은 정책·QA 검수와 태관 쪽 UnSmile 확인용 사례로 보강한다.
- BE 조회는 대역이다. 실제 사건 생성·가림·운영자 결정·제재는 실행하지 않는다.
- 지연은 사례별 순차 실행 시간이며 실시간 처리량이 아니다. 토큰은 금액이 아니다.
- `v3.2-draft`의 HATE 정답은 현재 출력 계약(`Category`)에 없는 유형이다. 유형이 추가되기 전 조사기는 HATE 위반을 낼 수 없으므로, 이 기준의 HATE 사례는 Phase 1에서 `HELD_VIOLATION`으로 나오는 것이 정상이다.
