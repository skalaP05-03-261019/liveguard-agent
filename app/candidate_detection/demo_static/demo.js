const chatLog = document.getElementById('chat-log');
const analysis = document.getElementById('analysis-content');
const form = document.getElementById('composer');
const message = document.getElementById('message');
const send = document.getElementById('send');
const counter = document.getElementById('counter');
let liveId = crypto.randomUUID();
let history = [];
let busy = false;
let backendConfigured = false;
let backendLiveId = null;

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
    backendConfigured = state.backendConfigured === true;
    document.getElementById('backend-setup').hidden = !backendConfigured;
    if (backendConfigured) {
      updateRoute();
      restoreBackendSession();
    }
  } catch {
    updateConnection(false);
  }
}

function integratedMode() {
  return backendConfigured && document.getElementById('demo-mode').value === 'backend';
}

function updateRoute() {
  document.getElementById('route-note').textContent = integratedMode()
    ? '검사 경로: 실제 BE 채팅 전송 → BE 처리 결과 + Agent 별도 후보 비교 (Agent 결과는 BE에 제출하지 않음)'
    : '검사 경로: BE 필터 미연결 → Agent 직접 판독';
}

function csrfToken() {
  const cookie = document.cookie.split('; ').find((value) => value.startsWith('XSRF-TOKEN='));
  return cookie ? decodeURIComponent(cookie.slice('XSRF-TOKEN='.length)) : '';
}

async function backendFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method === 'POST') {
    headers['Content-Type'] = 'application/json';
    const token = csrfToken();
    if (token) headers['X-XSRF-TOKEN'] = token;
  }
  const response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.code || `BE_HTTP_${response.status}`);
  return payload.data;
}

async function setBackendAccount(account) {
  const channels = account?.channelIds;
  if (!Array.isArray(channels) || !channels.length) throw new Error('BE_CHANNEL_REQUIRED');
  const select = document.getElementById('backend-channel');
  select.replaceChildren(...channels.map((id) => {
    const option = element('option', '', id);
    option.value = id;
    return option;
  }));
  document.getElementById('backend-login').hidden = true;
  document.getElementById('backend-session').hidden = false;
  await selectBackendLive();
}

async function selectBackendLive() {
  const previousLiveId = backendLiveId;
  backendLiveId = null;
  const state = document.getElementById('backend-state');
  try {
    const channelId = document.getElementById('backend-channel').value;
    const live = await backendFetch(`/api/backend/channels/${encodeURIComponent(channelId)}/current-live`);
    if (!live?.liveId) throw new Error('BE_LIVE_ID_MISSING');
    backendLiveId = live.liveId;
    if (previousLiveId && previousLiveId !== backendLiveId) history = [];
    state.textContent = `BE 연결됨 · 방송 ${backendLiveId.slice(0, 8)} · 로그인 계정으로 실제 채팅 전송`;
  } catch (error) {
    state.textContent = `BE 방송 연결 실패 · ${error.message}`;
  }
}

async function restoreBackendSession() {
  try {
    await setBackendAccount(await backendFetch('/api/backend/me'));
  } catch {
    document.getElementById('backend-state').textContent = 'BE 시청자 계정으로 로그인하세요.';
  }
}

document.getElementById('demo-mode').addEventListener('change', () => {
  history = [];
  liveId = crypto.randomUUID();
  updateRoute();
});
document.getElementById('backend-channel').addEventListener('change', selectBackendLive);
document.getElementById('backend-login').addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = document.getElementById('backend-username').value;
  const passwordField = document.getElementById('backend-password');
  const password = passwordField.value;
  passwordField.value = '';
  try {
    await setBackendAccount(await backendFetch('/api/backend/login', {
      method: 'POST', body: JSON.stringify({ username, password }),
    }));
  } catch (error) {
    document.getElementById('backend-state').textContent = `BE 로그인/방송 연결 실패 · ${error.message}`;
  }
});

function addChat(text) {
  document.getElementById('empty-chat')?.remove();
  const row = element('div', 'chat-entry');
  const avatar = element('span', 'avatar', '시');
  const wrap = element('div', 'bubble-wrap');
  wrap.append(element('p', 'bubble-meta', '시청자 · 방금'), element('div', 'bubble', text));
  const status = element('span', 'entry-status', 'AI 판독 중…');
  const agentStatus = element('span', 'entry-status', '');
  const origin = element('span', 'entry-origin', 'BE 미연결 · Agent 직접 검사');
  wrap.append(status, agentStatus, origin);
  row.append(avatar, wrap);
  chatLog.append(row);
  chatLog.scrollTop = chatLog.scrollHeight;
  return { row, status, agentStatus, origin };
}

function backendLabel(message) {
  return { PUBLIC: 'BE 공개', HELD: 'BE 임시 보류', HIDDEN: 'BE 숨김' }[message.visibility]
    || 'BE 상태 확인 필요';
}

function showBackend(message) {
  const card = element('section', `backend-result ${message.visibility?.toLowerCase() || ''}`);
  card.append(element('strong', '', backendLabel(message)),
              element('p', '', `BE 사유: ${message.moderationReasonCode || '규칙 후보 없음'}`));
  if (message.moderationNotice) card.append(element('p', '', message.moderationNotice));
  card.append(element('small', '', `BE 메시지 ${message.messageId} · Agent 별도 실험 결과는 실제 가림·제재에 반영되지 않음`));
  return card;
}

function showBackendFailure(code) {
  analysis.replaceChildren(element('section', 'backend-result hidden', `BE 전송 실패 · ${code}. 채팅이 저장되지 않아 Agent 비교도 실행하지 않았습니다.`));
}

