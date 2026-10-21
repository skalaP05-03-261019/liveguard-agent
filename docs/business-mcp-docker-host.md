# Business MCP Docker Host 421 대응 (#44)

`mcp.streamable_http_app(transport_security=TransportSecuritySettings(...))` 적용은 이미 PR #32 / main `15d816250d5c669c8ef43b501d9ed41c38c0d3d8`로 병합됐다. 구 이미지에는 이 수정이 없을 수 있다. 이후에도 기본값/기존 .env가 loopback만 허용하면 `Host: business-mcp:8002`가 421로 거부된다.

이번 수정은 기본값과 `.env.example`을 `business-mcp:*,localhost:*,127.0.0.1:*`로 맞춘다. DNS rebinding 검사는 계속 활성화하고 토큰 인증도 유지한다. 명시한 환경변수는 기본값을 **대체**하며 빈 값/전역 `*`는 설정 오류로 처리한다. 다른 compose 서비스 이름은 환경변수에 정확히 추가한다.

Infra compose의 **business-mcp 서비스 컨테이너 환경**에 다음을 전달한다. Agent 컨테이너나 호스트 `.env`에만 적으면 MCP 서버 설정이 바뀌지 않을 수 있다.

```yaml
business-mcp:
  environment:
    BUSINESS_MCP_HOST: "0.0.0.0"
    BUSINESS_MCP_PORT: "8002"
    BUSINESS_MCP_ALLOWED_HOSTS: "business-mcp:*,localhost:*,127.0.0.1:*"
```

현재 작업공간 Infra `compose.full.yaml`에는 이미 이 환경변수가 포함돼 있다. Infra 팀의 실제 실행 compose에도 전달되는지 확인하고 새 소스로 **재빌드·컨테이너 재생성**한다. 단순 restart는 이미지 코드/환경을 갱신하지 않는다. compose 파일명·project명은 실제 환경에 맞춘다.

```bash
docker compose -f compose.yaml -f compose.full.yaml up -d --build --no-deps business-mcp
```

확인 순서:

1. 컨테이너 내부 `Settings.from_env().allowed_hosts`에 세 Host가 있는지 확인(토큰·전체 환경 출력 금지).
2. Agent에서 `http://business-mcp:8002/mcp`로 실제 MCP Tool 호출. initialize뿐 아니라 tool 응답까지 확인.
3. 미인증은 401, 허용하지 않은 Host는 421을 유지하는지 확인.
4. Tool에서 BE `CASE_NOT_READY`/`NOT_IMPLEMENTED`가 돌아오면 Host 통신과 BE 업무 준비 상태를 구분한다. 421 해결이 전체 Agent 승인 E2E 성공을 뜻하지 않는다.

테스트는 실제 MCP SDK와 ASGI/TCP transport에서 Docker Host 헤더의 initialize·Tool 호출, 외부 Host 거부, 잘못된 토큰 거부, 환경변수 대체를 검사한다. Backend 응답은 통제 fixture이며 Docker DNS/실제 BE 연결은 Infra가 컨테이너 재빌드 후 확인한다. 이 환경에서는 Docker socket 접근이 거부돼 재빌드 완료를 주장하지 않는다.

검증 완료: 코드 `8fbed45`, CI run `39533908663`에서 실제 Postgres 포함 ASGI 196개(12.199초), TCP 196개(21.495초), skip 0 모두 통과. 상세 근거는 `evidence/business-mcp-host-2026-10-21.json`.
