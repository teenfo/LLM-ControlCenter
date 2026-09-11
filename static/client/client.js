/* 클라이언트 페이지 — 사람이 LLM 을 쓰는 화면. 의존성 없는 바닐라 JS.
 *
 * 관제 UI(app.js)와 같은 규칙이다: 프레임워크·CDN·웹폰트 없음, innerHTML 없음, 토큰은
 * sessionStorage, 화면은 마스킹본만. 헬퍼(t · applyStaticStrings · applyTitleStrings · el · api ·
 * login)는 app.js 의 것을 **글자 그대로** 복제했고 테스트가 두 사본의 동일성을 본다 — 한 벌로
 * 못 만드는 이유는 test_ui.py 가 app.js 를 파일 단위로 못박기 때문이다(ES 모듈 금지 · 함수 순서 핀).
 *
 * 화면은 넷이다: 요청(역할별 단발) · 대화(chat 역할이 있을 때만) · 기록(내 작업) · 계정.
 * **API 에 없는 기능은 그리지 않는다.** 서버에 대화 API 가 없으므로 대화는 화면이 이력을 평문
 * 표식으로 이어 붙여 한 번의 요청으로 보낸다 — 표식은 roles.yaml 의 chat 역할 지시문과 한 벌이다.
 */

'use strict';

//: 관제 UI 와 **다른** 키다. 같은 탭에서 두 면이 서로의 토큰으로 자동 접속하면 안 된다.
const TOKEN_KEY = 'llmcc.client.token';
const END_USER_KEY = 'llmcc.client.end_user';
//: 테마는 사람의 것이라 관제 UI 와 공유한다. 기기에 남는 키는 이 THEME_KEY 하나뿐이다.
const THEME_KEY = 'llmcc:theme';
//: 대화 합성 표식. `chat` 역할의 지시문이 같은 표식을 설명한다 — 한 벌이다. ChatML 류 제어
//: 토큰은 쓰지 않는다 — 베이스라인 가드(injection_control_token)가 마스킹한다.
const USER_MARK = '사용자: ';
const ASSISTANT_MARK = '도우미: ';
const CHAT_ROLE = 'chat';
//: 서버의 본문 한도와 같다. 브라우저에서 읽고 프롬프트에 붙이므로 업로드 라우트가 없다.
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const TEXT_FILE = /\.(txt|md|markdown|csv|tsv|json|log|ya?ml)$/i;
const WAIT_SECONDS = 30;
const MAX_RUN_MS = 10 * 60 * 1000;

const state = {
  token: null,
  session: null,
  strings: {},
  roles: [],
  limits: {},
  endUser: null,                 // 토큰 모드에서만 — 계정 세션은 서버가 아이디로 강제한다
  page: 'ask',
  ask: { role: null, prompt: '', system: '', pending: null, result: null, error: null },
  chat: { turns: [], busy: false, trimmed: 0, error: null, draft: '' },
  history: { jobs: [], filter: 'all', selected: null },
};

// ── app.js 와 글자 그대로 같은 헬퍼 (test_client_page 가 동일성을 본다) ──────

function t(key, params) {
  let text = state.strings[key] || key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split('{' + name + '}').join(String(value));
    }
  }
  return text;
}

function applyStaticStrings(root) {
  for (const node of (root || document).querySelectorAll('[data-t]')) {
    const text = state.strings[node.dataset.t];
    // **카탈로그가 비어 있으면 손대지 않는다.**
    //
    // `t()` 는 없는 키를 키 자체로 돌려주는데(누락이 화면을 멈추게 하지 않는다),
    // 로그인 전에는 카탈로그가 통째로 비어 있다 — 세션 API 로 받아오기 때문이다.
    // 그래서 이 함수가 index.html 의 폴백 텍스트를 `"ui.sign_in"` 같은 키
    // 리터럴로 덮어썼고, **모든 설치의 첫 화면이 깨져 보였다.**
    if (text) node.textContent = text;
  }
}

function applyTitleStrings(root) {
  for (const node of (root || document).querySelectorAll('[data-t-title]')) {
    const text = state.strings[node.dataset.tTitle];
    if (text) node.title = text;
  }
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'html') throw new Error('html 은 쓰지 않는다');
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers = Object.assign(
    { Authorization: 'Bearer ' + state.token }, opts.headers);
  const binary = opts.body instanceof ArrayBuffer || ArrayBuffer.isView(opts.body);
  if (binary) {
    // 플러그인 번들은 raw body 로 올린다 — 멀티파트를 받으려면 서버에
    // `python-multipart` 가 필요하고 그건 6번째 의존성이다.
    opts.headers['Content-Type'] = 'application/octet-stream';
  } else if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch (_) { body = { message: text }; }

  if (!response.ok) {
    // 분기는 코드로, 표시는 메시지로 — 서버가 둘 다 보내는 이유다.
    const error = new Error((body && body.message) || response.statusText);
    error.code = body && body.code;
    error.status = response.status;
    error.body = body;   // params(규칙·범위·retry_after)가 여기 실린다 — 화면은 코드로 분기하고 이것으로 설명한다
    throw error;
  }
  return body;
}

