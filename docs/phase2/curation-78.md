# UnSmile 로컬 선별·팀 검수 (#78)

## 구현 범위와 검수 책임

태관 담당 후보 평가 준비 도구다. 최종 서비스 기대값은 **팀 정책·QA 담당과 검수**한다(사용자 결정). 원본 라벨이나 AI 초안을 승인으로 변환하지 않는다. BE/FE·조사·정책·요약 모듈·제재 흐름은 수정하지 않는다.

`python -m app.candidate_detection.curation`은 로컬 TSV만 읽는다. 네트워크·모델·BE 호출이 없다. 기존 평가기의 EvalCase로 내보내는 준비 도구이며 실시간 탐지 경로에 자동 연결되지 않는다.

## 원본·선별 기준

[공식 UnSmile 저장소](https://github.com/smilegate-ai/korean_unsmile_dataset)의 커밋 `569ab5f495e046911898805d3bee92469988dcd9`를 고정했다. 원문·라벨은 수정하지 않고 별도 검수 필드에 서비스 기대값을 기록한다. 이용·공유 조건은 공식 README의 데이터셋 라이선스와 사용 안내를 확인한다. 이번 공개 PR에는 원문·행별 파생 분석·내보낸 사례를 포함하지 않는다.

- train 15,005행 → 개발용 40개, valid 3,737행 → 확인용 40개.
- 지역·여성/가족·남성·성소수자·인종/국적·연령·종교·기타 혐오·악플/욕설·clean 각각 4개. 복수 라벨은 이 순서의 첫 항목을 선별 버킷으로 사용하고 원본 복수 라벨은 보존한다. 개인지칭은 추가 표식이다.
- 고정 seed와 원본 SHA·행 번호로 재현한다. 행 번호는 헤더를 제외한 논리 레코드 번호다.
- 기존 개발 입력 36개의 정규화 해시를 제외하고, 확인용에서는 **train 전체**의 정규화 해시도 제외한다. NFKC·대소문자·공백 차이를 검사한다. 의역·동일 의미까지 탐지하지는 않으므로 검수자가 같은 변형 계열에 공통 `groupId`를 지정해 분할 간 겹침을 확인한다.
- 균등 선별 사례는 실제 서비스 빈도나 성능의 대표 표본이 아니다. 원본 댓글에 스트리밍 문맥을 만들어 붙이지 않는다. 내보낸 EvalCase의 context는 빈 목록이다.

## 실행

원본·검수 파일은 gitignored `local-data/`에 둔다. prepare와 export 출력 디렉터리는 새 경로여야 한다. 출력 디렉터리 0700, 파일 0600을 사용하고 기존 파일은 덮어쓰지 않는다.

```bash
python -m app.candidate_detection.curation prepare \
  --train local-data/unsmile-source/unsmile_train_v1.0.tsv \
  --valid local-data/unsmile-source/unsmile_valid_v1.0.tsv \
  --revision 569ab5f495e046911898805d3bee92469988dcd9 \
  --seed liveguard-phase2-20261022-v1 --per-bucket 4 \
  --exclude-cases tests/fixtures/phase2/authored-candidates-v1.json \
  --exclude-cases tests/fixtures/phase2/authored-regression-75.json \
  --out-dir local-data/unsmile-review-v1
```

`development-review.json`만 개발·프롬프트 개선에 사용한다. `confirmation-review.json`은 별도 검수 후 최종 확인에 사용하며 튜닝 입력으로 사용하지 않는다. 검수 담당도 확인용 표현을 개발 프롬프트에 전달하지 않는다.

## 검수 규칙

| 필드 | 처리 |
|---|---|
| text / originalLabels / sourceRow / id / normalizedSha256 / sourceFlags | 원본 그대로. 변경하면 내보내기 거부 |
| proposal | AI_DRAFT 제안. 서비스 정답·검수 승인으로 사용하지 않음 |
| status=PENDING | 아직 검수하지 않음 |
| status=DISAGREEMENT | 해석 불일치, 합의 필요 |
| status=APPROVED | expectedCandidate와 검수자·시간·이유 필수 |
| status=EXCLUDED | 제외 이유·검수자·시간 필수, 내보내기 수와 제외 ID 기록 |
| expectedKinds | 선택적 후보 신호. BE 제재 enum·정책 위반 확정과 다름 |
| groupId | 동일 의미·변형 계열 검수용. 분할 간 중복 거부 |

파일의 reviewer는 자기 신고 기록이며 인증·정책 승인 시스템이 아니다. 검수 담당이 실제로 확인한 뒤 작성한다. 기대값을 모르면 임의로 정상 처리하지 않고 DISAGREEMENT로 남긴다. 인용·반박·기관/사상 비판·개인 공격의 구분, 문맥 부족, 현재 후보 유형 밖의 성적 표현은 정책·QA 논의 대상으로 올린다.

공개 정책 대응표 PR #77은 제안 상태이며 신규 HATE 정책·제재 enum 승인 근거로 사용하지 않았다. 후보=true도 위반 확정이나 자동 강퇴를 뜻하지 않는다.

```bash
python -m app.candidate_detection.curation export \
  --review local-data/unsmile-review-v1/development-review.json \
  --peer local-data/unsmile-review-v1/confirmation-review.json \
  --source local-data/unsmile-source/unsmile_train_v1.0.tsv \
  --peer-source local-data/unsmile-source/unsmile_valid_v1.0.tsv \
  --out-dir local-data/unsmile-reviewed-development-v1
```

선택한 분할에 PENDING/DISAGREEMENT가 하나라도 있으면 출력 전에 거부한다. 상대 분할은 미검수여도 원본 일치·분할 중복 검사를 수행한다. 확인용 export는 review/peer와 source/peer-source를 반대로 지정한다. 검증 완료 뒤 쓰기를 시작하며 I/O 실패 시 부분 디렉터리가 남을 수 있다.

## 이번 관찰과 다음 단계

로컬 개발 40개에 AI 초안을 작성했다: 후보 제안 28, 정상 제안 5, 논의 필요 7. 전부 PENDING이며 사람 승인 0개다. 확인용 40개는 AI 초안을 작성하지 않았고 프롬프트 튜닝에 사용하지 않았다. 이 수치는 모델 성능이 아니다.

다음은 정책·QA의 서비스 기대값 검수 → 검수 완료분 export → 후보 탐지 baseline/동일 입력 비교 → 분리된 확인용 평가 순서다. 고객사 프로필별 정답은 기본 후보 기대값과 별도 합의가 필요하다. 합성 TSV 테스트만 공개하며 실제 선별 집계·해시는 `evidence/phase2/unsmile-selection-78.json`에 둔다.

검증: 합성 TSV 11개 검사를 포함한 전체 363 tests가 별도 PostgreSQL·ASGI/TCP에서 성공했으며 skip은 0개다. 실제 원본 대조와 미검수 export 거부도 확인했다.
