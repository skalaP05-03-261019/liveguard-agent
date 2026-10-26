# 직접 채팅하며 보는 Agent 후보 판독 데모 (#83)

사용자가 시청자 채팅을 입력하면 실제 Agent의 `CandidateScreen`이 현재 문장과 앞선 채팅 최대8개를 읽고 **검토 후보 여부·신호 유형·강도·인용·이유**를 화면에 반환한다. 정책 위반 판정이나 강퇴는 수행하지 않는다. 기본은 Agent 단독 모드이며, 선택적으로 로컬 BE의 실제 채팅 전송 결과를 나란히 볼 수 있다.

## 바로 열기

현재 작업 컴퓨터에서 Docker 컨테이너 `liveguard-candidate-demo`가 실행 중이면 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)로 접속한다. 예시 버튼은 입력만 채운다. 직접 입력 후 **보내고 판독**을 누르면 오른쪽에 실제 모델 근거가 뜬다. Enter로 전송, Shift+Enter로 줄바꿈한다. **대화 초기화**는 이전 채팅 문맥을 비운다.

다운로드 `.env`에서 모델 키 필드만 읽는다. 브라우저에 키를 보내거나 `.env` 전체를 앱 설정으로 로드하지 않는다. 다른 포트에서 재시작하려면 최신 Agent 코드에서 아래처럼 실행한다.

```bash
python -m app.candidate_detection.demo_web \
  --env-file /Users/taegwankim/Downloads/.env \
  --model gpt-5.4-mini --port 8765
```

로컬 Python 프로세스 사용 시 서버는 `127.0.0.1`에 바인딩된다. Docker에서는 컨테이너 내부에서 듣게 하고 **호스트의 127.0.0.1에만** 포트를 공개한다.

```bash
docker run --rm --name liveguard-candidate-demo \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD:/work" \
  -v /Users/taegwankim/Downloads/.env:/run/liveguard-model.env:ro \
  -w /work liveguard-unified-agent \
  python -m app.candidate_detection.demo_web \
  --env-file /run/liveguard-model.env --model gpt-5.4-mini --port 8765 \
  --backend-url http://host.docker.internal:8080 --container
```

Docker 명령은 repo 루트에서 실행한다. 사용 후 `docker stop liveguard-candidate-demo`로 종료한다. 같은 로컬 포트를 다른 앱이 사용하면 다른 `--port`와 Docker 양쪽 포트를 맞춘다. 키가 유효하지 않으면 화면에 오류 코드만 보이며 원문/모델 응답의 예외 내용은 외부 응답으로 내보내지 않는다.

## 모델·화면 동작

브라우저의 현재 대화는 임시 메모리 상태다. 새로고침하면 대화가 비워진다. 입력은 최대500자이고 모델 호출 중 추가 전송은 막는다. 모델 출력은 target 원문과 ID에 맞는 인용인지 Agent가 검증한다. 로컬 데모는 `include-uncertain` 실험을 사용해 유효한 신호가 하나라도 있으면 “검토 후보”, 신호가 없으면 “정상 · 후보 신호 없음”으로 표시한다. `UNCERTAIN` 신호는 부족한 사실과 함께 후보에 남긴다. 이것은 고객사 정책 프로필이나 위반 확정이 아니다. 모델·형식·연결 오류는 화면에서 “검토 후보 · AI 판독 불가”로 안전 분기하고, 검증된 Agent 후보 신호와 구분해 오류 코드를 표시한다. 오류는 정상 판정이나 위반 확정이 아니며 API의 오류 응답은 유지한다. 정상 채팅도 대화 문맥에는 남는다.

브라우저와 Agent는 같은 출처 HTTP로 통신한다. 로컬 Host·Origin·JSON형식·16KiB body 제한, 동시 처리 상한, 캐시 방지·CSP·원문을 담지 않는 입력/의존성 오류 응답을 적용한다. 채팅·근거는 화면에 `textContent`로 표시하고 서버 파일에 보관하지 않는다. Agent 단독 모드에서 판독이 실패한 채팅은 다음 문맥에서 제외한다. BE 통합 모드에서는 BE에 실제 저장된 채팅을 다음 문맥에 남긴다. 시연 페이지를 외부에 공개하도록 설계하지 않았다.

## 실제 확인

다운로드 `.env`의 키, Agent main `96a83dc406f9da91a5ecdeb5f68a986c9f7fef77` 기반 코드에서 브라우저 입력을 실모델로 검증했다.

1. 정상 표현 입력 → “특이 신호 없음”.
2. 욕설 변형 입력 → “검토 후보”, 욕설 변형·개인 공격 신호와 모델 이유/원문 인용.
3. 두 번째 요청에 첫 번째 채팅이 문맥으로 포함되는 것을 브라우저 네트워크 요청에서 확인.

Playwright 실제 브라우저 실행에서 페이지 오류0. 데스크톱·모바일 화면을 캡처해 레이아웃을 확인했다. 합성 ASGI 검사8개는 Host/Origin·본문 크기·입력 오류·모델 오류 누출·문맥 전달·정적 화면을 확인한다. 전체 회귀 382개가 격리 PostgreSQL·ASGI/TCP에서 성공했으며 skip은 0개다. GitHub CI 결과는 PR에서 확인한다.

BE 연결을 지정하지 않은 화면은 Agent 후보 판별만 호출한다. 검사 경로 표시는 “BE 미연결 · Agent 직접 검사”이며, BE가 실제로 걸러 낸 결과를 뜻하지 않는다. BE 채팅 저장, 사건 생성, 운영자 승인, 집행, FE 서비스 화면은 이 단독 모드의 결과가 아니다. 기존 팀 정책·승인 흐름은 그대로이며, 새 혐오/비하 정책 합의와 고객사 민감도 설정도 이 데모로 확정하지 않는다.

## 선택형 BE 연결

로컬 Python 실행에서 `--backend-url http://127.0.0.1:8080`을 지정하면 데모에 실제 BE 시청자 로그인·진행 중 방송 선택이 열린다. Docker 데모에서는 `http://host.docker.internal:8080`을 사용한다. URL은 로컬 호스트로 제한한다. 브라우저의 세션 쿠키와 CSRF 토큰을 같은 출처의 제한된 프록시가 BE의 로그인·계정·현재 방송·채팅 전송 경로에만 전달한다. 계정 비밀번호나 서비스 토큰을 데모 코드에 저장하지 않는다.

통합 모드의 순서는 **BE에 실제 채팅 전송 → BE 응답의 `visibility`·`moderationReasonCode` 표시 → 같은 메시지 ID로 Agent 후보 판독을 별도 실행**이다. 화면에서 BE 공개·보류·숨김과 Agent 후보 신호를 독립적으로 보여 준다. Agent 후보 판독은 BE 사건·운영자 결정 API에 제출하지 않으므로 실제 집행 근거가 아니다. BE가 전송을 거부하면 Agent 비교를 실행하지 않는다. Agent가 실패하면 BE 처리 상태를 유지하고 Agent 결과만 판독 불가로 표시한다. 현재 데모는 Agent Worker의 사건 조사 결과나 운영자 승인 결과를 읽지 않는다.