async function login(username, password) {
  const response = await fetch('/v1/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch (_) { body = { message: text }; }
  if (!response.ok) {
    const error = new Error((body && body.message) || response.statusText);
    error.code = body && body.code;
    throw error;
  }
  return body;
}

const $ = (id) => document.getElementById(id);

// ── 작은 헬퍼 ─────────────────────────────────────────────────────────────

let toastTimer = null;
function toast(message) {
  const box = $('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 2400);
}

function showError(err) {
  const box = $('error');
  box.textContent = t('client.error') + ': ' + (err && err.message ? err.message : err);
  box.hidden = false;
  setTimeout(() => { box.hidden = true; }, 8000);
}

function pill(text, tone) {
  return el('span', { class: 'pill' + (tone ? ' ' + tone : ''), text: String(text) });
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : '—';
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function newKey() {
  if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
}

/* 키를 문자열로 조립하지 않는다 — 조립한 키는 카탈로그 대조 테스트가 못 잡는다. */
const STATUS_LABEL = {
  ok: 'client.status_ok',
  pending: 'client.status_pending',
  failed: 'client.status_failed',
  blocked: 'client.status_blocked',
  cancelled: 'client.status_cancelled',
  needs_review: 'client.status_needs_review',
};
const STATUS_TONE = { ok: 'ok', pending: 'info', failed: 'danger', blocked: 'danger', cancelled: '', needs_review: 'warn' };
const GRADE_LABEL = {
  audit: 'client.grade_audit',
  partial: 'client.grade_partial',
  full: 'client.grade_full',
  block: 'client.grade_block',
};
//: 코드별 행동 안내. 문구는 서버 메시지(로케일 협상됨)가 맡고, 여기는 "그래서 무엇을 하나" 만.
const ERROR_HINT = {
  guard_blocked: 'client.err_blocked',
  rate_limited: 'client.err_rate_limited',
  budget_exceeded: 'client.err_budget',
  end_user_required: 'client.err_end_user_required',
  unknown_role: 'client.err_role',
  forbidden_role: 'client.err_role',
  payload_too_large: 'client.err_too_long',
  no_placement: 'client.err_retry_later',
  backend_unavailable: 'client.err_retry_later',
  node_unreachable: 'client.err_retry_later',
  model_not_installed: 'client.err_retry_later',
  administrative_wait_timeout: 'client.err_retry_later',
};

function statusPill(status) {
  return pill(t(STATUS_LABEL[status] || 'client.status_pending'), STATUS_TONE[status] || '');
}

/** 가드 요약 칩. 규칙 id 와 등급뿐 — 값은 서버가 애초에 주지 않는다. */
function guardChips(actions) {
  const entries = Object.entries(actions || {});
  if (!entries.length) return null;
  return el('div', { class: 'guard-chips' }, [
    el('span', { class: 'label', text: t('client.guard_note') }),
    ...entries.map(([rule, grade]) => el('span', { class: 'pill ' + (grade === 'block' ? 'danger' : 'warn') }, [
      el('span', { class: 'mono', text: rule }), ' · ', t(GRADE_LABEL[grade] || 'client.grade_audit'),
    ])),
  ]);
}

/** 오류를 설명한다 — **분기는 코드로**, 문구는 서버 메시지로. 메시지로 분기하지 않는다. */
function explainError(err) {
  const nodes = [el('p', { class: 'message', text: err && err.message ? err.message : String(err) })];
  const hint = err && err.code ? ERROR_HINT[err.code] : null;
  if (hint) nodes.push(el('p', { class: 'hint', text: t(hint) }));
  const body = (err && err.body) || {};
  if (body.retry_after) nodes.push(el('p', { class: 'hint', text: t('client.retry_in', { s: Math.ceil(Number(body.retry_after)) }) }));
  return nodes;
}

/** 401 은 어디서 나든 세션 만료다 — 로그인 화면으로. 처리했으면 true. */
function guard401(err) {
  if (!err || err.status !== 401) return false;
  disconnect();
  const box = $('login-error');
  box.textContent = t('client.session_expired');
  box.hidden = false;
  return true;
}

function withEndUser(body) {
  // 계정 세션은 서버가 아이디로 강제한다 — 보내도 무시된다. 토큰 모드만 이름을 싣는다.
  if (state.session && !state.session.account && state.endUser) body.end_user = state.endUser;
  return body;
}

// ── 테마 ──────────────────────────────────────────────────────────────────

function effectiveTheme() {
  const stamped = document.documentElement.dataset.theme;
  if (stamped) return stamped;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  $('theme').textContent = effectiveTheme() === 'dark' ? '☀' : '☾';
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) { saved = null; }
  applyTheme(saved === 'dark' || saved === 'light' ? saved : null);
}

function toggleTheme() {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (_) { /* 저장 못 해도 이 탭에서는 바뀐다 */ }
}

// ── 세션 ──────────────────────────────────────────────────────────────────

async function loadSession() {
  state.session = await api('/v1/session');
  state.strings = state.session.strings || {};
  document.documentElement.lang = state.session.locale;
  applyStaticStrings();
  applyTitleStrings();
  const roles = await api('/v1/roles');
  state.roles = (roles.roles || []).slice();
  state.limits = roles.limits || {};
  const s = state.session;
  const who = s.account
    ? t('client.signed_in_as', { name: s.account }) + ' · ' + s.tenant.name
    : s.tenant.name + ' · ' + s.service.id + (state.endUser ? ' · ' + state.endUser : '');
  $('who').textContent = who;
}

async function connect(token, endUser) {
  state.token = token;
  state.endUser = endUser || null;
  await loadSession();
  if (!state.session.account && !state.endUser) {
    // 토큰 모드는 이름이 있어야 이력이 그 사람 것만 보이고 사용량이 사람 단위로 잡힌다.
    state.token = null;
    throw new Error(t('client.err_end_user_required'));
  }
  // **sessionStorage 다** — 탭을 닫으면 지워진다. 공용 PC 에 토큰이 남지 않게.
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
    if (state.endUser) sessionStorage.setItem(END_USER_KEY, state.endUser);
    else sessionStorage.removeItem(END_USER_KEY);
  } catch (_) { /* 사파리 프라이빗 등 */ }
  $('login').hidden = true;
  $('shell').hidden = false;
  renderTabs();
  await render();
}

function disconnect() {
  state.token = null;
  state.session = null;
  state.roles = [];
  state.endUser = null;
  state.page = 'ask';   // 다음 사람은 첫 화면부터 — 앞사람이 보던 탭을 물려받지 않는다
  state.ask = { role: null, prompt: '', system: '', pending: null, result: null, error: null };
  state.chat = { turns: [], busy: false, trimmed: 0, error: null, draft: '' };
  state.history = { jobs: [], filter: 'all', selected: null };
  try { sessionStorage.removeItem(TOKEN_KEY); sessionStorage.removeItem(END_USER_KEY); } catch (_) { /* 무시 */ }
  $('shell').hidden = true;
  $('login').hidden = false;
}

// ── 페이지 ────────────────────────────────────────────────────────────────

function askRoles() {
  return state.roles.filter((r) => r.name !== CHAT_ROLE);
}

function chatRole() {
  return state.roles.find((r) => r.name === CHAT_ROLE && r.kind === 'generate') || null;
}

const PAGES = [
  { id: 'ask', label: 'client.tab_ask', show: () => askRoles().length > 0, render: renderAsk },
  // API 뒷받침이 없으면 그리지 않는다 — chat 역할이 없는 서비스에는 대화 탭이 없다.
  { id: 'chat', label: 'client.tab_chat', show: () => !!chatRole(), render: renderChat },
  { id: 'history', label: 'client.tab_history', show: () => true, render: renderHistory },
  { id: 'account', label: 'client.tab_account', show: () => true, render: renderAccount },
];

function visiblePages() {
  return PAGES.filter((p) => p.show());
}

function currentPage() {
  const pages = visiblePages();
  return pages.find((p) => p.id === state.page) || pages[0];
}

function go(pageId) {
  state.page = pageId;
  render();
}

function renderTabs() {
  const current = currentPage();
  $('tabs').replaceChildren(...visiblePages().map((p) => el('button', {
    type: 'button',
    class: 'tab' + (current && p.id === current.id ? ' active' : ''),
    'aria-current': current && p.id === current.id ? 'page' : null,
    text: t(p.label),
    onclick: () => go(p.id),
  })));
}

async function render() {
  const page = currentPage();
  if (!page) return;
  state.page = page.id;
  renderTabs();
  try {
    const nodes = [].concat(await page.render()).filter(Boolean);
    $('view').replaceChildren(...nodes);
  } catch (err) {
    if (!guard401(err)) showError(err);
  }
}

// ── 요청 ──────────────────────────────────────────────────────────────────

function roleLimits(r) {
  return t('client.role_limits', {
    seconds: r.timeout_seconds, chars: Number(r.max_prompt_chars || 0).toLocaleString(),
  });
}

function renderAsk() {
  const roles = askRoles();
  if (!state.ask.role || !roles.some((r) => r.name === state.ask.role)) {
    state.ask.role = roles.length ? roles[0].name : null;
  }
  const role = roles.find((r) => r.name === state.ask.role) || null;
  const limit = role ? Number(role.max_prompt_chars || 0) : 0;

  const picker = el('div', { class: 'role-grid' }, roles.map((r) => el('button', {
    type: 'button',
    class: 'role-card' + (r.name === state.ask.role ? ' active' : ''),
    'aria-pressed': r.name === state.ask.role ? 'true' : 'false',
    onclick: () => { state.ask.role = r.name; render(); },
  }, [
    el('div', { class: 'name', text: r.name }),
    el('div', { class: 'meta' }, [pill(r.kind, r.kind === 'embed' ? 'info' : 'accent'), el('span', { text: roleLimits(r) })]),
    r.has_default_system ? el('div', { class: 'note', text: t('client.system_default_note') }) : null,
  ])));

  const send = el('button', { type: 'button', class: 'primary', id: 'send', text: t('client.send') });
  const counter = el('span', { class: 'counter', id: 'counter' });
  const textarea = el('textarea', { id: 'prompt', rows: 10, placeholder: t('client.prompt_placeholder') });
  textarea.value = state.ask.prompt;
  const updateCounter = () => {
    const n = textarea.value.length;
    counter.textContent = t('client.chars', { n: n.toLocaleString(), limit: limit.toLocaleString() });
    const over = !!limit && n > limit;
    counter.classList.toggle('over', over);
    // 서버의 413 과 이중 방어다 — 넘는 줄 알면서 보내게 두지 않는다.
    send.disabled = !n || over || !!state.ask.pending || !role;
  };
  textarea.addEventListener('input', () => { state.ask.prompt = textarea.value; updateCounter(); });
  send.addEventListener('click', () => submitAsk(role));

  const fileInput = el('input', {
    type: 'file', id: 'files', multiple: true,
    accept: '.txt,.md,.markdown,.csv,.tsv,.json,.log,.yaml,.yml,text/*,application/json',
  });
  fileInput.addEventListener('change', () => { addFiles(fileInput.files, textarea, updateCounter); fileInput.value = ''; });
  const drop = el('label', { class: 'dropzone', for: 'files' }, [el('span', { text: t('client.drop_hint') }), fileInput]);
  drop.addEventListener('dragover', (event) => { event.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    drop.classList.remove('over');
    addFiles(event.dataTransfer.files, textarea, updateCounter);
  });

  const system = el('textarea', { id: 'system', rows: 2, placeholder: t('client.system_placeholder') });
  system.value = state.ask.system;
  system.addEventListener('input', () => { state.ask.system = system.value; });
  const advanced = el('details', { class: 'advanced' }, [
    el('summary', { text: t('client.system') }),
    system,
    el('p', { class: 'hint', text: t('client.system_hint') }),
  ]);

  const cancel = state.ask.pending && state.ask.pending.job_id
    ? el('button', { type: 'button', text: t('client.cancel'), onclick: () => cancelJob(state.ask.pending.job_id) })
    : null;
  updateCounter();

  return [
    el('section', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: t('client.role') })]),
      el('div', { class: 'card-body' }, [roles.length ? picker : el('p', { class: 'muted', text: t('client.none') })]),
    ]),
    el('section', { class: 'card composer' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: t('client.prompt') }), el('div', { class: 'aside' }, [counter])]),
      el('div', { class: 'card-body' }, [
        textarea, drop, role && role.kind === 'generate' ? advanced : null,
        el('div', { class: 'row' }, [send, cancel]),
      ]),
    ]),
    renderPending(state.ask.pending),
    renderResult(state.ask.result, state.ask.error),
  ];
}

