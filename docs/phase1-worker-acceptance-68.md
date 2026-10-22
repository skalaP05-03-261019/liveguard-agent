# Phase 1 Agent Worker 인수 검증 (#68)

## 결과와 담당 경계

사용자가 Phase 2 확장 전에 Phase 1 Agent 작업을 검토·검증하고 안전한 PR은 병합하도록 요청했습니다. 이후 BE 담당자가 계속 개발 중이므로 **BE 파일·브랜치·PR을 변경하지 말 것**을 다시 확인했습니다. 이 작업은 Agent 저장소와 격리된 시험용 PostgreSQL에서만 진행했습니다. 실행 중인 BE/FE/Agent 스택은 재배포하지 않았습니다.

최초 고정점은 Agent main `60df922127f99214e7b39822ba06a8383a083586`, Worker #66 head `8f15db6a22719ecbf9e81223530ade054329e935`였습니다. 충돌한 `.env.example`·API 구성·Inbox를 별도 브랜치에서 통합했습니다. 작업 중 작성자가 #66에 main을 반영한 `465f57c7f58bf6742b2a97aeed431be6d331e493`을 올렸고, 이 커밋도 보존했습니다. 이후 #66은 작성자 iihlolo가 `a0c84a129f73c9956d011da05d42ee592c632e8d`로 병합한 사실을 확인했습니다.

최종 후속 PR은 **이미 병합된 #66의 동작을 유지**하며, 주석·설명과 추가 인수 시험·증거를 제공합니다. 모델/BE 업무 응답을 실제 서비스 성공으로 표시하지 않습니다. Phase 2 조사 API 회귀도 전체 검사에 포함됩니다.

## 확인한 실행

증거: [evidence/phase1-worker-68.json](../evidence/phase1-worker-68.json).

| 시험 | 결과와 의미 |
|---|---|
| 전체 회귀 | 실제 PostgreSQL/pgvector, TCP 활성화 **344 tests PASS, skip 0** |
| 경쟁 선점 | 여러 연결/Worker가 같은 작업을 경쟁해 한 lease만 실행 |
| heartbeat·만료 | 긴 모델 대기 중 lease 갱신, 이전 소유자의 결과·checkpoint·후속 변경 차단 |
| 승인 대기 복원 | 새 프로세스가 같은 PostgreSQL checkpoint를 읽어 동일 thread의 CONTEXT/KICK 대기를 이어감 |
| AI·사람 근거 | 운영자 카드의 reason·policyIds·evidenceMessageIds·caseId 연결 확인. AI 정상 제안과 사람 위반 결정은 별도 보존 |
| 별도 강퇴 승인 | 프로세스별로 조사→문맥 위반 결정→KICK 승인 순서. KICK 전 호출 없음, 승인 후 같은 actionId/idempotencyKey로 1회 |
| 중복 전달 | 같은 KICK 결정 중복 접수 뒤 새 프로세스에서도 추가 강퇴 호출 없음 |
| 강퇴 거절 | 새 프로세스에서 REJECTED 확인, kick 호출 0회 |
| 보류·종류 오류 | DEFER/잘못된 reviewKind 뒤 WAITING_REVIEW 유지, 재조사/문맥 적용 없음 |
| 잘못된 방송 | 다른 liveId의 결정은 FAILED, apply/kick 없음 |
| 실제 프로세스 강제 종료 | 검토 생성 호출 중 SIGKILL 후 새 프로세스는 BE 상태 조회만 수행, create 재전송 없음, UNKNOWN 유지 |
| 종료·취소 | shutdown timeout 후 close 2회 안전, close 호출자의 취소는 삼키지 않음 |
| 기존 BE 조회 | 새 Gateway.read_case가 기존 배포 BE와 연결되어 case/snapshot/evidence/policy 참조 반환. GET만 수행 |

**별도 프로세스 시험의 모델/정책/업무 Gateway는 통제된 fixture입니다.** 실제 PostgreSQL 내 Agent 스키마와 실제 OS 프로세스 종료/재기동을 사용합니다. 승인 대기 복원과 단일 호출은 이 범위의 증거이며 실제 BE의 멱등 강퇴, 실제 모델 의미 정확도, 새 FE 화면을 증명하지 않습니다. 실제 BE 조회는 기존 배포의 읽기 호환성 증거이며 최신 BE main 배포 검증이 아닙니다.

실행:

```bash
AGENT_TEST_CHECKPOINT_DB_URL="$TEST_DATABASE_URL" \
AGENT_TEST_INBOX_DB_URL="$TEST_DATABASE_URL" \
RAG_TEST_DB_URL="$TEST_DATABASE_URL" \
LIVEGUARD_TEST_TCP=1 \
uv run --locked python -m unittest discover -s tests -v
```

각 테스트는 UUID별 Agent schema를 생성·삭제합니다. 테스트 전용 DB를 사용하고 운영 DB를 전달하지 않습니다. 토큰/DSN/실제 원문을 시험 로그나 증거에 보관하지 않습니다.

