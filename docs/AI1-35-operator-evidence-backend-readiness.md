# 운영자 근거 전달·최신 BE 연동 진단 (#35)

2026-10-21. 기존 Agent PR #27·#29·#32·#34를 사용자 요청으로 병합한 main `8b8e49c` 이후 변경입니다. 최신 BE 기준은 사건 영속화 PR #18의 main `184f2a3`입니다.

## 운영자 카드

기존 공통 CONTEXT/KICK 카드가 `MessageFinding.evidenceMessageIds`와 `missingFacts`를 누락해 운영자에게 AI 판단 근거와 미확인 사항을 충분히 전달하지 못했습니다. 이제 두 필드를 보존하며 정책 조항의 `text/version/category`와 기존 Resource URI를 함께 제공합니다. 공통 카드에 snapshotId/evidenceVersion과 `decisionAuthority=OPERATOR`도 넣습니다.

대상·문맥 메시지는 표시용 필드만 선택하고, 전체 카드를 기존 `safe_presentation`에 통과시켜 개인정보 마스킹·문자열 길이 제한을 적용합니다. 정책 표시 문자열도 이 경계를 거치므로 원문 확인에는 `resourceUri`를 사용합니다. 원래 State의 AI 제안과 근거는 변경하지 않습니다. 명령·토큰·도구 내부 데이터는 카드에 추가하지 않습니다.

CONTEXT는 AI가 정상·위반·보류 중 어느 판단을 해도 기존 interrupt에서 기다립니다. KICK도 원래 별도 승인·재검증 경로를 유지합니다. 카드 보강이 BE 검토 API나 FE 카드 렌더링 구현을 뜻하지는 않습니다.

## 진단 실행

```bash
uv run --locked --env-file .env python -m app.check_backend_connection \
  --case-id <실제_영속_사건_UUID> --output evidence/backend-readiness.json --require-ready
```

`BUSINESS_BACKEND_BASE_URL`, `BUSINESS_MCP_SERVICE_TOKEN`, `BUSINESS_MCP_URL`, `BUSINESS_MCP_AGENT_TOKEN`을 사용합니다. 실제 `.env`는 자동 생성/수정하지 않으며 키·원문을 출력하지 않습니다.

- HTTP의 case/context/channel-info/user-history/security-facts/kick-eligibility를 각각 조회합니다.
- MCP의 사건·문맥·방송·공식 링크·사실·이력도 각각 확인합니다. 한 조회의 501/도구 오류가 다른 조회 증거를 지우지 않습니다.
- `documentedB2Routes`는 Swagger에 경로가 있다는 뜻입니다. 기존 오해 가능한 `implementedB2Routes` 필드는 제거했습니다.
- HTTP/MCP별 상태와 오류 코드, 소비자 스키마 누락, auth 경계를 분리해 기록합니다. 반환된 원문·자격 증명은 기록하지 않습니다.
- 두 스키마가 유효해도 실제 `load_case_data`로 사건/스냅샷/증거 버전/대상 바인딩이 통과해야 `readContractReady=true`입니다. 서비스 인증 차단도 확인해야 합니다.
- `READ_CONTRACT_READY`는 사건 로더가 준비됐다는 뜻이며, 다른 조회/승인/변이 경로의 `blockers`가 남을 수 있습니다. 전체 관제 준비 완료가 아닙니다.
- `--require-ready`를 쓰면 PARTIAL도 종료 코드 2입니다. 기본값은 기존과 같이 BLOCKED일 때만 2입니다. 진행 이벤트 쓰기는 `--publish-test-event`를 명시한 경우만 가능합니다.

## 실제 최신 BE 관찰

로컬 기존 DB의 V200 적용은 오래된 메모리 case_id 외래키 때문에 실패했습니다. BE #20의 명시적 로컬 유지보수 절차로 전체 백업·복제 검증 후 이전 ID 연결을 별도 보존하고 참조만 분리했습니다. 기존 채팅 27건의 원문·visibility·version 등 나머지 컬럼 해시는 전후 동일하고, 임시 연결 18개를 보존했습니다. V200을 수정하거나 기존 메시지를 확정 위반으로 변환하지 않았습니다.

그다음 실제 브라우저용 API로 별도의 검증 메시지를 보냈습니다. 새 `JdbcCandidateSink`가 실제 caseId/snapshotId/evidenceVersion과 후보를 저장하고 findings는 0건인 것을 읽기 쿼리로 확인했습니다. 관리용 검증 쿼리이며 Agent 업무 실행 코드가 DB를 직접 조회/변경하도록 추가한 것은 아닙니다.

실제 HTTP/MCP 결과는 [영속 사건 연동 증거](../evidence/backend-persistence-2026-10-21.json)에 있습니다.

| 경로 | 관찰 |
| --- | --- |
| 무토큰·Bearer만으로 BE 내부 조회 | 403 |
| 서비스 토큰으로 context/channel-info | 200 |
| MCP chat_context/stream_info/official_links | 연결 성공 |
| case/user-history/security-facts/kick-eligibility | HTTP 501 |
| MCP moderation_case/user_history/security_facts | NOT_IMPLEMENTED, retryable=false |
| context 소비자 차이 | caseId/snapshotId/evidenceVersion 및 메시지 visibility 누락 |

BE의 InternalCase는 `targets[]` 형태이며 Agent 로더의 `targetMessageIds/targetVersions`와도 다릅니다. BE 사건 API 구현 시 이 어댑터 차이도 정렬해야 합니다. liveId/userId/handlingRoute/policyVersion을 DB 빈 값이나 channelId로 추정하지 않습니다. 전체 HTTP ModerationGateway·BE dispatch·사람 결정/재개 경로는 아직 미연결입니다. 따라서 현재 배포의 Agent readyForTasks=false를 유지합니다.

## 검증

최종 소스 확인 중 BE 정책 원본 정리 PR #19(main `3a33ee8`)도 병합됐습니다. 런타임 변경은 없고, `docs/policy/`를 기존 v0.5 설명보다 우선하는 것으로 정리했습니다. 필수 문맥 사람 결정·liveId·방송 종료 기반 강퇴는 Agent의 현재 정책 방향과 일치합니다. 단, TBD 결정안은 확정 전 fixture 원칙과 10/22 10:00 이견 기한을 적고 있으며, TBD-08의 활성 `v3.0` 제안은 Agent의 과거 `policy-v3.0.md` 내용과 다릅니다. 현재 `policy-v3.1.md`가 최신 의미를 반영하므로 문자열만 바꾸거나 과거 정책 파일을 덮지 않았습니다. 새 활성 ID/색인/BE 사건 버전은 별도 정렬해야 합니다. 앞의 실행 증거는 런타임 기준 `184f2a3`이며 문서 추가 커밋을 실제 실행한 것으로 표시하지 않습니다.

실제 격리 Postgres/pgvector를 사용한 전체 회귀: ASGI 199개(5.543초), TCP 199개(16.322초), 실패·오류·skip 0. 승인 대기 Graph를 통해 사라졌던 근거·미확인 사실 보존, PII 마스킹, 원 State 불변, typed finding, 승인 전 미실행을 확인했습니다. 진단은 501 뒤 조회 계속, 문서/구현 분리, 스키마 통과 후 stale binding 거부, auth 누락 거부, 직접 API 501 은폐 거부를 시험했습니다. 모델은 통제된 fixture이며 실제 모델 호출/제재는 0입니다.