/** 파일은 브라우저 안에서 읽어 프롬프트 뒤에 붙인다 — 업로드도 서버 변환도 없다. */
function addFiles(files, textarea, after) {
  for (const file of Array.from(files || [])) {
    if (file.size > MAX_FILE_BYTES) { toast(t('client.file_too_large', { name: file.name })); continue; }
    const textual = file.type.startsWith('text/') || file.type === 'application/json' || TEXT_FILE.test(file.name);
    if (!textual) { toast(t('client.file_unsupported', { name: file.name })); continue; }
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || '');
      textarea.value = (textarea.value ? textarea.value + '\n\n' : '') + '--- ' + file.name + ' ---\n' + text;
      state.ask.prompt = textarea.value;
      after();
      toast(t('client.file_added', { name: file.name }));
    };
    reader.readAsText(file, 'utf-8');
  }
}

/** 제출 → 대기 → 완료. **서버의 retry_after 를 지킨다** — 고정 간격 폴링은 큐가 길수록 컨트롤 플레인을 때린다.
 *  clients/client.py 의 run() 과 같은 루프다. 재시도는 같은 Idempotency-Key 로 — 작업이 두 번 만들어지지 않는다. */
async function runJob(body, onProgress) {
  const key = newKey();
  const started = Date.now();
  let result = await api('/v1/generate', {
    method: 'POST', headers: { 'Idempotency-Key': key },
    body: Object.assign({ wait: WAIT_SECONDS }, body),
  });
  while (result.status === 'pending' && Date.now() - started < MAX_RUN_MS) {
    const delay = Math.min(60, Math.max(1, Number(result.retry_after) || 2));
    if (onProgress) onProgress(result, delay);
    await sleep(delay * 1000);
    result = await api('/v1/jobs/' + encodeURIComponent(result.job_id) + '?wait=' + WAIT_SECONDS);
  }
  return result;
}