## Standards

고정된 #66 diff에 대한 독립 검토에서 차단할 문서 기준 위반을 찾지 못했습니다. 식별자 SQL은 Identifier를 사용하고, lease 갱신/선점은 짧은 트랜잭션이며 외부 I/O는 밖에서 실행됩니다. 체크포인트 쓰기와 소유권 행 잠금이 같은 트랜잭션이고, shutdown은 취소 전 guard를 폐기합니다. 오류는 고정 코드로 제한합니다.

비차단 유지보수 의견 2개는 남깁니다. Worker가 recovery의 `_bound`/`_waiting_is_bound`를 공유하고, 구성 코드가 `saver._cursor`/`inbox._jobs`로 동일 DB를 검증합니다. 향후 공개된 작은 검증 인터페이스로 정리할 수 있지만 이번 동작을 바꾸거나 병합을 막을 근거로 보지는 않았습니다.

## Spec

#60의 원자적 claim·lease·heartbeat·오래된 소유자 차단·시작 여부별 복구·불확실 변경의 BE 조회·backoff·graceful shutdown 요구에서 차단할 누락을 찾지 못했습니다. 기존 승인과 BE 발급 action identity를 보존하고 Rabbit 직접 소비나 exactly-once를 추가하지 않습니다.

명세상 남은 실제 외부 검증은 **최신 BE가 같은 actionId/idempotencyKey의 변경을 한 번만 적용하는지**입니다. 현재 프로세스 시험은 명령 보존/재호출 방지를 입증하고 실제 BE 중복 집행 방지 자체를 대신하지 않습니다. BE가 lease를 검증하지 않는 전송 경계의 경쟁도 기존 문서대로 보장 한계입니다.

축별 결론: Standards 차단 0·비차단 2, Spec 차단 0·실제 BE 추가 검증 1. 테스트 통과를 전체 운영 완료율로 환산하지 않습니다.

## 배포 전 Agent 확인

1. 기존 `app.main`은 AGENT_INBOX_DB_URL 설정 시 API-only입니다. 202는 영속 접수이지 실행 완료가 아닙니다. Worker는 별도 `python -m app.worker` 프로세스로 실행합니다.
2. Inbox/checkpoint는 **같은 PostgreSQL 서버의 같은 database**, 서로 다른 Agent schema를 사용해야 합니다. 기존 SQLite 작업은 자동 이관하지 않습니다. 기존 대기 작업의 보관·이관을 합의해야 합니다.
3. 모델·Business/RAG·Gateway 환경은 Worker에, 수신 토큰은 API에 설정합니다. 준비 실패를 우회하지 않습니다. `WORKER_READY`는 의존 서비스의 실제 업무 처리 성공을 증명하지 않습니다.
4. UNKNOWN을 QUEUED로 바꾸거나 초기 작업/승인 결정을 재전송하지 않습니다. BE 사건/조치/결정 상태를 대조한 후 운영자가 처리해야 합니다.
5. 실제 모델·BE·FE 재시험은 아래 공동 시나리오로 진행합니다. 현재 실행 중 스택의 설정이나 업무 데이터를 이 작업에서 변경하지 않았습니다.

## 공동 화면 시험안 · 아직 미실행

| 시나리오 | 화면/BE에서 확인할 결과 |
|---|---|
| 정상 메시지 | 다른 시청자에게 보임, 제재 없음 |
| 문맥 후보 | HELD, 작성자/담당 운영자 외 원문 비노출, 조사 근거 카드 |
| 문맥 정상·위반·보류 | 사람 결정에 따라 공개/위반확정/계속 대기. AI 제안과 결정 이력 구분 |
| 확정 3사건 미만/이상 | BE 집계 기준대로 KICK 심사 유무, 한 사건 다중유형 중복 가산 없음 |
| KICK 승인/거절 | 별도 사람 결정·BE 재검증 후 승인만 방송 종료까지 차단. 거절은 강퇴 없음 |
| 재시작·결정 중복 | 대기 카드 복원, 같은 action 중복 집행 없음 |
| 응답 유실 | UNKNOWN·조치 상태 확인, 성공 알림으로 오인하지 않음 |
| finding 취소 | BE 복구 규칙과 SSE에 따라 실제 상태·전송 가능 여부 회복 |
| 강퇴 당사자 안내 | 본인에게 허용된 사유·근거 사건·제한 범위 안내, 타인 원문/PII/운영자 전용 메모 미노출 |

시청자 상세 강퇴 사유 조회/화면은 BE·FE 담당 협의 사항이며 Agent가 임의 구현하지 않았습니다. 공동 최신 배포·전체 화면 재검증·종료 커밋/`phase1-end` 합의는 남겨 둡니다. 변형 욕설/욕설 없는 혐오 탐지 품질 개선은 Phase 2 범위로 유지합니다.
