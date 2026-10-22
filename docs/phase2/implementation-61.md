# 후보 판별·평가 모듈 — Agent #61

## 구현한 범위

태관 담당의 **독립·조회 전용 후보 판별과 측정 도구**입니다. 새 HTTP API·BE 호출·사건 생성·가림·제재·고객사 설정 저장은 추가하지 않았습니다. 기존 조사 프롬프트/Graph/정책 검색/요약(세원님 담당)과 Inbox·Worker #60(iihlolo 담당)은 수정하지 않습니다.

- `app/candidate_detection/contracts.py`: 별도 실험 입력·출력, 위험 신호, 인용 근거 검증, 실험용 선택 기준
- `app/candidate_detection/screen.py`: 기존 모델 인터페이스를 주입받아 한 번 호출. 전체 timeout, 추가 도구/행동 금지, 출처 검증
- `prompts/candidate-screen.md`: 한국어 혐오·지역비하·공격·변형 욕설의 **추가 조사 후보** 지침
- `app/candidate_detection/evaluate.py`: 실제 모델 또는 외부 예측 파일을 평가. 누락·오탐·오류·커버리지·유형·지연·토큰 기록
- `tests/fixtures/phase2/`: 직접 작성한 개발/검사 사례. UnSmile 원문·변형본·색인이 아님

`RiskKind`는 BE 위반 enum이 아닙니다. `CLEAR/POSSIBLE/UNCERTAIN`은 모델이 기술한 신호 강도이며 보정된 확률·서비스 정확도가 아닙니다. 출력에 정상 확정·제재·정책 버전 변경 권한은 없습니다.

## 안전·실패 경계

- target 1개·context 최대8개·각 메시지500자. 식별자 중복·빈 입력 거부.
- 모델은 `report_candidate_signals`만 반환할 수 있습니다. 변경 도구나 action 필드는 거부합니다.
- 모든 신호는 대상 원문의 인용을 포함해야 합니다. 문맥 메시지 단독으로 다른 발언을 표적화하지 않습니다.
- 인용 messageId와 문구를 입력에 대조합니다. 모델 지시에도 원문 전체 복사를 요구하고 해당 입력의 ID/text enum을 제공합니다. runtime은 여전히 출처를 다시 검증합니다.
- 불명확한 신호에는 missingFacts가 필요합니다. 추가 신호 없음은 위반 없음 확정이 아닙니다.
- 호출은 1회, 기본8초·설정 최대30초, 자동 재시도 없음. 오류를 정상 결과로 바꾸지 않습니다.
- 이 CLI의 독립 timeout은 기존 서비스 Harness를 대체하지 않습니다. 실제 서비스 연결 전 공유 예산·인증·처리량·BE 실패 정책 계약이 필요합니다.

## 실험용 선택 기준

| 이름 | 후보로 선택하는 신호 | 상태 |
|---|---|---|
| `clear-only` | CLEAR | 실험 |
| `clear-or-possible` | CLEAR/POSSIBLE | 실험 |
| `include-uncertain` | CLEAR/POSSIBLE/UNCERTAIN | 실험 |

한 모델 출력에 세 선택 기준을 적용해 모델 호출 횟수를 세 배로 늘리지 않습니다. 이는 레벨형 후보 범위 비교용이며 고객사 설정 UI·기본값·정책 승인이 아닙니다. 항목형·자연어형과 최종 선택은 팀 협의입니다.

## 재현

저장소 루트에서 실행합니다. Python·의존성은 기존 `uv.lock`을 사용합니다.

```bash
uv sync --locked --python 3.11
uv run --locked python -m unittest discover -s tests -p test_candidate_detection.py -v

# 외부 호출 없는 평가기 검사: 일부러 TP/FP/TN/FN을 각각 1개씩 넣은 통제 출력
uv run --locked python -m app.candidate_detection \
  --cases tests/fixtures/phase2/metrics-smoke-cases.json \
  --predictions tests/fixtures/phase2/metrics-smoke-predictions.json \
  --out local-data/metrics-smoke.json
```

통제 출력의 50% 지표는 AI 모델 성능이 아닙니다. confusion 계산이 기대대로 동작하는지 확인하는 예시입니다.