async function submitAsk(role) {
  if (!role || state.ask.pending) return;
  const prompt = state.ask.prompt.trim();
  if (!prompt) return;
  state.ask.error = null;
  state.ask.result = null;
  state.ask.pending = { job_id: null, queue: null, delay: null };
  const started = Date.now();
  await render();
  try {
    if (role.kind === 'embed') {
      const body = await api('/v1/embed', { method: 'POST', body: withEndUser({ role: role.name, input: prompt }) });
      state.ask.result = { kind: 'embed', body, elapsed: Date.now() - started };
    } else {
      const payload = withEndUser({ role: role.name, prompt });
      if (state.ask.system.trim()) payload.system = state.ask.system.trim();
      const result = await runJob(payload, (r, delay) => {
        state.ask.pending = { job_id: r.job_id, queue: r.queue_position, delay };
        render();
      });
      state.ask.result = { kind: 'generate', body: result, elapsed: Date.now() - started };
    }
  } catch (err) {
    if (guard401(err)) return;
    state.ask.error = err;
  }
  state.ask.pending = null;
  await render();
}

async function cancelJob(jobId) {
  try {
    await api('/v1/jobs/' + encodeURIComponent(jobId), { method: 'DELETE' });
    toast(t('client.status_cancelled'));
  } catch (err) {
    if (!guard401(err)) showError(err);   // 실행 중이면 409 — 취소할 수 없다고 서버가 말한다
  }
}

