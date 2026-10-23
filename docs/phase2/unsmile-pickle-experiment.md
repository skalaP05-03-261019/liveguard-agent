# UnSmile `.pkl` 비교 실험

이 실험은 Agent 전용 로컬 화면에서 **동일한 대상 채팅**을 기존 GPT 후보 탐지와 UnSmile 다중 라벨 분류에 넣어 결과를 나란히 보여준다. BE 사건 생성·채팅 가림·운영자 승인·제재와 연결하지 않는다.

## 모델 준비

제공된 `Smilegate_AI_UnSmile_dataset_fine_tuning_tutorial_ipynb의_사본.ipynb`의 43번 셀은 PyTorch 모델을 `unsmile_model.pkl`로 저장하는 **코드**다. 노트북 파일 자체에는 가중치가 없다. 46번 셀의 서빙 예제는 토크나이저, 10개 라벨, sigmoid 점수를 사용한다. 저장한 모델과 같은 토크나이저를 로컬에 준비한다. 공개 체크포인트를 내보낸 파일은 별도 학습 결과가 아니라 **공개 UnSmile 모델**로 표시한다.

```bash
uv sync
uv pip install --python .venv/bin/python -r requirements-unsmile-experiment.txt
uv run --no-sync python -m app.candidate_detection.compare_web \
  --unsmile-pkl /absolute/path/unsmile_model.pkl \
  --tokenizer /absolute/path/unsmile_tokenizer --trust-local-pickle \
  --env-file /absolute/path/.env --model gpt-5.4-mini
```

브라우저에서 `http://127.0.0.1:8766/compare`를 연다. GPT 없이 로컬 모델만 확인할 때는 `--env-file`과 `--model`을 함께 생략한다. `.pkl`은 코드를 실행할 수 있으므로 직접 만든 신뢰 가능한 파일만 명시적으로 로드한다. 모델 파일·키·원문 입력은 Git에 올리지 않는다.

## 비교 해석

- GPT: 대상과 앞선 최대 8개 메시지를 보고 후보 유형·강도·인용 근거를 제안한다.
- UnSmile: 대상 문장 하나에서 10개 다중 라벨 점수를 낸다. `clean`은 별도 라벨이고, 나머지 라벨 중 임계값 이상을 실험상 후보로 표시한다. 단어 위치·문맥 근거·서비스 정책 위반 확정은 없다.
- 같은 문장과 같은 순서로 응답 시간과 후보 여부를 비교할 수 있다. 입력 정보가 다르므로 정확도 우열로 해석하지 않는다. 정확도 평가는 정책·QA가 검수한 독립 확인셋이 필요하다.
- 두 결과가 다르면 이유와 유형을 사람이 확인한다. 어느 결과도 자동 가림이나 강퇴로 전달하지 않는다.

## 로컬 실물 검증 · 2026-10-23

사용자가 Colab에서 내보낸 `unsmile_model.pkl`(435,805,720바이트, SHA-256 `d65e741a3a6a902f5573b204f5c27ea5caad1b83a46669d8271ac78ad1627a83`)과 `unsmile_tokenizer/`를 로컬에서 로드했다. 모델 설정의 원본은 `smilegate-ai/kor_unsmile`, 작업은 다중 라벨 분류였다. 별도 fine-tuning 산출물이라는 증거는 없다. 이 호스트에서 PyTorch 2.14.1·Transformers 4.57.6 조합으로 실제 추론을 확인했다. Transformers 5.14.1은 pickle이 참조하는 `BertSdpaSelfAttention` 클래스가 없어 로딩되지 않았다.

같은 대상 문장 8개를 비교 앱에 넣은 **개발용 smoke 결과**는 [`evidence/phase2/unsmile-local-probe-2026-10-23.json`](../../evidence/phase2/unsmile-local-probe-2026-10-23.json)에 있다. UnSmile은 8/8 추론됐고, 예를 들어 “씨바, 너는 이것도 못하냐”는 `악플/욕설` 0.899로 후보가 됐다. 반면 지역 비하 문장의 `지역` 점수는 0.2838로 기본 임계값 0.5 아래였고, “'한남'이라는 단어를 사용하지 말아 주세요.”는 `남성` 0.9049로 후보가 됐다. 정책·QA의 기대값 검수 전이므로 정확도·재현율 수치로 해석하지 않는다.

같은 실행에서 GPT 쪽은 이 환경의 모델 연결 실패(`MODEL_UNAVAILABLE`)로 8/8 결과가 없었다. 따라서 **두 모델 성능 비교는 미완료**다. 서버 포트 바인딩도 실행 환경에서 거부되어 실제 브라우저 화면은 열지 못했다. 팀원의 일반 로컬 환경에서 위 명령으로 `/compare`를 띄우고, 동일 사례의 GPT 응답·지연을 다시 기록해야 한다. 교수님 [참고자료](https://butter-tapir-b9e.notion.site/AI-2027-4015de72e5b781bfa226d1aa6eba8ac4)의 평가 구분에 맞춰 기능 검사, 개발 smoke, 팀 검수된 독립 확인셋 결과를 분리한다.