실제 모델은 명시적으로 설정한 기존 OpenAI adapter를 재사용합니다. 로컬 `.env`에 `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY` 또는 `OPENAI_API_KEY`를 설정하고 키를 출력·공유하지 않습니다. 모델은 기존 통합 실행과 같은 명시 설정을 사용하며 자동 모델 선택·업그레이드는 하지 않습니다.

```bash
# API 비용이 발생합니다. 최초에는 5개로 연결/출력 확인 후 범위를 늘립니다.
uv run --locked --env-file .env python -m app.candidate_detection \
  --cases tests/fixtures/phase2/authored-candidates-v1.json \
  --live --limit 5 --timeout 20 \
  --out local-data/candidates-live-smoke.json
```

`--live`에는 명시적인 `--limit`(1~100)이 필요합니다. 출력 파일이 이미 있으면 덮어쓰지 않습니다. 실제 모델·프롬프트 SHA·평가 입력 SHA가 보고서에 기록됩니다. 모델 자체의 비결정성 때문에 같은 실행을 반복해도 결과가 달라질 수 있습니다.

## 지표와 공개 범위

- 성공한 행만 계산한 Precision/Recall/F1·정상 후보율과 함께 전체/성공/오류/coverage를 반드시 표시합니다.
- 기대 후보인데 오류로 평가되지 않은 행을 포함한 미도달 건수도 별도 기록합니다. 오류를 FN/TN으로 조용히 섞지 않습니다.
- `expectedKinds=null`인 대상 불명확 사례는 유형별 점수에서 제외합니다. 후보 평가에는 포함합니다.
- 지연은 이 순차 실행에서 모델 응답·검증까지의 시간입니다. BE 수신→카드 도착 지연이나 실시간 처리량이 아닙니다.
- 토큰은 기록하되 모델 가격이나 금액을 추정하지 않습니다. 오류로 버려진 응답의 사용량은 현재 집계에 없으므로 전체 과금량으로 인용하지 않습니다.
- 보고서는 원문·인용·판단 이유·예외 메시지·키를 저장하지 않습니다. case ID·정답/선택 결과·신호 종류·오류 코드·지연·반환된 사용량만 기록합니다.
- 보고서에 text가 없더라도 고객사 사례 ID와 파생 결과는 공개 허가 없이 게시하지 않습니다. 공개 증거는 직접 작성한 사례만 사용합니다.

## 데이터와 baseline 상태

20개 사례는 개발자가 작성한 진단용 초안입니다. 팀 검수·대표성·UnSmile holdout 평가를 완료한 자료가 아닙니다. 이 사례를 보며 프롬프트를 수정했으므로 독립 확인셋 결과로 주장하지 않습니다.

UnSmile은 사용 조건·서비스 라벨·출처를 확인한 후 로컬에서 선별합니다. 원본 라벨을 BE 유형으로 자동 치환하거나 데이터 원문·변형본·색인·모델 가중치를 이 PR에 게시하지 않습니다. 커뮤니티 은어 수집·학습도 아직 하지 않았습니다.

착수 시 `phase1-end` 태그는 없습니다. 시작 main SHA를 기록했지만 팀 승인된 Phase 1 종료 태그를 임의로 만들지 않았습니다. 이번 실제 모델 결과는 **새 독립 후보 모듈의 개발 진단**이며 Phase 1과의 개선율이 아닙니다. 팀 종료 커밋·같은 평가셋·기존 BE 필터 결과를 고정한 비교는 다음 단계입니다.

## 다음 연결

1. 공통 개발/확인셋과 서비스 기대 판정을 태관·세원님·정책/QA 담당자가 검수합니다.
2. 후보 입력·출력·시간 초과·노출 정책을 BE와 합의합니다. 별도 신호를 새 제재 enum처럼 사용하지 않습니다.
3. 기존 확정 규칙·사건·승인 흐름을 보존한 BE 어댑터를 연결하고 실제 도달률·지연·과부하를 측정합니다.
4. 세원님 조사 모듈과 정책 근거를 이어 운영자 정상/위반/보류·복구를 회귀 검증합니다.
5. 요약 기술은 세원님 담당으로 병행하고, 이용 역할·시점은 미정으로 유지합니다.