function renderPending(p) {
  if (!p) return null;
  return el('section', { class: 'card pending' }, [
    el('div', { class: 'card-body row' }, [
      el('span', { class: 'dot pulse' }),
      el('span', { text: t('client.pending') }),
      p.queue !== null && p.queue !== undefined ? el('span', { class: 'muted', text: t('client.queue_position', { n: p.queue }) }) : null,
      p.delay ? el('span', { class: 'muted', text: t('client.retry_in', { s: p.delay }) }) : null,
      p.job_id ? el('span', { class: 'mono muted', text: p.job_id }) : null,
    ]),
  ]);
}

function renderResult(result, error) {
  if (error) {
    return el('section', { class: 'card bad result' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: t('client.error') })]),
      el('div', { class: 'card-body' }, explainError(error)),
    ]);
  }
  if (!result) return null;
  if (result.kind === 'embed') {
    const vectors = result.body.vectors || [];
    const dims = vectors.length ? vectors[0].length : 0;
    return el('section', { class: 'card result' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: t('client.response') }), el('div', { class: 'aside' }, [
        el('span', { text: t('client.vectors', { n: vectors.length, dims }) })])]),
      el('div', { class: 'card-body' }, [
        el('pre', { class: 'response', text: vectors.map((v) => v.slice(0, 8).map((x) => Number(x).toFixed(4)).join(', ') + (v.length > 8 ? ' …' : '')).join('\n') }),
        guardChips(result.body.guard_actions),
        el('p', { class: 'hint', text: t('client.elapsed', { s: (result.elapsed / 1000).toFixed(1) }) + ' · ' + (result.body.model || '') }),
      ]),
    ]);
  }
  const j = result.body;
  const failed = j.status !== 'ok';
  return el('section', { class: 'card result' + (failed ? ' bad' : '') }, [
    el('div', { class: 'card-head' }, [el('h2', { text: t('client.response') }), el('div', { class: 'aside' }, [statusPill(j.status)])]),
    el('div', { class: 'card-body' }, [
      el('pre', { class: 'response', text: failed ? (j.error || t(STATUS_LABEL[j.status] || 'client.status_failed')) : (j.response || '') }),
      guardChips(j.guard_actions),
      el('details', { class: 'meta' }, [
        el('summary', { text: t('client.details') }),
        el('dl', {}, [
          el('dt', { text: t('client.model') }), el('dd', { class: 'mono', text: (j.model || '—') + (j.tier ? ' · ' + j.tier : '') }),
          el('dt', { text: t('client.attempts') }), el('dd', { text: String(j.attempts ?? 0) }),
          el('dt', { text: t('client.job_id') }), el('dd', { class: 'mono', text: j.job_id }),
          el('dt', { text: t('client.elapsed', { s: '' }).trim() }), el('dd', { text: (result.elapsed / 1000).toFixed(1) + 's' }),
        ]),
      ]),
    ]),
  ]);
}

