const input = document.getElementById('message');
const submit = document.getElementById('submit');
const notice = document.getElementById('notice');
const count = document.getElementById('count');
let liveId = crypto.randomUUID();
let history = [];

function write(id, value) { document.getElementById(id).textContent = value; }
function put(parent, className, text) {
  const element = document.createElement('div');
  element.className = className;
  element.textContent = text;
  parent.append(element);
}
function verdict(id, selected, okText = '후보 아님') {
  const node = document.getElementById(id);
  node.textContent = selected ? '검토 후보' : okText;
  node.className = selected ? 'flag' : 'clear';
}
function render(data) {
  const gpt = data.gpt;
  const local = data.unsmile;
  const signals = document.getElementById('gpt-signals');
  const scores = document.getElementById('unsmile-scores');
  signals.replaceChildren(); scores.replaceChildren();
  if (gpt.status === 'OK') {
    verdict('gpt-verdict', gpt.selected);
    write('gpt-time', `${gpt.elapsedMs} ms`);
    write('gpt-detail', gpt.selected ? '표현과 대화 문맥에 근거한 후보 신호입니다. 최종 판단은 사람에게 있습니다.' : '현재 실험 기준에서 후보 신호가 선택되지 않았습니다.');
    for (const signal of gpt.signals) put(signals, 'signal', `${signal.kind} · ${signal.strength} — ${signal.reason}`);
  } else {
    write('gpt-verdict', gpt.status === 'NOT_CONFIGURED' ? '연결 안 됨' : '판독 실패');
    write('gpt-time', gpt.elapsedMs === undefined ? '—' : `${gpt.elapsedMs} ms`);
    write('gpt-detail', 'GPT 설정 또는 응답을 확인해 주세요. 다른 모델의 결과는 별도로 표시됩니다.');
  }
  if (local.status === 'OK') {
    verdict('unsmile-verdict', local.selected, '임계값 미달');
    write('unsmile-time', `${local.elapsedMs} ms`);
    write('unsmile-detail', `대상 문장만 검사 · 임계값 ${local.threshold} · 해당 라벨 ${local.matchedLabels.join(', ') || '없음'}`);
    for (const [label, score] of Object.entries(local.scores)) put(scores, 'score', `${label}    ${score.toFixed(4)}`);
  } else {
    write('unsmile-verdict', local.status === 'NOT_CONFIGURED' ? '연결 안 됨' : '판독 실패');
    write('unsmile-time', local.elapsedMs === undefined ? '—' : `${local.elapsedMs} ms`);
    write('unsmile-detail', '신뢰할 수 있는 로컬 .pkl과 토크나이저 설정을 확인해 주세요.');
  }
}

async function compare() {
  const text = input.value.trim();
  if (!text || text.length > 500 || submit.disabled) return;
  submit.disabled = true;
  notice.textContent = '두 모델의 응답을 기다리는 중입니다…';
  const target = { messageId: crypto.randomUUID(), text };
  try {
    const response = await fetch('/api/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requestId: crypto.randomUUID(), experiment: 'clear-or-possible',
                             input: { liveId, target, context: history.slice(-8) } }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.code || 'REQUEST_FAILED');
    render(data);
    if (data.gpt.status === 'OK' || data.unsmile.status === 'OK') history.push(target);
    notice.textContent = '동일한 대상 문장으로 비교했습니다. 후보 신호는 운영자 판단 전 단계입니다.';
  } catch (error) {
    notice.textContent = `비교 요청 실패: ${error instanceof Error ? error.message : 'REQUEST_FAILED'}`;
  } finally {
    submit.disabled = false;
  }
}
submit.addEventListener('click', compare);
input.addEventListener('input', () => { count.textContent = `${input.value.length} / 500`; });
input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); compare(); }
});
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => {
  input.value = button.dataset.example;
  count.textContent = `${input.value.length} / 500`;
  input.focus();
}));
fetch('/api/compare/health').then(response => response.json()).then(state => {
  notice.textContent = `GPT ${state.gptConfigured ? '연결됨' : '미설정'} · UnSmile ${state.unsmileConfigured ? '연결됨' : '미설정'}`;
}).catch(() => { notice.textContent = '모델 연결 상태를 확인할 수 없습니다.'; });