function showThinking() {
  analysis.replaceChildren(element('div', 'thinking', '채팅의 표현과 문맥을 확인하고 있습니다…'));
}

function showError(code, elapsedMs, backendMessage = null) {
  analysis.replaceChildren();
  const head = element('div', 'result-head');
  head.append(element('span', 'result-title', '방금 보낸 채팅의 처리'),
              element('span', 'elapsed', `${(elapsedMs / 1000).toFixed(2)}초`));
  analysis.append(head);
  if (backendMessage) analysis.append(showBackend(backendMessage));
  analysis.append(element('p', 'source-summary', backendMessage
    ? 'BE 결과는 위와 같음 · Agent 별도 판독 불가 · 모델 후보 여부 미확인'
    : 'BE 미연결 · Agent 판독 불가 · 시스템 검토 전환'));
  const verdict = element('div', 'verdict flagged');
  verdict.append(element('small', 'verdict-caption', 'AI 근거 없음 · 안전 분기'),
                 element('strong', '', '검토 후보'),
                 element('p', '', backendMessage
                   ? 'Agent 후보 여부를 확인할 수 없습니다. 실제 BE 처리 결과는 위에 표시한 상태가 유지됩니다.'
                   : 'AI가 위반이라고 판단한 결과가 아닙니다. 응답을 확인할 수 없어 정상으로 단정하지 않고 사람이 원문과 문맥을 검토하도록 남겼습니다.'));
  analysis.append(verdict,
                  element('p', 'review-note', errorFor[code] || 'AI 판독 불가 · 모델 연결을 확인해 주세요.'),
                  element('p', 'diagnostic-code', `AI 판독 불가 · ${code}`));
}

function showResult(data, elapsedMs, backendMessage = null) {
  analysis.replaceChildren();
  const head = element('div', 'result-head');
  head.append(element('span', 'result-title', '방금 보낸 채팅의 판독'),
              element('span', 'elapsed', `${(elapsedMs / 1000).toFixed(2)}초`));
  analysis.append(head);
  if (backendMessage) analysis.append(showBackend(backendMessage));
  analysis.append(element('p', 'source-summary',
    `${backendMessage ? 'BE 결과와 별도 비교 · ' : 'BE 미연결 · '}${data.selected ? 'Agent 후보 신호' : 'Agent 후보 신호 없음'}`));
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
  if (integratedMode() && !backendLiveId) {
    document.getElementById('backend-state').textContent = '먼저 BE에 로그인하고 진행 중 방송을 선택하세요.';
    return;
  }
  busy = true;
  send.disabled = true;
  const target = { messageId: crypto.randomUUID(), text: text.trim() };
  const context = history.slice(-8);
  const entry = addChat(target.text);
  const withBackend = integratedMode();
  if (withBackend) entry.status.textContent = 'BE 전송 중…';
  let backendMessage = null;
  showThinking();
  const started = performance.now();
  try {
    if (withBackend) {
      try {
        backendMessage = await backendFetch(`/api/backend/lives/${encodeURIComponent(backendLiveId)}/messages`, {
          method: 'POST', body: JSON.stringify({ clientMessageId: target.messageId, text: target.text }),
        });
        if (!backendMessage?.messageId || !['PUBLIC', 'HELD', 'HIDDEN'].includes(backendMessage.visibility)) {
          throw new Error('BE_INVALID_MESSAGE');
        }
      } catch (error) {
        const code = error instanceof Error ? error.message : 'BACKEND_UNAVAILABLE';
        showBackendFailure(code);
        entry.status.textContent = `BE 전송 실패 · ${code}`;
        entry.agentStatus.textContent = 'Agent 미실행';
        entry.origin.textContent = 'BE 응답 오류 · Agent 미실행';
        return;
      }
      target.messageId = backendMessage.messageId;
      liveId = backendLiveId;
      entry.status.textContent = `${backendLabel(backendMessage)} · ${backendMessage.moderationReasonCode || 'PASS'}`;
      entry.origin.textContent = 'BE 실제 채팅 처리 · Agent 별도 실험 중';
    }
    history.push(target);
    const response = await fetch('/api/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requestId: crypto.randomUUID(), experiment: 'include-uncertain',
                             input: { liveId, target, context } }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.code || 'MODEL_UNAVAILABLE');
    showResult(data, performance.now() - started, backendMessage);
    entry.row.classList.add(withBackend && backendMessage.visibility !== 'PUBLIC'
      ? 'flagged' : data.selected ? 'flagged' : 'cleared');
    (withBackend ? entry.agentStatus : entry.status).classList.add(data.selected ? 'flagged' : 'cleared');
    (withBackend ? entry.agentStatus : entry.status).textContent = data.selected
      ? 'Agent 검토 후보 · 별도 실험' : 'Agent 정상 · 후보 신호 없음';
    entry.origin.textContent = `${withBackend ? 'BE 실제 처리 · ' : 'BE 미연결 · '}${data.selected ? 'Agent 후보 신호' : 'Agent 후보 신호 없음'}`;
    updateConnection(true);
  } catch (error) {
    const code = error instanceof Error ? error.message : 'MODEL_UNAVAILABLE';
    showError(code, performance.now() - started, backendMessage);
    entry.row.classList.add('flagged');
    (withBackend ? entry.agentStatus : entry.status).classList.add('flagged');
    (withBackend ? entry.agentStatus : entry.status).textContent = '검토 후보 · AI 판독 불가';
    entry.origin.textContent = `${withBackend ? 'BE 실제 처리 · ' : 'BE 미연결 · '}Agent 판독 불가`;
    // A failed call has no verified observation; do not pass it as prior context.
    if (!withBackend) history = history.filter((item) => item.messageId !== target.messageId);
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