// ── 대화 ──────────────────────────────────────────────────────────────────

/** 이력을 평문 표식으로 이어 붙인다. 오래된 턴부터 잘라 한도에 맞춘다 — 마지막 사용자 턴은 남긴다. */
function composePrompt(turns, limit) {
  const tail = ASSISTANT_MARK.trim();
  const lines = [];
  let used = tail.length + 1;
  let dropped = 0;
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const turn = turns[i];
    const line = (turn.role === 'user' ? USER_MARK : ASSISTANT_MARK) + turn.text;
    if (lines.length && limit && used + line.length + 1 > limit) { dropped = i + 1; break; }
    lines.unshift(line);
    used += line.length + 1;
  }
  return { prompt: lines.join('\n') + '\n' + tail, dropped };
}

function renderChat() {
  const role = chatRole();
  if (!role) return [];
  const limit = Number(role.max_prompt_chars || 0);
  const log = el('div', { class: 'chat-log', id: 'chat-log' },
    state.chat.turns.length
      ? state.chat.turns.map((turn) => el('div', { class: 'bubble ' + turn.role }, [
        el('div', { class: 'text', text: turn.text }), turn.guard ? guardChips(turn.guard) : null]))
      : [el('div', { class: 'empty', text: t('client.chat_empty') })]);
  if (state.chat.busy) log.appendChild(el('div', { class: 'bubble assistant pending' }, [el('span', { class: 'dot pulse' }), ' ', t('client.pending')]));
  if (state.chat.error) log.appendChild(el('div', { class: 'bubble error' }, explainError(state.chat.error)));

  const input = el('textarea', { id: 'chat-input', rows: 3, placeholder: t('client.chat_placeholder') });
  // 응답이 오면 화면을 다시 그린다 — 그 사이에 치던 글이 사라지면 안 된다.
  input.value = state.chat.draft || '';
  input.addEventListener('input', () => { state.chat.draft = input.value; });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendChat(input, role, limit); }
  });
  const send = el('button', { type: 'button', class: 'primary', id: 'chat-send', text: t('client.send'), onclick: () => sendChat(input, role, limit) });
  send.disabled = state.chat.busy;
  const reset = el('button', {
    type: 'button', class: 'sm', text: t('client.chat_new'),
    onclick: () => { state.chat = { turns: [], busy: false, trimmed: 0, error: null, draft: '' }; render(); },
  });
  return [
    el('section', { class: 'card chat' }, [
      el('div', { class: 'card-head' }, [
        el('h2', { text: t('client.tab_chat') }),
        el('div', { class: 'aside' }, [el('span', { class: 'mono', text: role.name }), el('span', { text: roleLimits(role) }), reset]),
      ]),
      log,
      state.chat.trimmed ? el('p', { class: 'hint trimmed', text: t('client.chat_trimmed', { n: state.chat.trimmed }) }) : null,
      el('div', { class: 'chat-input' }, [input, send]),
    ]),
  ];
}

