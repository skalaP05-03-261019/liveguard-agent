# 개발셋 관찰·불일치 검수 큐 (#81)

## 목적과 경계

정책·QA 검수를 기다리는 동안 현재 후보 탐지기가 무엇을 반환하는지 확인한다. 사람이 검수하지 않은 AI_DRAFT를 서비스 정답으로 간주하지 않는다. 기존 `curation export`의 검수 조건은 그대로 유지하고, 별도 개발 진단에서 **정확도·precision·recall·F1·혼동행렬을 계산하지 않는다**.

[OpenAI의 평가 안내](https://developers.openai.com/api/docs/guides/evaluation-getting-started)도 전문가의 기대값 검수를 구분한다. 이번 도구는 로컬 실행 도구이며 OpenAI Datasets/Evals 플랫폼에 데이터셋을 업로드하지 않는다. 실모델 모드에서는 기존 후보 모듈의 설정된 API로 선택한 원문만 보낸다.

`development`/`train` 검수 파일만 허용한다. `confirmation`은 모델 초기화 이전에 거부한다. 원본 SHA·원문·행·라벨·ID의 대조를 수행한다. 원본 라벨, AI 초안과 검수 기대값은 탐지 모델 입력에 포함하지 않는다. 문맥을 만들지 않고 빈 context를 사용한다.

## 실행

원본·검수 파일은 #78 절차로 준비한다. 코드·프롬프트·정책은 이번 진단을 위해 수정하지 않는다. 모델은 기존 명시 설정을 사용한다.

```bash
python -m app.candidate_detection.diagnose \
  --review local-data/unsmile-review-v1/development-review.json \
  --source local-data/unsmile-source/unsmile_train_v1.0.tsv \
  --live --limit 40 --timeout 8 \
  --out-dir local-data/diagnostics-r1
```

- live는 명시적 `--limit 1..100` 필수. 순차 호출하며 기존 CandidateScreen의 시간 제한·출처 검증을 사용한다. 재시도는 추가하지 않는다.
- 인증·권한 오류가 발생하면 남은 호출은 NOT_RUN으로 기록한다. 다른 모델 오류는 ERROR이며 정상이나 초안과 일치한 사례에 넣지 않는다.
- 외부 예측 재생은 `--live` 대신 `--predictions local-data/predictions.json`을 사용한다. 선택한 입력과 ID가 정확히 일치해야 하며 원문 인용도 검증한다. 모델 호출 성능 증거로 해석하지 않는다.
- 새 출력 디렉터리만 허용한다. 디렉터리0700/파일0600이며 기존 파일을 덮어쓰지 않는다. 출력 I/O 실패 시 부분 파일이 남을 수 있다.

## 출력

| 파일 | 내용·보관 |
|---|---|
| diagnostics.json | 행별 모델 신호·인용·이유, AI 초안과의 비교. 로컬 전용 |
| review-queue.txt | 원문·원본 라벨·AI 초안·모델 관찰을 모은 텍스트. 로컬 전용 |
| summary.json | 원문·행 ID·모델 이유가 없는 집계·해시. 공개 증거로 별도 확인 후 사용 가능 |

큐는 호출 오류 → clear-or-possible 실험 기준 초안 불일치 → 초안 미정 → 초안 일치 순서다. 이 순서는 검수 편의를 위한 것이며 위험도·조치 우선순위가 아니다. plain text를 사용하므로 채팅을 실행 가능한 HTML로 취급하지 않는다.

비교 값은 `AGREES_WITH_AI_DRAFT`, `DIFFERS_FROM_AI_DRAFT`, `UNRESOLVED`다. **불일치는 모델 오류 확정도, 초안 오류 확정도 아니다.** 정책·QA 담당은 둘 다 수정할 수 있다. 모델은 검수 상태나 기대값을 변경하지 않으며 미검수 export는 여전히 거부된다.

요약은 세 가지 실험용 선택 기준의 후보 수·초안 비교 수, 오류/미실행, 순차 호출 지연·토큰 사용량을 기록한다. 사용량은 금액이 아니며 호출 지연은 스트리밍 전체 지연·처리량이 아니다. source/review/input/prompt 해시로 실행 입력을 구분한다. 고객사 운영 프로필의 확정 기준으로 해석하지 않는다.

## 검증

합성 입력으로 확인용 차단·원본 위조 차단·라벨 비전달·호출 수 제한·오류/미실행·거짓 인용 거부·비공개 파일 권한·집계 원문 제외·검수 상태 불변을 검사한다. 실제 모델 진단의 집계·후속 관찰은 별도 evidence 파일로 기록한다.

최종 서비스 기대값은 사용자 선택대로 팀 정책·QA 담당과 검수한다. 검수 완료 후 승인된 사례를 #78 도구로 export하여 기존 후보 평가기로 측정한다. BE/FE·세원 정책/조사·실행기 PR #80·기존 승인/집행 경로는 변경하지 않았다.

## 실제 개발 관찰 (2026-10-22 R1)

기존 gpt-5.4-mini, 프롬프트 SHA `dfaba6af1d2b12e5ba2e1af2183e223a768ee05d1ab27fa04c92c239c852cd57`로 개발40개를 한 번씩 순차 호출했다. 40유효·오류0·미실행0이며 확인용은 호출하지 않았다. 검수 파일 해시 불변, PENDING40개 유지.

| 실험 기준 | 후보 수 | AI 초안과 일치 | 불일치 | 초안 미정 |
|---|---:|---:|---:|---:|
| clear-only | 25 | 26 | 7 | 7 |
| clear-or-possible | 32 | 29 | 4 | 7 |
| include-uncertain | 33 | 28 | 5 | 7 |

불일치와 미정은 정책·QA 검수 큐에 표시했다. 주요 논점은 지역/인종 은어의 의미·대상 확인, 사상/종교/기관 비판과 집단 공격의 경계다. AI 초안·모델 중 어느 쪽이 맞는지 확정하지 않았으며 프롬프트를 바꾸지 않았다.

단일 순차 모델 호출 관찰 p50 1,344.05ms / p95 2,286.71ms, 입력47,095·출력6,691토큰. 독립 정확도·실서비스 지연·실시간 처리량 증거가 아니다. 집계/해시는 `evidence/phase2/development-diagnostics-81.json`에 있고 원문/행별 결과는 로컬만 보관한다.

신규 합성11개 포함 전체374 tests가 격리PostgreSQL·ASGI/TCP에서 통과했다(skip0). 테스트 출력은 통제 모델이며 위 실제 호출 결과와 구분한다.

## 16개 직접 작성 사례로 빠르게 시험하기

API 키는 환경변수 `MODEL_API_KEY` 또는 `OPENAI_API_KEY`로 로컬에서 설정한다. `MODEL_PROVIDER=openai`, `MODEL_NAME=gpt-5.4-mini`를 명시한다. 키를 명령문/저장소에 기록하지 않는다. 원본 repo의 `local-data/`가 gitignore인지 확인하고 새 출력 경로를 쓴다.

```bash
python -m app.candidate_detection \
  --cases tests/fixtures/phase2/authored-regression-75.json \
  --live --limit 16 --timeout 8 \
  --out local-data/quick-authored16.json
```

실행 결과는 16유효·오류0, 후보 기준 세 실험 모두 TP8/FP0/TN8/FN0이었다. **이전에 프롬프트 개발에 쓴 직접 작성 사례**라서 100%를 일반 성능으로 발표하면 안 된다. 유형별로는 TARGETED_ATTACK이 기대값보다 2개 많았다. 두 욕설 변형 사례를 개인 공격으로도 표시했으며, 기대 유형을 팀 정책·QA가 재검토해야 한다. 모델 결과가 반드시 오판이라고 확정하지 않았다.

집계 증거는 `evidence/phase2/quick-authored16-82.json`, 행별 실행 결과는 로컬에 둔다. 정확도를 제시하려면 정책·QA 검수 완료 후 #78 export → `--cases`에 확정 사례 지정 → 분리된 확인용40개에서 재평가해야 한다.
