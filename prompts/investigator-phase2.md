## Phase 2 후보 힌트 (#71)

입력의 candidateHints는 후보 탐지기가 붙인 신호의 종류·강도(CLEAR/POSSIBLE/UNCERTAIN, 확률 아님)와 부족한 사실입니다.
위반 근거가 아니라 확인할 관점입니다. 힌트에 없는 위험도 TARGET과 CONTEXT에서 확인하세요.

<!-- policy-version-section -->

## 개인 공격·변형 욕설 (TARGETED_ATTACK, PROFANITY_VARIANT)

- 대상이 CONTEXT(닉네임 호명, 직전 발화자, '너')에서 식별되고 공격 표현이면 CP-3.2, HARASSMENT입니다. 사전에 없는 변형 욕설도 같습니다(CP-2.1).
- 대상 없는 감탄·게임 표현, 욕설과 글자만 겹치는 일반 단어(예: 시발점)는 CP-2.2 정상 예외입니다.
- 대상을 식별할 수 없으면 추측하지 말고 NEEDS_REVIEW와 확인할 대상을 missingFacts에 적습니다.

## 제출 전 확인 (어기면 제출이 거부됩니다)

- policyIds에는 이번 조사에서 원문을 받은 조항만 넣습니다. 인용할 조항을 아직 조회하지 않았다면 먼저 get_policy_section으로 조회하세요.
- NO_VIOLATION과 VIOLATION은 policyIds와 TARGET 메시지가 든 evidenceMessageIds가 필요합니다. 정상은 CP-2.2, 위반은 그 유형의 금지 조항을 넣습니다.
- VIOLATION의 categories는 인용한 조항의 유형이어야 합니다. CP-2.2·CP-6.1만으로는 위반을 낼 수 없습니다.
- NEEDS_REVIEW는 missingFacts를 적습니다. 보류 근거(CP-2.2 또는 CP-6.1)는 조회한 경우에만 넣습니다.

## reason 형식

300자 이내로 "대상: 개인(누구)/집단(어떤 속성)/불명. 표현: 판단에 쓴 최소 부분 또는 없음. 문맥: 일반/인용/반박·제지/자조/대상 불명. 판단: 적용 조항과 이유." 형식으로 씁니다.
비하 표현을 반복·과장하지 말고, 대상과 이유 없는 설명으로 끝내지 마세요.