async function sendChat(input, role, limit) {
  const text = input.value.trim();
  if (!text || state.chat.busy) return;
  if (limit && text.length + USER_MARK.length + ASSISTANT_MARK.length + 2 > limit) {
    state.chat.error = Object.assign(new Error(t('client.too_long')), { code: 'payload_too_large' });
    await render();
    return;
  }
  state.chat.turns.push({ role: 'user', text });
  state.chat.busy = true;
  state.chat.error = null;
  state.chat.draft = '';
  await render();
  const composed = composePrompt(state.chat.turns, limit);
  state.chat.trimmed = composed.dropped;
  try {
    // system 은 보내지 않는다 — 역할의 기본 지시문(표식을 설명하는 그것)이 적용돼야 한다.
    const result = await runJob(withEndUser({ role: role.name, prompt: composed.prompt }));
    if (result.status === 'ok') {
      state.chat.turns.push({ role: 'assistant', text: result.response || '', guard: result.guard_actions });
    } else {
      state.chat.error = Object.assign(
        new Error(result.error || t(STATUS_LABEL[result.status] || 'client.status_failed')),
        { code: result.error_code || null });
    }
  } catch (err) {
    if (guard401(err)) return;
    state.chat.error = err;
  }
  state.chat.busy = false;
  await render();
  const log = $('chat-log');
  if (log) log.scrollTop = log.scrollHeight;
}

// ── 기록 ──────────────────────────────────────────────────────────────────

const HISTORY_FILTERS = [
  { id: 'all', label: 'client.filter_all', match: () => true },
  { id: 'pending', label: 'client.filter_pending', match: (j) => j.status === 'pending' },
  { id: 'done', label: 'client.filter_done', match: (j) => j.status === 'ok' },
  { id: 'failed', label: 'client.filter_failed', match: (j) => j.status === 'failed' || j.status === 'cancelled' },
  { id: 'guard', label: 'client.filter_guard', match: (j) => j.status === 'blocked' || Object.keys(j.guard_actions || {}).length > 0 },
];

async function loadHistory() {
  // 마스킹본만 온다 — 원문은 이 화면에 없다. 토큰 모드는 이름으로 범위를 좁힌다.
  const query = state.session.account ? '' : '&end_user=' + encodeURIComponent(state.endUser || '');
  const body = await api('/v1/jobs?limit=100' + query);
  state.history.jobs = body.jobs || [];
}

async function renderHistory() {
  await loadHistory();
  const filter = HISTORY_FILTERS.find((f) => f.id === state.history.filter) || HISTORY_FILTERS[0];
  const jobs = state.history.jobs.filter(filter.match);
  const chips = el('div', { class: 'chips' }, HISTORY_FILTERS.map((f) => el('button', {
    type: 'button', class: 'chip' + (f.id === filter.id ? ' active' : ''),
    onclick: () => { state.history.filter = f.id; render(); },
  }, [t(f.label), el('span', { class: 'n', text: String(state.history.jobs.filter(f.match).length) })])));
  const refresh = el('button', { type: 'button', class: 'sm', text: t('client.refresh'), onclick: () => render() });
  const rows = jobs.map((j) => el('button', {
    type: 'button', class: 'history-row' + (state.history.selected === j.job_id ? ' active' : ''),
    onclick: () => { state.history.selected = j.job_id; render(); },
  }, [
    statusPill(j.status),
    el('div', { class: 'body' }, [
      el('div', { class: 'title', text: j.role + (j.model ? ' · ' + j.model : '') }),
      el('div', { class: 'detail', text: (j.prompt_masked || '').slice(0, 140) }),
    ]),
    el('div', { class: 'time', text: when(j.created_at) }),
  ]));
  const selected = jobs.find((j) => j.job_id === state.history.selected) || null;
  return [
    el('div', { class: 'row between' }, [chips, refresh]),
    el('section', { class: 'card' }, [
      rows.length ? el('div', { class: 'list' }, rows) : el('div', { class: 'empty', text: t('client.history_empty') }),
    ]),
    selected ? renderDetail(selected) : null,
  ];
}

function renderDetail(j) {
  const pending = j.status === 'pending';
  return el('section', { class: 'card detail' }, [
    el('div', { class: 'card-head' }, [
      statusPill(j.status), el('h2', { class: 'mono', text: j.job_id }),
      el('div', { class: 'aside' }, [el('button', {
        type: 'button', class: 'sm', text: t('client.close'), onclick: () => { state.history.selected = null; render(); },
      })]),
    ]),
    el('div', { class: 'card-body' }, [
      el('label', { text: t('client.prompt') + ' · ' + t('client.masked_only') }),
      el('pre', { class: 'response', text: j.prompt_masked || '' }),
      j.response ? el('label', { text: t('client.response') }) : null,
      j.response ? el('pre', { class: 'response', text: j.response }) : null,
      guardChips(j.guard_actions),
      el('dl', {}, [
        el('dt', { text: t('client.model') }), el('dd', { class: 'mono', text: (j.model || '—') + (j.tier ? ' · ' + j.tier : '') }),
        el('dt', { text: t('client.created') }), el('dd', { text: when(j.created_at) }),
        el('dt', { text: t('client.finished') }), el('dd', { text: when(j.finished_at) }),
        j.error_code ? el('dt', { text: t('client.error') }) : null,
        j.error_code ? el('dd', { class: 'mono', text: j.error_code }) : null,
      ]),
      pending ? el('div', { class: 'row' }, [
        el('button', { type: 'button', class: 'sm', text: t('client.check_now'), onclick: () => render() }),
        el('button', { type: 'button', class: 'sm', text: t('client.cancel'), onclick: async () => { await cancelJob(j.job_id); render(); } }),
      ]) : null,
    ]),
  ]);
}

