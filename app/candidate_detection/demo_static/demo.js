const chatLog = document.getElementById('chat-log');
const analysis = document.getElementById('analysis-content');
const form = document.getElementById('composer');
const message = document.getElementById('message');
const send = document.getElementById('send');
const counter = document.getElementById('counter');
let liveId = crypto.randomUUID();
let history = [];
let busy = false;

const labelFor = {
  REGION_DISPARAGEMENT: '지역 비하',
  GROUP_HATE: '집단 혐오·배제',
  TARGETED_ATTACK: '개인 대상 공격',
  PROFANITY_VARIANT: '욕설 변형',
};
const errorFor = {
  CANDIDATE_TIMEOUT: 'AI 응답 시간이 초과됐습니다. 잠시 후 다시 보내 주세요.',
  CANDIDATE_BUSY: '다른 판독을 처리 중입니다. 잠시 후 다시 보내 주세요.',
  MODEL_RESPONSE_REJECTED: 'AI 근거 형식을 확인할 수 없습니다. 대화 초기화 후 다시 시도해 주세요.',
  MODEL_UNAVAILABLE: '모델 연결을 확인해 주세요.',
  VALIDATION_FAILED: '채팅 길이와 입력 형식을 확인해 주세요.',
};

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function updateConnection(connected) {
  const dot = document.getElementById('connection-dot');
  dot.classList.toggle('connected', connected);
  dot.classList.toggle('disconnected', !connected);
  document.getElementById('connection-text').textContent = connected ? '실제 모델 연결됨' : '모델 연결 확인 필요';
}

async function checkHealth() {
  try {
    const response = await fetch('/health', { cache: 'no-store' });
    const state = await response.json();
    updateConnection(response.ok && state.modelConfigured === true);
  } catch {
    updateConnection(false);
  }
}

function addChat(text) {
  document.getElementById('empty-chat')?.remove();
  const row = element('div', 'chat-entry');
  const avatar = element('span', 'avatar', '시');
  const wrap = element('div', 'bubble-wrap');
  wrap.append(element('p', 'bubble-meta', '시청자 · 방금'), element('div', 'bubble', text));
  const status = element('span', 'entry-status', 'AI 판독 중…');
  wrap.append(status);
  row.append(avatar, wrap);
  chatLog.append(row);
  chatLog.scrollTop = chatLog.scrollHeight;
  return { row, status };
}

function showThinking() {
  analysis.replaceChildren(element('div', 'thinking', '채팅의 표현과 문맥을 확인하고 있습니다…'));
}

function showError(code) {
  analysis.replaceChildren(element('div', 'error-box', errorFor[code] || '판독에 실패했습니다. 모델 연결을 확인하고 다시 보내 주세요.'));
}

function showResult(data, elapsedMs) {
  analysis.replaceChildren();
  const head = element('div', 'result-head');
  head.append(element('span', 'result-title', '방금 보낸 채팅의 판독'),
              element('span', 'elapsed', `${(elapsedMs / 1000).toFixed(2)}초`));
  analysis.append(head);
  const signals = data.assessment.signals || [];
  const hasSignals = signals.length > 0;
  const verdict = element('div', `verdict ${data.selected ? 'flagged' : 'clear'}`);
  verdict.append(element('small', 'verdict-caption', '현재 데모 기준 · 신호 있으면 검토'),
                 element('strong', '', data.selected ? '검토 후보' : '정상 · 후보 신호 없음'),
                 element('p', '', data.selected
                    ? '근거를 확인한 뒤 사람이 최종 판단합니다.'
                    : '현재 모델은 이 채팅에서 위험 신호를 제시하지 않았습니다.'));
  analysis.append(verdict);
  analysis.append(element('p', 'signal-heading', `근거 신호 · ${signals.length}건`));
  if (!hasSignals) analysis.append(element('p', 'no-signal', '표시할 위험 신호가 없습니다. 앞선 채팅 문맥도 함께 검사했습니다.'));
  for (const signal of signals) {
    const card = element('section', 'signal-card');
    const top = element('div', 'signal-top');
    top.append(element('span', 'signal-kind', labelFor[signal.kind] || signal.kind),
               element('span', 'strength', signal.strength));
    card.append(top, element('p', '', signal.reason));
    for (const evidence of signal.evidence || []) card.append(element('span', 'quote', `“${evidence.text}”`));
    analysis.append(card);
  }
}

async function submitChat(text) {
  if (busy || !text.trim() || text.length > 500) return;
  busy = true;
  send.disabled = true;
  const target = { messageId: crypto.randomUUID(), text: text.trim() };
  const context = history.slice(-8);
  history.push(target);
  const entry = addChat(target.text);
  showThinking();
  const started = performance.now();
  try {
    const response = await fetch('/api/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requestId: crypto.randomUUID(), experiment: 'include-uncertain',
                             input: { liveId, target, context } }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.code || 'MODEL_UNAVAILABLE');
    showResult(data, performance.now() - started);
    entry.row.classList.add(data.selected ? 'flagged' : 'cleared');
    entry.status.classList.add(data.selected ? 'flagged' : 'cleared');
    entry.status.textContent = data.selected ? '검토 후보 · 데모 결과' : '정상 · 후보 신호 없음';
    updateConnection(true);
  } catch (error) {
    const code = error instanceof Error ? error.message : 'MODEL_UNAVAILABLE';
    showError(code);
    entry.status.textContent = '판독 실패 · 다시 시도 가능';
    // A failed call has no verified observation; do not pass it as prior context.
    history = history.filter((item) => item.messageId !== target.messageId);
    if (code === 'MODEL_UNAVAILABLE') updateConnection(false);
  } finally {
    busy = false;
    send.disabled = false;
    message.focus();
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = message.value;
  message.value = '';
  counter.textContent = '0 / 500';
  submitChat(text);
});
message.addEventListener('input', () => { counter.textContent = `${message.value.length} / 500`; });
message.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});
document.querySelectorAll('[data-example]').forEach((button) => button.addEventListener('click', () => {
  message.value = button.dataset.example;
  counter.textContent = `${message.value.length} / 500`;
  message.focus();
}));
document.getElementById('reset').addEventListener('click', () => {
  if (busy) return;
  liveId = crypto.randomUUID();
  history = [];
  chatLog.replaceChildren(element('div', 'empty-chat', '새 대화입니다. 채팅을 입력해 보세요.'));
  analysis.replaceChildren(element('div', 'empty-analysis', '새 채팅을 기다리는 중'));
  message.value = '';
  counter.textContent = '0 / 500';
  message.focus();
});
checkHealth();
