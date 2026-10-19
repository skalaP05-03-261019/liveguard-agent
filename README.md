# LIVE:GUARD Agent 골격

김태관 / ⑤ AI 1·Agent의 Python 서비스 시작 구조. 현재 범위는 **API·스키마·State·Graph 경계**다.
실제 모델 판단, MCP/Backend 연결, DB 저장, 제재, checkpoint/resume는 미구현이다.

## 실행

```bash
cd agent
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

`http://127.0.0.1:8001/docs`에서 입력 형식을 확인한다. `/health`는 `readyForTasks: false`다.
run/resume는 유효한 요청에도 **501**을 반환한다. 작업을 저장하지 않으므로 접수 완료 202를 반환하지 않는다.
현재 인증이 없으므로 로컬 개발 주소만 사용한다.

## 파일과 다음 작업

| 파일 | 역할 / 다음 작업 |
|---|---|
| app/schemas.py | camelCase JSON, 작업 입력·판단·후보 조치 검증 |
| app/state.py | 조사·실행 상태; Harness 담당과 저장 형식 합의 |
| app/graph.py | 초기 분석 단계·판단 분기; 각 노드는 아직 연결 필요 오류 발생 |
| app/nodes.py | 단계별 구현 위치. 첫 작업은 load_case와 investigate |
| app/clients/ports.py | Backend·조회 MCP·실행 MCP 인터페이스 초안 |
| prompts/investigator.md | 조사 규칙 초안; 아직 모델에 연결되지 않음 |
| tests/fixtures/D01.json | 기존 개발 입력의 D01. 정답은 포함하지 않음 |

Graph: load_case → investigate → validate_judgment → 정상 보고 / 검토 대기 / 계획 요청 → 실행 → 확인 → 보고.
검토 노드는 현재 실행 불가이며 실제 interrupt·결정 재검증·재개 분기는 후속 구현한다.

첫 목표는 D01에서 **실제 모델의 도구 선택 → mock 응답 조회 → 근거 검증 → 판단**이다.
fixture는 HTTP 요청 자체가 아니다. POST에는 taskEnvelope만 넣고 모델에는 선택한 도구 응답만 제공한다.
seedMetadata·seedMessages를 도구에서 얻은 사실처럼 모델에 넘기지 않는다.

## 기준·미확정

- 기준: main 문서 v0.3, 정책 0.3-draft, 로컬 Agent 연결 계약 v0.1. 출력·포트는 팀 검토용이다.
- 0.4-review의 5개 유형·이력 기반 최대 7일 규칙은 반영하지 않았다.
- P1~P6 중 출력 형식, task context, 전체 사건 증거, 이벤트, Harness 연결은 팀 합의 필요.
- 모델·MCP SDK 선택/버전과 환경변수 연결은 다음 작업이다. .env.example은 향후 설정 목록이다.
- 수업 PDF 실제 72쪽은 변경 전 승인을 요구하지만 기획서는 최초 자동 조치다.
  현재 실행 기능은 없으며, 구현 전 팀·강사 확인 결과에 맞춰 실행 직전 경계를 확정한다.
- PDF 74쪽 Agent 설계·구현: Graph/State, 모델 도구 선택·근거, 저장·재개·진행 이벤트 증거가 필요하다.
  현재 골격이나 import 확인을 해당 기능의 완료 증거로 사용하지 않는다.

실행 검증 범위는 evidence/scaffold-check.json에 기록한다.
