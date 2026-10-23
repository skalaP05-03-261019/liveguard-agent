# 공개 채팅 요약 입력 경계 — Agent #112

이 문서는 Agent #72 요약 모듈 앞에 놓을 **제안 내부 계약**이다. BE API 변경이나 고객사·시청자용 공개 API의 확정안이 아니다.

`PublicChatWindow`는 `liveId`, UTC-aware 반열린 구간 `[windowStart, windowEnd)`, `complete=true`, 최대 100개 `messages`, 선택적 `sourceVersion`을 받는다. 각 메시지는 `messageId`, 동일 `liveId`, `text`, `sentAt`, `visibility=PUBLIC`, 양수 `version`이 필수다. 입력에 HELD/HIDDEN, 다른 방송, 구간 밖 메시지, 중복 ID 또는 미완료 페이지가 있으면 묶음 전체를 거부한다. 빈 구간은 세원님 요약 모듈의 대화 부족 결과를 위해 허용한다. 메시지를 시간순으로 정렬하고 원문·버전·범위를 포함한 해시 `sourceFingerprint`를 계산해 정정 시 다른 입력으로 구별한다. 해시는 권한 증명이 아니다.

이 모듈은 **BE 조회, 인증, 호출자 권한 검증, 요약 모델 실행, 캐시 저장, FE 노출을 구현하지 않는다.** 현재 BE `GET /api/lives/{liveId}/messages`는 시청자에게 본인 HELD/HIDDEN을, 담당 운영자에게 전체 메시지를 반환하며 시간창 조회 계약도 없다. 그 응답을 요약의 공개 입력으로 그대로 사용해서는 안 된다.

BE 담당자와 정할 항목: PUBLIC만 반환하는 인증된 조회 경로, 시간창·페이지네이션의 일관성, `complete`와 `sourceVersion`의 근거, 최대 건수 초과/방송 종료 처리, 숨김·정정 이벤트의 전달 방법. 세원님과 정할 항목: 최종 요약 입력 스키마와 `sourceFingerprint`·메시지 버전 인계. 그때까지 #72의 모델·평가 구현과 이 PR의 검증 경계를 독립적으로 유지한다.

BE와 논의할 최소 응답 예시는 아래와 같다. **경로와 필드는 미확정**이다. BE가 공개 범위·권한을 확인하고 모든 페이지를 모은 뒤에만 `complete=true`를 준다. 100건을 넘거나 중간 페이지가 빠졌으면 범위를 나누거나 오류를 반환하며, 일부만 담아 완료된 구간이라고 표시하지 않는다.

```json
{
  "liveId": "00000000-0000-4000-8000-000000000001",
  "windowStart": "2026-10-23T06:00:00Z",
  "windowEnd": "2026-10-23T06:05:00Z",
  "complete": true,
  "sourceVersion": "public-window-snapshot-1",
  "messages": [{
    "messageId": "00000000-0000-4000-8000-000000000003",
    "liveId": "00000000-0000-4000-8000-000000000001",
    "text": "오늘 방송 재미있어요",
    "sentAt": "2026-10-23T06:01:00Z",
    "visibility": "PUBLIC",
    "version": 1
  }]
}
```

요약을 내보내기 직전에는 BE의 최신 공개 상태/버전을 다시 확인하거나 숨김·정정 이벤트로 해당 `sourceFingerprint`를 무효화해야 한다. 해시만 저장하고 상태 확인을 하지 않으면, 뒤늦게 숨겨진 메시지의 내용이 오래된 요약에 남을 수 있다. 이 검증은 후속 BE·Agent 통합 작업이다.

세원님 PR #109의 `SummaryInput` 필드와 같은 이름으로 `summary_input_payload()`를 제공한다. `complete`는 입력 공급자에 대한 경계 검사에만 쓰고 요약 모듈에는 전달하지 않는다. #109는 모델 입력 한도 때문에 한글 약 3,000자를 넘는 구간에서 `MODEL_CONTEXT_LIMIT`가 날 수 있다. 100건 이내여도 길이 제한에 걸릴 수 있으므로 **호출자가 누락 없이 구간을 분할하는 계약**이 추가로 필요하다. 이 PR은 임의로 메시지를 잘라 성공 요약을 만들지 않는다.
