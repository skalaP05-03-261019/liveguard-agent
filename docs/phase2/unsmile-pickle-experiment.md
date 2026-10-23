# UnSmile `.pkl` 비교 실험

이 실험은 Agent 전용 로컬 화면에서 **동일한 대상 채팅**을 기존 GPT 후보 탐지와 UnSmile 다중 라벨 분류에 넣어 결과를 나란히 보여준다. BE 사건 생성·채팅 가림·운영자 승인·제재와 연결하지 않는다.

## 모델 준비

제공된 `Smilegate_AI_UnSmile_dataset_fine_tuning_tutorial_ipynb의_사본.ipynb`의 43번 셀은 학습된 PyTorch 모델을 `unsmile_model.pkl`로 저장하는 **코드**다. 노트북 파일에는 `.pkl` 가중치가 들어 있지 않다. 46번 셀의 서빙 예제는 `beomi/kcbert-base` 토크나이저, 10개 라벨, sigmoid 점수를 사용한다. 자신의 Colab에서 학습·내보낸 파일과 동일 토크나이저를 로컬에 준비해야 그 학습 결과를 비교할 수 있다. 다른 공개 체크포인트로 `.pkl`을 만들면 별도 모델로 표시해야 한다.

```bash
uv sync
uv pip install --python .venv/bin/python -r requirements-unsmile-experiment.txt
uv run --no-sync python -m app.candidate_detection.compare_web \
  --unsmile-pkl /absolute/path/unsmile_model.pkl \
  --tokenizer beomi/kcbert-base --trust-local-pickle \
  --env-file /absolute/path/.env --model YOUR_EXISTING_GPT_MODEL
```

브라우저에서 `http://127.0.0.1:8766/compare`를 연다. GPT 없이 로컬 모델만 확인할 때는 `--env-file`과 `--model`을 함께 생략한다. `.pkl`은 코드를 실행할 수 있으므로 직접 만든 신뢰 가능한 파일만 명시적으로 로드한다. 모델 파일·키·원문 입력은 Git에 올리지 않는다.

## 비교 해석

- GPT: 대상과 앞선 최대 8개 메시지를 보고 후보 유형·강도·인용 근거를 제안한다.
- UnSmile: 대상 문장 하나에서 10개 다중 라벨 점수를 낸다. `clean`은 별도 라벨이고, 나머지 라벨 중 임계값 이상을 실험상 후보로 표시한다. 단어 위치·문맥 근거·서비스 정책 위반 확정은 없다.
- 같은 문장과 같은 순서로 응답 시간과 후보 여부를 비교할 수 있다. 입력 정보가 다르므로 정확도 우열로 해석하지 않는다. 정확도 평가는 정책·QA가 검수한 독립 확인셋이 필요하다.
- 두 결과가 다르면 이유와 유형을 사람이 확인한다. 어느 결과도 자동 가림이나 강퇴로 전달하지 않는다.

현재 리포지토리와 로컬 다운로드 폴더에서 `unsmile_model.pkl`을 찾지 못했다. 따라서 이 문서만으로 실제 `.pkl` 비교 수치를 주장하지 않는다.