// ── 계정 ──────────────────────────────────────────────────────────────────

function renderAccount() {
  const s = state.session;
  const facts = el('dl', {}, [
    el('dt', { text: t('client.name') }), el('dd', { text: s.account || state.endUser || '—' }),
    el('dt', { text: t('client.tenant') }), el('dd', { text: s.tenant.name }),
    el('dt', { text: t('client.service') }), el('dd', { class: 'mono', text: s.service.id }),
  ]);
  const cards = [el('section', { class: 'card' }, [
    el('div', { class: 'card-head' }, [el('h2', { text: t('client.account') })]),
    el('div', { class: 'card-body' }, [
      facts,
      s.account ? null : el('p', { class: 'hint', text: t('client.token_mode_hint') }),
      el('div', { class: 'row' }, [el('button', { type: 'button', text: t('client.logout'), onclick: logoutClick })]),
    ]),
  ])];
  if (s.account) {
    const current = el('input', { id: 'pw-current', type: 'password', autocomplete: 'current-password' });
    const next = el('input', { id: 'pw-next', type: 'password', autocomplete: 'new-password' });
    const again = el('input', { id: 'pw-again', type: 'password', autocomplete: 'new-password' });
    const form = el('form', {
      class: 'stack',
      onsubmit: async (event) => {
        event.preventDefault();
        if (next.value !== again.value) { showError(new Error(t('client.password_mismatch'))); return; }
        try {
          await api('/v1/session/password', { method: 'POST', body: { current_password: current.value, new_password: next.value } });
          current.value = next.value = again.value = '';
          toast(t('client.password_changed'));
        } catch (err) { if (!guard401(err)) showError(err); }
      },
    }, [
      el('div', {}, [el('label', { for: 'pw-current', text: t('client.current_password') }), current]),
      el('div', {}, [el('label', { for: 'pw-next', text: t('client.new_password') }), next]),
      el('div', {}, [el('label', { for: 'pw-again', text: t('client.confirm_password') }), again]),
      el('button', { type: 'submit', class: 'primary', text: t('client.change_password') }),
    ]);
    cards.push(el('section', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h2', { text: t('client.change_password') })]),
      el('div', { class: 'card-body' }, [form]),
    ]));
  }
  return cards;
}

async function logoutClick() {
  if (state.session && state.session.account) {
    try { await api('/v1/logout', { method: 'POST' }); } catch (_) { /* 이미 만료됐을 수 있다 */ }
  }
  disconnect();
}

// ── 부팅 ──────────────────────────────────────────────────────────────────

async function boot() {
  applyStaticStrings();
  initTheme();
  const setTokenMode = (on) => {
    $('account-fields').hidden = on;
    $('token-fields').hidden = !on;
    $('login-mode-token').hidden = on;
    $('login-mode-account').hidden = !on;
  };
  $('login-mode-token').addEventListener('click', () => setTokenMode(true));
  $('login-mode-account').addEventListener('click', () => setTokenMode(false));

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const box = $('login-error');
    box.hidden = true;
    try {
      if (!$('token-fields').hidden) {
        const name = $('end-user').value.trim();
        if (!name) throw new Error($('end-user-hint').textContent);
        await connect($('token').value.trim(), name);
      } else {
        const session = await login($('username').value.trim(), $('password').value);
        $('password').value = '';
        await connect(session.token, null);
      }
    } catch (err) {
      box.textContent = err.message || String(err);
      box.hidden = false;
    }
  });
  $('theme').addEventListener('click', toggleTheme);
  $('logout').addEventListener('click', logoutClick);

  let saved = null;
  let savedName = null;
  try { saved = sessionStorage.getItem(TOKEN_KEY); savedName = sessionStorage.getItem(END_USER_KEY); } catch (_) { saved = null; }
  if (saved) {
    try { await connect(saved, savedName); return; } catch (_) { /* 만료·폐기된 토큰 */ }
  }
  $('login').hidden = false;
}

document.addEventListener('DOMContentLoaded', boot);
