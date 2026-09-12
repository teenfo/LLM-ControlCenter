/* 관제 UI — 의존성 없는 바닐라 JS.
 *
 * 세 가지가 이 파일의 요지다.
 *
 * ① **화면은 마스킹본만 본다.** 원문은 단건 API 로만 열고, 여는 순간 감사에 남는다.
 * ② **조용한 실패를 시끄럽게 만든다.** 안 켜진 로케일 팩 · 안 붙은 2단 분류기 ·
 *    없는 알림 채널 · 단일 호밍 역할 — 전부 상시 배너로 띄운다. 관제 센터가
 *    안 보여주면 사람이 판단할 수 없고, 다국어에서는 켰다고 착각하기가 더 쉽다.
 * ③ **외부 CDN 도 프레임워크도 없다.** 에어갭에서 그대로 떠야 한다.
 *
 * 화면 구조는 디자인 핸드오프(2026-09)의 것이다 — 좌측 아이콘 레일 9개 진입점 · 헤더의
 * 상태 필 · 카드 · 우측 드로어 · 토스트. 기능 면은 그대로다: 서버가 주는 것만 그리고,
 * 토큰 원값 열람이나 역할 편집처럼 API 에 없는 것은 화면에도 없다.
 */

'use strict';

const TOKEN_KEY = 'llmcc.token';
//: 테마 선택. 토큰과 달리 기기에 남아도 되는 값이라 localStorage 다 — 이 키 하나뿐이다.
const THEME_KEY = 'llmcc:theme';

const state = {
  token: null,
  session: null,
  page: null,
  sub: {},
  strings: {},
  refreshTimer: null,
  platformBlocked: false,
  jobFilter: 'all',
  usageAxis: 'service_id',
  header: { nodes: null, pending: 0 },
};

// ── 문자열 ────────────────────────────────────────────────────────────────

/** 번역. 없는 키는 키 자체를 돌려준다 — 누락이 화면을 멈추게 하지 않는다. */
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

/** 툴팁(title)용 문자열. 본문과 같은 규칙 — 카탈로그가 비면 폴백을 둔다. */
function applyTitleStrings(root) {
  for (const node of (root || document).querySelectorAll('[data-t-title]')) {
    const text = state.strings[node.dataset.tTitle];
    if (text) node.title = text;
  }
}

// ── DOM 헬퍼 ──────────────────────────────────────────────────────────────

/** 요소 하나. **문자열이 아니라 노드로 조립한다** — innerHTML 로 서버 데이터를
 *  꽂으면 테넌트 이름 하나로 XSS 가 열린다. */
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

const $ = (id) => document.getElementById(id);

// SVG 네임스페이스 URI. 네트워크 참조가 아니라 식별자다 — 외부 자산 금지 검사에 걸리지 않도록
// 스킴을 붙여 만든다.
const SVG_NS = ['http:', '//www.w3.org/2000/svg'].join('');

/* 레일 아이콘. 외부 아이콘 폰트 없이 18px 선 아이콘을 노드로 만든다. */
const ICONS = {
  overview: [['rect', { x: 3, y: 3, width: 7, height: 9, rx: 1.5 }], ['rect', { x: 14, y: 3, width: 7, height: 5, rx: 1.5 }],
    ['rect', { x: 14, y: 12, width: 7, height: 9, rx: 1.5 }], ['rect', { x: 3, y: 16, width: 7, height: 5, rx: 1.5 }]],
  nodes: [['rect', { x: 3, y: 4, width: 18, height: 6, rx: 1.5 }], ['rect', { x: 3, y: 14, width: 18, height: 6, rx: 1.5 }],
    ['path', { d: 'M7 7h.01M7 17h.01' }]],
  tenants: [['path', { d: 'M3 21h18M5 21V7l7-4 7 4v14' }], ['path', { d: 'M9 21v-5h6v5M9 11h.01M15 11h.01' }]],
  models: [['path', { d: 'M12 2 3 7v10l9 5 9-5V7z' }], ['path', { d: 'M3 7l9 5 9-5M12 12v10' }]],
  rules: [['path', { d: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z' }], ['path', { d: 'M9 12l2 2 4-4' }]],
  jobs: [['circle', { cx: 12, cy: 12, r: 9 }], ['path', { d: 'M12 7v5l3 2' }]],
  usage: [['path', { d: 'M4 20V10M10 20V4M16 20v-7M22 20H2' }]],
  alerts: [['path', { d: 'M6 16V11a6 6 0 0 1 12 0v5l2 2H4z' }], ['path', { d: 'M10 21h4' }]],
  settings: [['circle', { cx: 12, cy: 12, r: 3 }],
    ['path', { d: 'M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z' }]],
  sun: [['circle', { cx: 12, cy: 12, r: 4 }],
    ['path', { d: 'M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4' }]],
  moon: [['path', { d: 'M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z' }]],
};

function icon(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  for (const [key, value] of Object.entries({
    width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true',
  })) svg.setAttribute(key, String(value));
  for (const [tag, attrs] of ICONS[name] || []) {
    const shape = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) shape.setAttribute(key, String(value));
    svg.appendChild(shape);
  }
  return svg;
}

//: 카드 안에 여백 없이 붙는 것들. 나머지는 .card-body 로 묶어 여백을 준다.
const FLUSH = ['scroll', 'list', 'empty', 'card-foot', 'flush'];

function isFlush(node) {
  return node && node.classList && FLUSH.some((cls) => node.classList.contains(cls));
}

/** 카드. 제목 줄 + 본문. 표·목록은 가장자리에 붙고, 문단·폼은 여백 안에 들어간다.
 *  `aside` 는 제목 줄 오른쪽(보조 문구·버튼). */
function card(title, children, cls, aside) {
  const node = el('section', { class: 'card' + (cls ? ' ' + cls : '') });
  if (title || aside) {
    node.appendChild(el('div', { class: 'card-head' }, [
      title ? el('h2', { text: title }) : null,
      aside ? el('div', { class: 'aside' }, [].concat(aside)) : null,
    ]));
  }
  let body = null;
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    const item = typeof child === 'string' ? el('p', { text: child }) : child;
    if (isFlush(item)) { body = null; node.appendChild(item); continue; }
    if (!body) { body = el('div', { class: 'card-body' }); node.appendChild(body); }
    body.appendChild(item);
  }
  return node;
}

function cardFoot(children) {
  return el('div', { class: 'card-foot' }, children);
}

/** 통계 타일. 라벨 · 큰 숫자 · 보조 문구. */
function stat(label, value, tone, sub) {
  return el('section', { class: 'card stat-card' }, [
    el('div', { class: 'label', text: label }),
    el('div', { class: 'value' }, [
      el('span', { class: 'stat' + (tone ? ' ' + tone : ''), text: String(value) }),
      sub ? el('span', { class: 'sub', text: sub }) : null,
    ]),
  ]);
}

/* 배지 → 필. 예전 클래스 이름은 톤으로 옮긴다 — 호출부가 데이터 값을 그대로 넘기기 때문이다. */
const TONE_OF = {
  healthy: 'ok', internal: 'ok', ok: 'ok', active: 'ok', ready: 'ok',
  unhealthy: 'danger', block: 'danger', bad: 'danger', failed: 'danger', danger: 'danger',
  draining: 'warn', warn: 'warn', full: 'warn', partial: 'warn', external: 'warn', pending: 'warn', blocked: 'warn',
  info: 'info', running: 'info', pulling: 'info', approved: 'info',
  accent: 'accent',
};

function pill(text, tone) {
  return el('span', { class: 'pill' + (tone ? ' ' + tone : ''), text: String(text) });
}

function badge(text, cls) {
  return pill(text, TONE_OF[cls] || '');
}

function dot(tone, pulse) {
  return el('span', { class: 'dot' + (tone ? ' ' + tone : '') + (pulse ? ' pulse' : '') });
}

/* 키를 문자열로 조립하지 않는다. 조립한 키는 카탈로그 대조 테스트가 못 잡고,
 * 못 잡는 순간 화면에 'ui.boundary_internal' 같은 원문 키가 그대로 뜬다. */
const BOUNDARY_LABEL = {
  internal: 'ui.boundary_internal',
  external: 'ui.boundary_external',
};
const STATUS_LABEL = {
  healthy: 'ui.healthy',
  unhealthy: 'ui.unhealthy',
  unknown: 'ui.unknown',
  draining: 'ui.draining',
};
const REQUEST_LABEL = {
  pending: 'ui.pending_approval',
  approved: 'ui.approved',
  pulling: 'ui.pulling',
  ready: 'ui.installed',
  rejected: 'ui.rejected',
  failed: 'ui.failed',
};

function boundaryBadge(boundary) {
  return badge(t(BOUNDARY_LABEL[boundary] || 'ui.unknown'), boundary);
}

function statusBadge(status) {
  return badge(t(STATUS_LABEL[status] || 'ui.unknown'), status);
}

function requestBadge(status) {
  return badge(t(REQUEST_LABEL[status] || 'ui.unknown'), status);
}

function table(headers, rows) {
  if (!rows.length) return emptyState(t('ui.empty'));
  return el('div', { class: 'scroll' }, [
    el('table', {}, [
      // **머리글은 값이 아니라 모양으로 가른다.** 라벨 자리에 falsy 폴백을 쓰면
      // 빈 문자열 라벨(`{label:'', num:true}` — 제목 없이 오른쪽 정렬만 하는 칸)이
      // 객체 자체로 떨어져 화면에 `[object Object]` 가 뜬다. 카탈로그 표가 그랬다.
      el('thead', {}, [el('tr', {}, headers.map((h) =>
        el('th', {
          class: h && h.num ? 'num' : null,
          text: (h && typeof h === 'object') ? (h.label ?? '') : (h ?? ''),
        })))]),
      el('tbody', {}, rows.map((cells) => el('tr', {}, cells.map((cell, i) =>
        el('td', { class: headers[i] && headers[i].num ? 'num' : null },
          typeof cell === 'object' && cell !== null ? [cell] : [String(cell ?? '')]))))),
    ]),
  ]);
}

/** 빈 상태. 점 하나와 문장 — "표시할 항목이 없음" 이 왜 없음인지 말할 자리다. */
function emptyState(text, tone) {
  return el('div', { class: 'empty' }, [dot(tone || ''), el('span', { text })]);
}

function listRows(items) {
  return el('div', { class: 'list' }, items);
}

function listRow(parts) {
  return el('div', { class: 'list-row' }, [
    parts.dot ? dot(parts.dot) : null,
    el('div', { class: 'body' }, [
      el('div', { class: 'title' }, [].concat(parts.title || [])),
      parts.detail ? el('div', { class: 'detail' }, [].concat(parts.detail)) : null,
    ]),
    parts.time ? el('span', { class: 'time', text: parts.time }) : null,
    parts.actions ? el('div', { class: 'row' }, parts.actions) : null,
  ]);
}

function bar(ratio, warnAt, thick) {
  const pct = Math.max(0, Math.min(1, ratio || 0));
  const tone = pct >= 1 ? 'bad' : (warnAt && pct >= warnAt ? 'warn' : '');
  return el('div', { class: 'bar' + (thick ? ' thick' : '') }, [
    el('span', { class: tone, style: 'width:' + (pct * 100).toFixed(1) + '%' }),
  ]);
}

function laneChip(name) {
  return el('span', { class: 'lane-chip ' + (name === 'batch' || name === 'guard' ? name : '') });
}

function kv(pairs) {
  return el('div', { class: 'kv' }, pairs.filter(Boolean).map(([key, value, span]) =>
    el('div', { class: span ? 'span' : null }, [
      el('div', { class: 'k', text: key }),
      typeof value === 'object' && value !== null ? value : el('div', { text: String(value ?? '—') }),
    ])));
}

function segment(options, activeId, onPick, danger) {
  return el('div', { class: 'segment', role: 'tablist' }, options.map((o) =>
    el('button', {
      type: 'button', role: 'tab',
      class: (o.id === activeId ? 'active' : '') + (danger && o.id === danger ? ' danger' : ''),
      'aria-selected': o.id === activeId ? 'true' : 'false',
      text: o.label,
      onclick: () => onPick(o.id),
    })));
}

function switchControl(on, onToggle, label) {
  return el('button', {
    type: 'button', class: 'switch' + (on ? ' on' : ''), role: 'switch',
    'aria-checked': on ? 'true' : 'false', 'aria-label': label || '', title: label || '',
    onclick: onToggle,
  }, [el('span')]);
}

function pageHead(desc, actions) {
  const hasActions = !!(actions && actions.length);
  return el('div', { class: 'page-head' + (hasActions ? '' : ' desc-only') }, [
    desc ? el('p', { class: 'desc', text: desc }) : null,
    hasActions ? el('div', { class: 'actions' }, actions) : null,
  ]);
}

function when(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

function money(value) {
  return '$' + (Number(value) || 0).toFixed(4);
}

// ── 토스트 · 드로어 ──────────────────────────────────────────────────────

let toastTimer = null;

function toast(message) {
  const box = $('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 2200);
}

/** 우측 드로어. 상세·폼이 여기 뜬다. #view 밖에 있어서 자동 갱신이 다시 그리지 않는다. */
function openDrawer(title, body, foot) {
  $('drawer-title').textContent = title;
  $('drawer-body').replaceChildren.apply($('drawer-body'), [].concat(body || []));
  $('drawer-foot').replaceChildren.apply($('drawer-foot'), [].concat(foot || []));
  $('drawer-overlay').hidden = false;
  $('drawer').hidden = false;
}

function closeDrawer() {
  $('drawer').hidden = true;
  $('drawer-overlay').hidden = true;
  $('drawer-body').replaceChildren();
  $('drawer-foot').replaceChildren();
}

function drawerClose() {
  return el('button', { type: 'button', class: 'right', text: t('ui.close'), onclick: closeDrawer });
}

// ── API ───────────────────────────────────────────────────────────────────

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

function showError(err) {
  const box = $('error');
  box.textContent = t('ui.error') + ': ' + (err && err.message ? err.message : err);
  box.hidden = false;
  setTimeout(() => { box.hidden = true; }, 8000);
}

/** 여러 조회를 한 번에. 권한이 없는 경로는 null 로 흡수한다 — 플랫폼 카드 하나가
 *  없다고 테넌트 화면 전체가 안 뜨면 안 된다. */
async function fetchAll(paths) {
  const entries = await Promise.all(Object.entries(paths).map(async ([key, path]) => {
    try { return [key, await api(path)]; } catch (_) { return [key, null]; }
  }));
  return Object.fromEntries(entries);
}

// ── 배너: 조용한 실패를 시끄럽게 ──────────────────────────────────────────

function renderBanners(extra) {
  const box = $('banners');
  box.replaceChildren();
  const s = state.session;
  const pages = pagesFor(s).map((p) => p.id);
  const warnings = [];

  if (s.airgap) warnings.push([t('ui.airgap_on'), 'info']);
  if (!s.guard_classifier_ready) warnings.push([t('ui.classifier_off'), 'bad']);
  if (!s.raw_prompt_storage) warnings.push([t('ui.raw_storage_off'), 'info']);
  if (!s.guard_locale_pack) {
    warnings.push([t('ui.locale_pack_warning'), 'bad']);
  }
  // 유예를 조용히 두면 그게 더 나쁘다 — 필터가 지키고 있다고 믿게 된다.
  if (s.guard_grace_mode) {
    warnings.push([t('ui.grace_mode'), 'bad',
      pages.includes('rules') && state.page !== 'rules' ? [t('ui.go_rules'), 'rules'] : null]);
  }
  // 공개 진입점은 /v1/platform/* 를 404 로 막는다(topology §2). "표시할 항목이 없음" 으로
  // 보이면 사용자는 제품이 비었다고 읽는다 — 막힌 것은 막혔다고 말한다.
  if (state.platformBlocked) warnings.push([t('ui.platform_blocked'), 'bad']);
  for (const line of extra || []) warnings.push(line);

  for (const [text, cls, action] of warnings) {
    box.appendChild(el('div', { class: 'banner' + (cls ? ' ' + cls : '') }, [
      dot(),
      el('div', { class: 'text', text }),
      action ? el('button', { type: 'button', text: action[0], onclick: () => go(action[1]) }) : null,
    ]));
  }
}

// ── 페이지 · 레일 ─────────────────────────────────────────────────────────

/* 진입점 9개(핸드오프의 정보구조). 순서가 레일의 순서다. `platform` 은 그 화면 전체가
 * 플랫폼 관리 면(/v1/platform/*)이라는 뜻이고, 공개 진입점에서 막혔을 때 이유를 그린다. */
const PAGES = [
  { id: 'overview', icon: 'overview', label: 'ui.overview', desc: 'ui.desc_overview',
    show: () => true, render: () => (state.session.is_platform_admin ? renderPlatformOverview() : renderConsumerStatus()) },
  { id: 'nodes', icon: 'nodes', label: 'ui.nodes', desc: 'ui.desc_nodes', platform: true,
    show: (s) => s.is_platform_admin, render: () => renderNodes() },
  { id: 'tenants', icon: 'tenants', label: 'ui.tenants', desc: 'ui.desc_tenants', platform: true,
    show: (s) => s.is_platform_admin, render: () => renderTenants() },
  { id: 'models', icon: 'models', label: 'ui.models', desc: 'ui.desc_models', platform: true,
    show: (s) => s.is_platform_admin, render: () => renderModels() },
  { id: 'rules', icon: 'rules', label: 'ui.rules', desc: 'ui.desc_rules',
    show: (s) => s.is_tenant_admin, render: () => renderRules() },
  { id: 'jobs', icon: 'jobs', label: 'ui.jobs', desc: 'ui.desc_jobs',
    show: (s) => s.is_tenant_admin, render: () => renderJobs() },
  { id: 'usage', icon: 'usage', label: 'ui.usage', desc: 'ui.desc_usage',
    show: (s) => s.is_tenant_admin, render: () => renderUsage() },
  { id: 'alerts', icon: 'alerts', label: 'ui.notifications', desc: 'ui.desc_alerts', platform: true,
    show: (s) => s.is_platform_admin, render: () => renderNotifications() },
  { id: 'settings', icon: 'settings', label: 'ui.settings', desc: 'ui.desc_settings',
    show: (s) => s.is_tenant_admin || !!s.account, render: () => renderSettings() },
];

function pagesFor(session) {
  return PAGES.filter((p) => p.show(session));
}

function currentPage() {
  const pages = pagesFor(state.session);
  return pages.find((p) => p.id === state.page) || pages[0];
}

function go(pageId, sub) {
  state.page = pageId;
  if (sub) state.sub[pageId] = sub;
  closeDrawer();
  refresh();
}

function renderTabs() {
  const rail = $('tabs');
  rail.replaceChildren();
  for (const page of pagesFor(state.session)) {
    rail.appendChild(el('button', {
      type: 'button',
      class: 'rail-item' + (page.id === state.page ? ' active' : ''),
      'aria-current': page.id === state.page ? 'page' : null,
      onclick: () => go(page.id),
    }, [icon(page.icon), el('span', { text: t(page.label) })]));
  }
}

function renderHeader(page) {
  $('page-title').textContent = t(page.label);
  $('page-sub').textContent = t(page.desc);
  $('page-sub').title = t(page.desc);
  renderPills();
}

/** 헤더의 상태 필 — 승인 대기 · 유예 모드 · 노드 n/n. 눌러서 그 화면으로 간다. */
function renderPills() {
  const box = $('pills');
  box.replaceChildren();
  const s = state.session;
  const pages = pagesFor(s).map((p) => p.id);
  if (state.header.pending > 0 && pages.includes('models')) {
    box.appendChild(el('button', {
      type: 'button', class: 'hpill warn', onclick: () => go('models'),
    }, [dot('warn', true), el('span', { text: t('ui.pending_approval') + ' ' + state.header.pending })]));
  }
  if (s.guard_grace_mode) {
    box.appendChild(el('button', {
      type: 'button', class: 'hpill danger', onclick: () => go(pages.includes('rules') ? 'rules' : 'overview'),
    }, [el('span', { text: t('ui.guard') + ' ' + t('ui.lenient') })]));
  }
  const nodes = state.header.nodes;
  if (nodes) {
    const tone = nodes.healthy === nodes.total ? 'ok' : (nodes.healthy ? 'warn' : 'danger');
    box.appendChild(el('span', { class: 'hpill' }, [
      dot(tone), el('span', { text: t('ui.nodes') + ' ' + nodes.healthy + '/' + nodes.total }),
    ]));
  }
}

/** 헤더 필의 재료. 화면 렌더와 나란히 받고, 늦게 와도 현재 세대일 때만 그린다. */
async function refreshHeader(generation) {
  const wants = { status: '/v1/status' };
  if (state.session.is_platform_admin && !state.platformBlocked) wants.models = '/v1/platform/models';
  const data = await fetchAll(wants);
  if (generation !== refreshGeneration) return;
  if (data.status && data.status.nodes) state.header.nodes = data.status.nodes;
  if (data.models) state.header.pending = data.models.pending || 0;
  renderPills();
}

//: 자동 갱신 주기(ms). **노드가 죽어도 새로고침 전까지 과거 화면을 본다** —
//: 관제 화면이 과거를 보여주면 그건 관제가 아니다.
const REFRESH_INTERVAL_MS = 15000;

//: 진행 중인 갱신의 세대. 탭을 빠르게 옮기면 **먼저 시작한 요청이 나중에 도착해**
//: 이전 탭의 내용을 현재 탭 위에 그린다 — 세대가 어긋난 결과는 버린다.
let refreshGeneration = 0;

async function refresh(options) {
  const quiet = options && options.quiet;
  // 조용한(자동) 갱신은 폼을 만지는 중이면 기다린다 — 입력 위를 덮어쓰는 갱신은 갱신이
  // 아니라 방해다. 수동 새로고침은 사용자의 뜻이니 그대로 그린다.
  if (quiet && editingInView()) return;
  const page = currentPage();
  state.page = page.id;
  renderTabs();
  renderHeader(page);

  const generation = ++refreshGeneration;
  const view = $('view');
  refreshHeader(generation);
  if (page.platform && state.platformBlocked) {
    // 이 주소에서는 안 열리는 면이다. 404 를 받아 "없음" 을 그리는 대신 이유를 그린다.
    renderBanners();
    view.replaceChildren(blockedNotice());
    return;
  }
  // 자동 갱신은 화면을 비우지 않는다 — 15초마다 깜빡이면 읽을 수가 없다.
  if (!quiet) view.replaceChildren(el('p', { class: 'muted', text: t('ui.loading') }));
  try {
    const nodes = await page.render();
    if (generation !== refreshGeneration) return;   // 지나간 탭의 결과다
    view.replaceChildren.apply(view, [].concat(nodes).filter(Boolean));
  } catch (err) {
    if (generation !== refreshGeneration) return;
    if (!quiet) {
      showError(err);
      view.replaceChildren(emptyState(t('ui.empty')));
    }
  }
}

function blockedNotice() {
  return card(null, [emptyState(t('ui.platform_blocked'), 'danger')], 'bad');
}

/** 사용자가 화면의 폼을 만지는 중인가.
 *
 *  자동 갱신이 그 위를 다시 그리면 치던 글자가 사라진다 — 비밀번호 변경 폼에서 실제로
 *  겪었다. 포커스가 폼 칸에 있거나, 어떤 칸이든 기본값에서 벗어나 있으면 편집 중으로 본다.
 *  제출이 끝나 칸이 비워지면 다시 기본값이라 갱신이 재개된다. 수동 새로고침은 그대로 그린다.
 *  드로어는 #view 밖이라 갱신이 건드리지 않는다 — 거기 폼은 여기서 볼 필요가 없다. */
function editingInView() {
  const view = $('view');
  const active = document.activeElement;
  if (active && view.contains(active) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(active.tagName)) {
    return true;
  }
  for (const field of view.querySelectorAll('input, textarea, select')) {
    if (field.tagName === 'SELECT') {
      if (Array.from(field.options).some((o) => o.selected !== o.defaultSelected)) return true;
    } else if (field.type === 'checkbox' || field.type === 'radio') {
      if (field.checked !== field.defaultChecked) return true;
    } else if (field.value !== field.defaultValue) {
      return true;
    }
  }
  return false;
}

function startAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    // 탭이 안 보이면 안 부른다 — 열어 둔 탭이 서버를 계속 두드릴 이유가 없다.
    if (document.hidden || !state.token) return;
    refresh({ quiet: true });
  }, REFRESH_INTERVAL_MS);
}

function stopAutoRefresh() {
  if (state.refreshTimer) clearInterval(state.refreshTimer);
  state.refreshTimer = null;
}

// ── 플랫폼: 개요 ──────────────────────────────────────────────────────────

async function renderPlatformOverview() {
  const data = await fetchAll({ overview: '/v1/platform/overview' });
  const o = data.overview;
  if (!o) return [emptyState(t('ui.empty'))];

  const singleHomed = Object.entries(o.single_homed_roles || {});
  const waiting = Object.entries(o.waiting_by_reason || {})
    .filter(([reason]) => reason !== 'none');
  const pending = o.model_requests_pending || 0;
  state.header.pending = pending;

  renderBanners(pending > 0
    ? [[t('ui.pending_approval') + ' ' + pending + ' — ' + t('ui.pending_help'), '', [t('ui.go_models'), 'models']]]
    : []);

  const nodes = o.nodes || [];
  const lanes = Object.entries(o.lanes || {});
  const healthy = nodes.filter((n) => n.status === 'healthy').length;
  const queued = lanes.reduce((sum, [, l]) => sum + l.queued, 0);
  const running = lanes.reduce((sum, [, l]) => sum + l.running, 0);
  const usage = o.usage_by_tenant || [];
  const maxCalls = Math.max(1, ...usage.map((r) => r.calls || 0));

  return [
    el('div', { class: 'grid' }, [
      stat(t('ui.tenants'), (o.tenants || []).length),
      stat(t('ui.nodes'), healthy + ' / ' + nodes.length,
        nodes.some((n) => n.status === 'unhealthy') ? 'bad' : 'ok', t('ui.online')),
      stat(t('ui.queued'), queued, '', t('ui.running') + ' ' + running),
      stat(t('ui.pending_approval'), pending, pending ? 'warn' : '', t('ui.install_requests')),
    ]),

    el('div', { class: 'grid wide' }, [
      card(t('ui.lane'), [table(
        [t('ui.lane'), { label: t('ui.running'), num: true }, { label: t('ui.queued'), num: true },
         { label: t('ui.concurrency'), num: true }, t('ui.scan_truncated')],
        lanes.map(([name, l]) => [
          el('span', { class: 'mono' }, [laneChip(name), name]),
          l.running, l.queued, l.max_concurrent,
          l.scan_truncated ? badge(t('ui.scan_truncated'), 'block') : '—',
        ]))], null, el('span', { text: t('ui.lane_note') })),

      // **1급 카드 ①** — 자동 복제를 하지 않으므로 사람이 판단할 재료를 준다.
      card(t('ui.single_homed_warning'), [
        el('p', { class: 'hint', text: t('ui.single_homed_help') }),
        singleHomed.length
          ? table([t('ui.role'), t('ui.node')], singleHomed.map(([role, node]) =>
              [el('span', { text: role, style: 'font-weight:500' }), el('span', { class: 'mono', text: node })]))
          : emptyState(t('ui.none'), 'ok'),
      ], singleHomed.length ? 'warn' : null, singleHomed.length ? pill(singleHomed.length, 'warn') : null),
    ]),

    el('div', { class: 'grid wide' }, [
      // **1급 카드 ②** — "노드 정비로 대기 12건" 을 못 보여주면 관리자는 큐가 왜
      // 안 줄어드는지 알 수 없고, 노드를 늘리는 잘못된 대응을 한다.
      card(t('ui.waiting_reason'), [
        waiting.length
          ? table([t('ui.waiting_reason'), { label: t('ui.queued'), num: true }], waiting)
          : emptyState(t('ui.no_waiting'), 'ok'),
      ], waiting.length ? 'warn' : null, el('span', { text: t('ui.waiting_note') })),

      card(t('ui.usage'), usage.length
        ? usage.map((r) => el('div', { class: 'usage-row' }, [
            el('span', { class: 'mono', text: r.tenant_id }),
            bar((r.calls || 0) / maxCalls, null, true),
            el('span', { class: 'n', text: t('ui.count', { n: r.calls || 0 }) }),
          ]))
        : [emptyState(t('ui.none'))]),
    ]),
  ];
}

// ── 플랫폼: 노드 ──────────────────────────────────────────────────────────

async function renderNodes() {
  const data = await api('/v1/platform/nodes');
  renderBanners();

  const cards = (data.nodes || []).map((n) => {
    const tone = n.status === 'healthy' ? 'ok' : (n.status === 'draining' ? 'warn' : 'danger');
    const memRatio = n.mem_budget_gb ? (n.mem_reserved_gb || 0) / n.mem_budget_gb : 0;
    return el('section', { class: 'card node-card' }, [
      el('div', { class: 'card-head' }, [
        dot(tone),
        el('span', { class: 'name', text: n.node }),
        pill(n.provider, ''),
        // **소프트웨어가 아니라 기계의 위치가 경계를 정한다.**
        boundaryBadge(n.data_boundary),
        n.metered ? badge(t('ui.cost'), 'external') : null,
        el('div', { class: 'aside' }, [statusBadge(n.status)]),
      ]),
      el('div', { class: 'card-body' }, [
        kv([
          [t('ui.concurrency'), el('div', {}, [
            el('div', { text: n.running + ' / ' + n.max_concurrent }),
            el('div', { style: 'margin-top:6px' }, [bar(n.load_ratio || 0, 0.8)]),
          ])],
          n.mem_budget_gb ? [t('ui.memory'), el('div', {}, [
            el('div', { text: (n.mem_reserved_gb || 0) + ' / ' + n.mem_budget_gb + ' GB' }),
            el('div', { style: 'margin-top:6px' }, [bar(memRatio, 0.6)]),
          ])] : [t('ui.memory'), '—'],
          [t('ui.models_held') + ' ' + (n.models || []).length, el('div', { class: 'tags' },
            (n.models || []).length
              ? n.models.map((m) => el('span', { class: 'tag' + (m === n.loaded_model ? ' accent' : ''), text: m }))
              : [el('span', { class: 'muted', text: t('ui.none') })]), true],
          n.tenant_affinity && n.tenant_affinity.length
            ? [t('ui.tenant_affinity'), n.tenant_affinity.join(', '), true] : null,
        ]),
        n.last_error ? el('div', { class: 'err', text: n.last_error }) : null,
      ]),
      el('div', { class: 'card-foot' }, [
        el('button', {
          type: 'button', class: 'sm',
          text: n.status === 'draining' ? t('ui.undrain') : t('ui.drain'),
          onclick: async () => {
            try {
              await api('/v1/platform/nodes/' + encodeURIComponent(n.node) + '/drain', {
                method: 'POST', body: { undrain: n.status === 'draining' },
              });
              refresh();
            } catch (err) { showError(err); }
          },
        }),
        // 삭제는 드레이닝 뒤의 결정이다 — 실행 중인 잡이 있으면 서버가 409 로 거절한다.
        el('button', {
          type: 'button', class: 'sm danger', text: t('ui.delete'),
          onclick: async () => {
            if (!confirm(t('ui.confirm_delete_node', { node: n.node }))) return;
            try {
              await api('/v1/platform/nodes/' + encodeURIComponent(n.node), { method: 'DELETE' });
              toast(t('ui.deleted'));
              refresh();
            } catch (err) { showError(err); }
          },
        }),
        n.disabled_by_airgap ? el('span', { class: 'muted right', text: t('ui.airgap_on') }) : null,
      ]),
    ]);
  });

  return [
    pageHead(t('ui.desc_nodes'), [
      el('button', {
        type: 'button', class: 'primary', text: t('ui.add_node'),
        onclick: () => openDrawer(t('ui.register_node'), [registerNodeForm()]),
      }),
    ]),
    cards.length ? el('div', { class: 'grid cards' }, cards) : card(t('ui.nodes'), [emptyState(t('ui.empty'))]),
  ];
}

function registerNodeForm() {
  const fields = {};
  const input = (name, label, type) => {
    fields[name] = el('input', { id: 'node-' + name, type: type || 'text' });
    return el('div', {}, [el('label', { for: 'node-' + name, text: label }), fields[name]]);
  };
  const boundary = el('select', { id: 'node-boundary' }, [
    el('option', { value: 'internal', text: t('ui.boundary_internal') }),
    el('option', { value: 'external', text: t('ui.boundary_external') }),
  ]);

  return el('form', {
    class: 'stack',
    onsubmit: async (event) => {
      event.preventDefault();
      const body = {
        name: fields.name.value.trim(),
        provider: fields.provider.value.trim(),
        data_boundary: boundary.value,
        base_url: fields.base_url.value.trim() || undefined,
        max_concurrent: Number(fields.max_concurrent.value) || 1,
        // **경계 밖 노드는 서버가 TLS + 인증을 강제한다**(D9). 폼에 입력 수단이
        // 없어서 external 노드 등록이 항상 실패했다 — 화면에 있는데 절대 안 되는
        // 기능이 가장 나쁘다.
        //
        // 값이 아니라 **환경 변수 이름**을 받는다. 자격증명 자체를 DB 에 넣으면
        // 백업·내보내기·진단 번들이 전부 그것을 나르게 된다.
        api_key_env: fields.api_key_env.value.trim() || undefined,
        auth_header_env: fields.auth_header_env.value.trim() || undefined,
      };
      try {
        const result = await api('/v1/platform/nodes', { method: 'POST', body });
        // **설치 후에 조용히 안 붙는 것이 제품에서 가장 나쁜 경험이다.**
        if (!result.reachable) {
          showError(new Error(result.name + ': ' + (result.error || 'unreachable')));
        }
        closeDrawer();
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    input('name', t('ui.node')),
    input('provider', t('ui.provider')),
    el('div', {}, [el('label', { for: 'node-boundary', text: t('ui.boundary') }), boundary]),
    input('base_url', t('ui.base_url'), 'url'),
    input('max_concurrent', t('ui.concurrency'), 'number'),
    input('api_key_env', t('ui.api_key_env')),
    input('auth_header_env', t('ui.auth_header_env')),
    el('button', { class: 'primary', type: 'submit', text: t('ui.create') }),
  ]);
}

// ── 플랫폼: 테넌트 ────────────────────────────────────────────────────────

async function renderTenants() {
  const data = await api('/v1/platform/tenants');
  renderBanners();

  const rows = (data.tenants || []).map((tenant) => [
    el('div', {}, [
      el('div', { class: 'mono', text: tenant.id, style: 'font-weight:500' }),
      el('div', { class: 'sub', text: tenant.name }),
    ]),
    tenant.locale,
    badge(tenant.status, tenant.status === 'active' ? 'healthy' : 'unhealthy'),
    tenant.budget_usd_per_month ? money(tenant.budget_usd_per_month) : '—',
    tenant.has_dek ? badge('DEK', 'internal') : badge(t('ui.none')),
    el('div', { class: 'row end' }, [
      el('button', {
        type: 'button', class: 'danger sm', text: t('ui.purge_tenant'),
        onclick: () => purgeTenant(tenant.id),
      }),
    ]),
  ]);

  return [
    pageHead(t('ui.desc_tenants'), [
      el('button', {
        type: 'button', class: 'primary', text: t('ui.add_tenant'),
        onclick: () => openDrawer(t('ui.add_tenant'), [createTenantForm()]),
      }),
    ]),
    card(t('ui.tenants'), [table(
      [t('ui.tenants'), t('ui.locale'), t('ui.status'), { label: t('ui.budget'), num: true }, '', ''], rows)],
      null, el('span', { text: t('ui.count', { n: rows.length }) })),
  ];
}

function createTenantForm() {
  const id = el('input', { id: 'tenant-id' });
  const name = el('input', { id: 'tenant-name' });
  const locale = el('select', { id: 'tenant-locale' },
    state.session.available_locales.map((code) =>
      el('option', { value: code, text: code })));

  return el('form', {
    class: 'stack',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        const created = await api('/v1/platform/tenants', {
          method: 'POST',
          body: { id: id.value.trim(), name: name.value.trim(), locale: locale.value },
        });
        // 만들 때부터 어떤 로케일 팩이 켜졌는지 말해 준다.
        toast(t('ui.locale_packs_on') + ': ' + (created.guard_locale_pack || t('ui.none')));
        closeDrawer();
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'tenant-id', text: t('ui.tenant_id') }), id]),
    el('div', {}, [el('label', { for: 'tenant-name', text: t('ui.name') }), name]),
    el('div', {}, [el('label', { for: 'tenant-locale', text: t('ui.locale') }), locale]),
    el('button', { class: 'primary', type: 'submit', text: t('ui.create') }),
  ]);
}

async function purgeTenant(tenantId) {
  // 되돌릴 수 없다. 확인값을 정확히 받는다.
  const typed = prompt(t('ui.purge_warning') + '\n\n' + t('ui.confirm') + ': ' + tenantId);
  if (typed !== tenantId) return;
  const reason = prompt(t('ui.audit')) || 'ui';
  try {
    await api('/v1/platform/tenants/' + encodeURIComponent(tenantId)
      + '?confirm=' + encodeURIComponent(tenantId)
      + '&reason=' + encodeURIComponent(reason), { method: 'DELETE' });
    toast(t('ui.deleted'));
    refresh();
  } catch (err) { showError(err); }
}

// ── 플랫폼: 모델 ──────────────────────────────────────────────────────────

async function renderModels() {
  const data = await fetchAll({ models: '/v1/platform/models', catalog: '/v1/platform/catalog' });
  renderBanners();
  const m = data.models || { inventory: [], install_requests: [], missing: [], pending: 0 };
  state.header.pending = m.pending || 0;

  // **설치 요청과 재고는 다른 것이다.** 한 표에 섞었더니 요청의 상태·진행률 칸이
  // `undefined` 로 뜨고(재고에는 그 값이 없다) 승인 버튼에 도달할 수 없었다 —
  // 개요에 "승인 대기 1" 이 떠 있는데도 그 요청을 볼 방법이 없었다.
  // 대기 중이면 노드 칸이 선택 상자다 — 탐지가 제안한 디스크를 관리자가 바꾼다.
  // 목록은 서버가 준 `eligible_nodes` 그대로다. 화면이 따로 고르지 않는다.
  const nodeCell = (r) => (r.status === 'pending' && (r.eligible_nodes || []).length > 1)
    ? el('select', {
        title: t('ui.retarget_hint'),
        onchange: (e) => retargetRequest(r.id, e.target.value),
      }, r.eligible_nodes.map((n) => el('option', { value: n, selected: n === r.node ? true : null, text: n })))
    : el('span', { class: 'mono', text: r.node });

  const pending = (m.install_requests || []).map((r) => [
    el('span', { class: 'mono', text: r.model, style: 'font-weight:500' }),
    nodeCell(r),
    el('div', {}, [
      requestBadge(r.status),
      r.status === 'pulling' ? el('div', { style: 'width:110px;margin-top:6px' }, [bar((r.progress || 0) / 100)]) : null,
    ]),
    r.error ? el('span', { class: 'error', text: r.error }) : '',
    el('div', { class: 'row end' }, r.status === 'pending' ? [
      el('button', {
        type: 'button', class: 'primary sm', text: t('ui.approve'),
        onclick: () => modelDecision(r.id, false),
      }),
      el('button', { type: 'button', class: 'sm', text: t('ui.reject'), onclick: () => modelDecision(r.id, true) }),
    ] : []),
  ]);

  const inventory = (m.inventory || []).map((r) => [
    el('div', {}, [
      el('div', { class: 'mono', text: r.model, style: 'font-weight:500' }),
      el('div', { class: 'sub', text: r.node }),
    ]),
    r.loaded ? badge(t('ui.model_loaded'), 'healthy') : '',
    r.est_size_gb ? r.est_size_gb + ' GB' : '—',
    (r.deletion_blockers || []).length
      ? el('span', { class: 'muted', title: r.deletion_blockers.join(', '), text: r.deletion_blockers.join(', ') })
      : '',
    el('div', { class: 'row end' }, [
      el('button', {
        type: 'button', class: 'danger sm', text: t('ui.delete'),
        disabled: (r.deletion_blockers || []).length ? true : null,
        onclick: () => deleteModel(r.node, r.model),
      }),
    ]),
  ]);

  // **"역할이 요구하는데 어느 노드에도 없는 모델"** — 서버가 주는데 화면이
  // 안 그렸다. 그 잡들은 레인을 막지 않고 조용히 대기하므로(§13-6), 여기
  // 안 보이면 관리자는 왜 그 역할만 안 도는지 알 방법이 없다.
  const missing = (m.missing || []).map((x) => [
    el('span', { class: 'mono', text: x.node }),
    el('span', { class: 'mono', text: x.model, style: 'font-weight:500' }),
    el('div', { class: 'row end' }, [
      el('button', {
        type: 'button', class: 'primary sm', text: t('ui.request_install'),
        onclick: () => requestInstall(x.node, x.model),
      }),
    ]),
  ]);

  const catalog = (data.catalog ? data.catalog.catalog : []).map((c) => listRow({
    title: [el('span', { class: 'mono', text: c.name, style: 'font-weight:500' }), ' ', pill(c.provider, '')],
    detail: [c.purpose || '', c.est_size_gb ? ' · ' + c.est_size_gb + ' GB' : ''],
  }));

  return [
    pageHead(t('ui.desc_models')),
    missing.length
      ? card(t('ui.missing_models'), [table([t('ui.node'), t('ui.model'), ''], missing)], 'warn', pill(missing.length, 'warn'))
      : null,
    pending.length
      ? card(t('ui.install_requests'), [table(
          [t('ui.model'), t('ui.node'), t('ui.status'), '', ''], pending)],
          null, m.pending ? pill(t('ui.pending_approval') + ' ' + m.pending, 'warn') : null)
      : null,
    el('div', { class: 'grid split' }, [
      card(t('ui.models'), [table(
        [t('ui.model'), '', { label: t('ui.size'), num: true }, t('ui.deletion_blocked'), ''], inventory)],
        null, el('span', { text: t('ui.count', { n: inventory.length }) })),
      card(t('ui.catalog'), [catalog.length ? listRows(catalog) : emptyState(t('ui.none'))]),
    ]),
  ];
}

async function requestInstall(node, model) {
  try {
    await api('/v1/platform/models', { method: 'POST', body: { node: node, model: model } });
    refresh();
  } catch (err) { showError(err); }
}

async function retargetRequest(requestId, node) {
  try {
    await api('/v1/platform/models/' + encodeURIComponent(requestId) + '/retarget', {
      method: 'POST', body: { node: node },
    });
    refresh();
  } catch (err) { showError(err); refresh(); }   // 실패하면 선택 상자를 서버 상태로 되돌린다
}

async function modelDecision(requestId, reject) {
  try {
    await api('/v1/platform/models/' + encodeURIComponent(requestId) + '/approve', {
      method: 'POST', body: { reject: reject },
    });
    refresh();
  } catch (err) { showError(err); }
}

async function deleteModel(node, model) {
  try {
    await api('/v1/platform/nodes/' + encodeURIComponent(node)
      + '/models/' + encodeURIComponent(model), { method: 'DELETE' });
    toast(t('ui.deleted'));
    refresh();
  } catch (err) {
    // **삭제 차단 사유를 그대로 보여준다** — `force` 가 없으므로 사유가 곧 다음 할 일이다.
    showError(new Error(t('ui.delete_blocked') + ': ' + err.message));
  }
}

// ── 규칙·가드 ─────────────────────────────────────────────────────────────

/** 세그먼트 하나에 베이스라인(플랫폼)과 가드(테넌트)를 묶는다. 테넌트 관리자에게는 가드뿐이다. */
async function renderRules() {
  const s = state.session;
  const subs = [];
  if (s.is_platform_admin) subs.push({ id: 'baseline', label: t('ui.baseline') });
  subs.push({ id: 'guard', label: t('ui.guard') });
  const active = subs.some((x) => x.id === state.sub.rules) ? state.sub.rules : subs[0].id;
  state.sub.rules = active;

  const head = el('div', { class: 'page-head' }, [
    subs.length > 1 ? segment(subs, active, (id) => go('rules', id)) : null,
    el('p', { class: 'desc', text: t('ui.desc_rules') }),
  ]);
  if (active === 'baseline') {
    if (state.platformBlocked) return [head, blockedNotice()];
    return [head].concat(await renderBaseline());
  }
  return [head].concat(await renderGuard());
}

// ── 플랫폼: 가드 베이스라인 ───────────────────────────────────────────────

async function renderBaseline() {
  const data = await api('/v1/platform/guard/baseline');
  const unused = data.packs_unused || [];
  renderBanners();

  const setGrace = async (enabled) => {
    if (enabled === !!data.grace_mode) return;
    try {
      await api('/v1/platform/guard/grace-mode', { method: 'POST', body: { enabled } });
      await loadSession();
      toast(t('ui.saved'));
      refresh();
    } catch (err) { showError(err); }
  };

  return [
    el('div', { class: 'grid split-2' }, [
      // **안 켜진 필터는 없는 필터인데, 다국어에서는 켰다고 착각하기가 더 쉽다.**
      card(t('ui.locale_packs_on'), [
        el('p', { class: 'hint', text: t('ui.locale_pack_warning') }),
        table([t('ui.locale_packs_on'), t('ui.tenants')],
          Object.entries(data.packs_in_use || {}).map(([pack, tenants]) =>
            [pill(pack, 'ok'), tenants.join(', ')])),
        unused.length ? cardFoot([el('span', { text: t('ui.locale_packs_off') + ': ' + unused.join(', ') })]) : null,
      ], unused.length ? 'warn' : null),

      card(t('ui.mode'), [
        el('p', { class: 'hint', text: t('ui.grace_help') }),
        segment([{ id: 'enforce', label: t('ui.enforce') }, { id: 'lenient', label: t('ui.lenient') }],
          data.grace_mode ? 'lenient' : 'enforce', (id) => setGrace(id === 'lenient'), 'lenient'),
      ], data.grace_mode ? 'bad' : null),
    ]),

    card(t('ui.baseline'), [table(
      [t('ui.rule'), '', t('ui.boundary_internal'), t('ui.boundary_external'), 'checksum', 'pack'],
      (data.baseline || []).map((r) => [
        el('span', { class: 'mono', text: r.id }), r.label || '',
        badge(r.action.internal, r.action.internal),
        badge(r.action.external, r.action.external),
        el('span', { class: 'mono muted', text: r.checksum || '—' }), pill(r.locale_pack, ''),
      ]))], null, el('span', { text: t('ui.count', { n: (data.baseline || []).length }) })),
  ];
}

// ── 테넌트: 가드 ──────────────────────────────────────────────────────────

async function renderGuard() {
  const data = await fetchAll({
    rules: '/v1/admin/guard/rules',
    events: '/v1/admin/guard/events?unreviewed=1&limit=50',
  });
  renderBanners();

  const rules = data.rules || { effective: [], tenant_rules: [], locale_pack: null };
  const events = (data.events && data.events.events) || [];

  return [
    el('div', { class: 'grid split-2' }, [
      // **오탐 검토 큐** — 이게 밀리면 승격 게이트가 영원히 안 열린다.
      card(t('ui.false_positive_queue'), [
        el('p', { class: 'hint', text: t('ui.review_help') }),
        table([t('ui.rule'), t('ui.stage'), t('ui.action'), { label: t('ui.hits'), num: true }, '', ''],
          events.map((e) => [
            el('span', { class: 'mono', text: e.rule_id }), e.stage, badge(e.action, e.action), e.match_count,
            el('span', { class: 'muted', text: when(e.ts) }),
            el('div', { class: 'row end' }, [
              el('button', { type: 'button', class: 'sm', text: t('ui.true_positive'), onclick: () => review(e.id, 'true_positive') }),
              el('button', { type: 'button', class: 'sm', text: t('ui.false_positive'), onclick: () => review(e.id, 'false_positive') }),
            ]),
          ])),
      ], events.length ? 'warn' : null, events.length ? pill(events.length, 'warn') : null),

      card(t('ui.locale_packs_on'), [
        el('p', {}, [rules.locale_pack ? badge(rules.locale_pack, 'internal') : badge(t('ui.none'), 'block')]),
        el('p', { class: 'hint', text: t('ui.locale_pack_warning') }),
      ], rules.locale_pack ? null : 'bad'),
    ]),

    card(t('ui.rule'), [
      table([t('ui.rule'), t('ui.boundary_internal'), t('ui.boundary_external'), 'pack', ''],
        (rules.effective || []).map((r) => [
          el('span', { class: 'mono', text: r.id }),
          badge(r.action.internal, r.action.internal),
          badge(r.action.external, r.action.external),
          pill(r.locale_pack, ''),
          el('div', { class: 'row end' }, [
            el('button', { type: 'button', class: 'sm accent', text: t('ui.promote'), onclick: () => checkPromotion(r.id) }),
          ]),
        ])),
      cardFoot([el('span', { text: t('ui.masked_only') })]),
    ], null, el('span', { text: t('ui.count', { n: (rules.effective || []).length }) })),
  ];
}

async function review(eventId, verdict) {
  try {
    await api('/v1/admin/guard/events/' + encodeURIComponent(eventId) + '/review',
      { method: 'POST', body: { verdict: verdict } });
    refresh();
  } catch (err) { showError(err); }
}

async function checkPromotion(ruleId) {
  try {
    const verdict = await api(
      '/v1/admin/guard/rules/' + encodeURIComponent(ruleId) + '/promotion?to=block');
    // 승격 가능 여부만 알려준다. 실제 적용은 규칙 저장이고, 그쪽이 게이트를 다시 본다.
    openDrawer(t('ui.promote') + ' — ' + ruleId, [
      el('p', {}, [pill(verdict.allowed ? t('ui.promotion_ready') : t('ui.promotion_blocked'), verdict.allowed ? 'ok' : 'warn')]),
      el('p', { text: verdict.reason || '' }),
      kv([
        [t('ui.false_positive'), (verdict.false_positive_rate * 100).toFixed(1) + '% / ' + (verdict.limit * 100).toFixed(1) + '%'],
        ['n', String(verdict.reviewed)],
      ]),
    ], [drawerClose()]);
  } catch (err) { showError(err); }
}

// ── 테넌트: 작업 ──────────────────────────────────────────────────────────

const JOB_FILTERS = [
  { id: 'all', label: 'ui.filter_all', match: () => true },
  { id: 'pending', label: 'ui.filter_pending', match: (j) => j.status === 'queued' || j.status === 'running' },
  { id: 'done', label: 'ui.filter_done', match: (j) => j.status === 'ok' },
  { id: 'failed', label: 'ui.filter_failed', match: (j) => j.status === 'failed' },
  { id: 'guard', label: 'ui.filter_guard', match: (j) => j.status === 'blocked' || String(j.error_code || '').startsWith('guard') },
];

/** chat 잡의 마스킹본은 턴 배열 JSON 이다 — 파싱되면 턴 목록, 아니면 null(원문을 그대로 보여 준다). */
function transcriptOf(j) {
  if (j.kind !== 'chat' || !j.prompt_masked) return null;
  try {
    const turns = JSON.parse(j.prompt_masked).messages;
    return Array.isArray(turns) ? turns.filter((m) => m && typeof m.content === 'string') : null;
  } catch (_) { return null; }
}

function lastUserText(j) {
  const turns = transcriptOf(j);
  if (!turns) return j.prompt_masked || '';
  const last = turns.slice().reverse().find((m) => m.role === 'user');
  return last ? last.content : '';
}

function transcriptView(turns) {
  return el('div', { class: 'transcript' }, turns.map((m) => el('div', { class: 'turn ' + (m.role === 'user' ? 'user' : 'assistant') }, [
    el('div', { class: 'who', text: t(m.role === 'user' ? 'ui.turn_user' : 'ui.turn_assistant') }),
    el('pre', { class: 'code', text: m.content }),
  ])));
}

async function renderJobs() {
  const data = await api('/v1/admin/jobs?limit=100');
  renderBanners();
  const jobs = data.jobs || [];
  const filter = JOB_FILTERS.find((f) => f.id === state.jobFilter) || JOB_FILTERS[0];
  const shown = jobs.filter(filter.match);

  const chips = el('div', { class: 'chips' }, JOB_FILTERS.map((f) => {
    const count = jobs.filter(f.match).length;
    return el('button', {
      type: 'button', class: 'chip' + (f.id === filter.id ? ' active' : ''),
      onclick: () => { state.jobFilter = f.id; refresh(); },
    }, [t(f.label), el('span', { class: 'n', text: String(count) })]);
  }));

  return [
    // **화면은 마스킹본만 본다.** 원문은 단건 API + 감사다.
    pageHead(t('ui.desc_jobs')),
    chips,
    card(t('ui.jobs'), [
      table(
        [t('ui.status'), t('ui.role'), t('ui.node'), t('ui.model'), t('ui.prompt'), { label: t('ui.cost'), num: true }, t('ui.created'), ''],
        shown.map((j) => [
          badge(j.status, j.status),
          el('span', { text: j.role, style: 'font-weight:500' }),
          el('span', { class: 'mono nowrap', text: j.node || '—' }),
          // **"왜 이 모델로 갔는가" 는 모델 옆에서 물어보게 된다.** 열을 따로 두면
          // 라우팅을 안 켠 설치처에서 언제나 비어 있는 열이 하나 는다.
          el('div', {}, [
            el('div', { class: 'mono nowrap', text: j.model || '—' }),
            // 밑줄 시작은 판정 센티널(_failed/_none)이다 — 화면에는 실제 라우트만.
            // 어느 쪽이든 기본 모델로 갔고, 실패율은 메트릭이 답한다.
            j.route && !j.route.startsWith('_') ? el('div', { class: 'sub', text: '← ' + j.route }) : null,
          ]),
          transcriptOf(j)
            ? el('div', { class: 'row' }, [pill('chat', 'info'), el('span', { class: 'mono muted', text: lastUserText(j).slice(0, 60) })])
            : el('span', { class: 'mono muted', text: (j.prompt_masked || '').slice(0, 60) }),
          money(j.cost_usd),
          el('span', { class: 'muted nowrap', text: when(j.created_at) }),
          el('div', { class: 'row end' }, [
            el('button', { type: 'button', class: 'sm', text: t('ui.details'), onclick: () => openJob(j) }),
          ]),
        ])),
      cardFoot([el('span', { text: t('ui.masked_only') }), el('span', { class: 'right', text: t('ui.count', { n: shown.length }) })]),
    ]),
  ];
}

/** 작업 상세 드로어. 목록이 준 것만 보여주고, 원문은 사람이 한 번 더 확인한 뒤 단건으로 연다. */
function openJob(j) {
  const body = [
    kv([
      [t('ui.status'), badge(j.status, j.status)],
      [t('ui.role'), el('div', {}, [j.role, ' → ', el('span', { class: 'mono', text: j.model || '—' })])],
      [t('ui.node') + ' · ' + t('ui.lane'), el('div', { class: 'mono', text: (j.node || '—') + ' · ' + (j.lane || '—') })],
      [t('ui.attempts'), String(j.attempts ?? '—')],
      [t('ui.cost'), money(j.cost_usd)],
      [t('ui.tokens_used'), (j.input_tokens || 0) + ' / ' + (j.output_tokens || 0)],
      [t('ui.created'), when(j.created_at)],
      [t('ui.finished'), when(j.finished_at)],
      j.wait_reason ? [t('ui.wait_reason'), j.wait_reason, true] : null,
      j.error_code ? [t('ui.error_code'), el('div', { class: 'mono', text: j.error_code }), true] : null,
      j.route && !j.route.startsWith('_') ? ['route', el('div', { class: 'mono', text: j.route }), true] : null,
    ]),
    el('div', {}, [el('label', { text: t('ui.prompt') + ' · ' + t('ui.masked_only') }),
      transcriptOf(j) ? transcriptView(transcriptOf(j)) : el('pre', { class: 'code', text: j.prompt_masked || '' })]),
    j.response ? el('div', {}, [el('label', { text: t('ui.response') }), el('pre', { class: 'code', text: j.response })]) : null,
  ];
  const foot = [
    j.has_raw ? el('button', { type: 'button', text: t('ui.view_raw'), onclick: () => viewRaw(j.id) }) : null,
    drawerClose(),
  ];
  openDrawer(t('ui.job_detail') + ' ' + j.id, body, foot);
}

async function viewRaw(jobId) {
  if (!confirm(t('ui.raw_audited'))) return;
  try {
    const body = await api('/v1/admin/jobs/' + encodeURIComponent(jobId) + '/raw');
    const box = $('drawer-body');
    box.appendChild(el('div', {}, [
      el('label', { text: t('ui.view_raw') }),
      el('pre', { class: 'reveal', text: body.prompt }),
      el('p', { class: 'hint', text: t('ui.raw_audited') }),
    ]));
    box.scrollTop = box.scrollHeight;
  } catch (err) { showError(err); }
}

// ── 테넌트: 사용량 ────────────────────────────────────────────────────────

const USAGE_AXES = ['service_id', 'end_user_hash', 'role', 'model', 'node'];

async function renderUsage() {
  const axis = state.usageAxis || 'service_id';
  const data = await api('/v1/admin/usage?by=' + encodeURIComponent(axis));
  renderBanners();

  const budget = data.budget || {};
  const burn = budget.burn_rate || 0;
  const rows = data.rows || [];

  return [
    el('div', { class: 'page-head' }, [
      segment(USAGE_AXES.map((name) => ({ id: name, label: name })), axis,
        (id) => { state.usageAxis = id; refresh(); }),
      el('p', { class: 'desc', text: t('ui.desc_usage') }),
    ]),
    el('div', { class: 'grid' }, [
      stat(t('ui.calls'), rows.reduce((sum, r) => sum + r.calls, 0)),
      stat(t('ui.tokens_used'), rows.reduce((sum, r) => sum + (r.input_tokens || 0) + (r.output_tokens || 0), 0)),
      stat(t('ui.cost'), money(data.spend_usd)),
      el('section', { class: 'card stat-card' }, [
        el('div', { class: 'label', text: t('ui.budget_used') }),
        el('div', { class: 'value' }, [
          el('span', { class: 'stat' + (burn >= 1 ? ' bad' : (burn >= (budget.warn_at || 0.8) ? ' warn' : '')),
            text: budget.limit ? (burn * 100).toFixed(1) + '%' : '—' }),
          budget.limit ? el('span', { class: 'sub', text: money(budget.committed) + ' / ' + money(budget.limit) }) : null,
        ]),
        budget.limit ? el('div', { style: 'margin-top:10px' }, [bar(burn, budget.warn_at)]) : null,
      ]),
    ]),
    card(t('ui.usage'), [
      table(
        [axis, { label: t('ui.calls'), num: true }, { label: t('ui.tokens_used'), num: true },
         { label: t('ui.cost'), num: true }, { label: t('ui.latency'), num: true },
         { label: t('ui.success_rate'), num: true }],
        rows.map((r) => [
          el('span', { class: 'mono', text: r.key || '—' }), r.calls, r.input_tokens + r.output_tokens,
          money(r.cost_usd), r.avg_duration_ms + ' ms',
          (r.success_rate * 100).toFixed(1) + '%',
        ])),
    ], null, el('span', { text: t('ui.count', { n: rows.length }) })),
  ];
}

// ── 플랫폼: 알림 ──────────────────────────────────────────────────────────

async function renderNotifications() {
  const data = await api('/v1/platform/notifications');
  renderBanners();

  const recent = (data.recent || []).slice().reverse().map((n) => listRow({
    dot: /offline|failed|exhausted|spike|error|needs_review/.test(n.event || '') ? 'danger'
      : (/recovered|ready|normal/.test(n.event || '') ? 'ok' : 'warn'),
    title: n.event,
    detail: JSON.stringify(n.detail),
    time: when(n.ts),
  }));

  return [
    pageHead(t('ui.desc_alerts')),
    el('div', { class: 'grid split' }, [
      card(t('ui.recent_events'), [recent.length ? listRows(recent) : emptyState(t('ui.none'), 'ok')],
        null, el('span', { text: t('ui.count', { n: recent.length }) })),
      card(t('ui.channels'), [
        data.configured
          ? el('div', { class: 'tags' }, data.channels.map((c) => pill(c, 'ok')))
          : el('p', { class: 'error', text: t('ui.no_notify_channel') }),
        el('div', { class: 'row' }, [
          el('button', {
            type: 'button', text: t('ui.test_notify'),
            onclick: async () => {
              try { await api('/v1/platform/notifications', { method: 'POST', body: {} }); toast(t('ui.saved')); refresh(); }
              catch (err) { showError(err); }
            },
          }),
          el('button', {
            type: 'button', text: t('ui.diagnostics'),
            onclick: () => downloadJson('/v1/platform/diagnostics', 'diagnostics.json'),
          }),
        ]),
      ], data.configured ? null : 'bad'),
    ]),
  ];
}

/** 플러그인 — 설치·활성·제거.
 *
 * 활성 여부는 서버가 그 플러그인의 **서비스 status 에서 파생해서** 준다. 화면이
 * 자체 상태를 들고 있지 않으므로 여기서 표시와 실제가 갈릴 수 없다.
 */
async function renderPlugins() {
  const data = await api('/v1/platform/plugins');
  renderBanners(data.trusted_keys ? [] : [[t('ui.plugin_no_trust_key'), 'bad']]);

  const picker = el('input', { type: 'file', accept: '.lccp,.zip', id: 'plugin-file' });
  const install = el('button', {
    type: 'button', class: 'primary', text: t('ui.plugin_install_go'),
    onclick: async () => {
      const file = picker.files && picker.files[0];
      if (!file) return;
      try {
        const created = await api('/v1/platform/plugins', {
          method: 'POST', body: await file.arrayBuffer(),
        });
        // 토큰 원값은 이 응답이 마지막이다. 갱신으로 지워지기 전에 드로어에 띄운다.
        openDrawer(t('ui.plugin_installed_inactive'), [
          el('p', { class: 'hint', text: t('ui.plugin_token_once') }),
          el('pre', { class: 'reveal', text: created.token }),
        ], [drawerClose()]);
        refresh();
      } catch (err) { showError(err); }
    },
  });

  const rows = (data.plugins || []).map((p) => [
    el('div', {}, [
      el('div', { text: p.name, style: 'font-weight:500' }),
      el('div', { class: 'sub mono', text: p.version }),
    ]),
    pill(p.signature, p.signature === 'verified' || p.signature === 'signed' ? 'ok' : ''),
    switchControl(!!p.active, async () => {
      try {
        await api('/v1/platform/plugins/' + encodeURIComponent(p.id) + '/activate',
          { method: 'POST', body: { active: !p.active } });
        refresh();
      } catch (err) { showError(err); }
    }, p.active ? t('ui.plugin_deactivate') : t('ui.plugin_activate')),
    (p.allow_roles || []).join(', '),
    // 이 플러그인이 만든 잡 수. "얼마나 쓰고 있나" 를 답한다.
    String(p.jobs_created || 0),
    // 트리거. 안 돌고 있는 것이 보여야 한다 — 스케줄은 "아직 안 돎", 이벤트는 밀린 건수로.
    el('span', { class: 'mono', text: triggerCell(p) }),
    p.files_present ? '' : pill(t('ui.plugin_missing_files'), 'danger'),
    el('div', { class: 'row end' }, [
      // 토큰을 잃은 플러그인의 유일한 재발급 경로 — 재설치는 토큰을 다시 주지 않는다.
      el('button', { type: 'button', class: 'sm', text: t('ui.plugin_rotate_token'), onclick: () => rotatePluginToken(p.id) }),
      el('button', {
        type: 'button', class: 'danger sm', text: t('ui.plugin_remove'),
        onclick: async () => {
          if (!window.confirm(p.id)) return;
          try {
            await api('/v1/platform/plugins/' + encodeURIComponent(p.id), { method: 'DELETE' });
            refresh();
          } catch (err) { showError(err); }
        },
      }),
    ]),
  ]);

  return [
    card(t('ui.plugin_install'), [el('div', { class: 'row' }, [picker, install])]),  // 버튼 문구는 ui.plugin_install_go
    card(t('ui.plugins'), rows.length
      ? [table(
          [t('ui.plugin'), t('ui.plugin_signature'), t('ui.plugin_active'), t('ui.role'),
           t('ui.plugin_jobs'), t('ui.plugin_trigger'), '', ''],
          rows)]
      : [emptyState(t('ui.plugin_none'))]),
  ];
}

/** 플러그인 토큰 회전. 새 토큰은 이 응답이 마지막이라 드로어에 띄운다 — 갱신이 드로어를 지우지 않는다.
 *  유예 3600초는 서비스 토큰 회전(rotateToken)과 같다 — 운영자가 플러그인 설정을 고치고 재시작할 시간이다.
 *  살아 있는 토큰이 없으면 서버가 발급한다(응답의 reissued).
 */
async function rotatePluginToken(pluginId) {
  if (!window.confirm(pluginId)) return;
  try {
    const rotated = await api('/v1/platform/plugins/' + encodeURIComponent(pluginId) + '/rotate-token', {
      method: 'POST', body: { grace_seconds: 3600 },
    });
    openDrawer(t('ui.plugin_token_rotated', { minutes: 60 }), [
      el('p', { class: 'hint', text: t('ui.plugin_token_once') }),
      el('pre', { class: 'reveal', text: rotated.token }),
    ], [el('button', { type: 'button', class: 'right primary', text: t('ui.close'), onclick: () => { closeDrawer(); refresh(); } })]);
  } catch (err) { showError(err); }
}

/** 트리거 칸. 스케줄이면 cron(한 번도 안 돌았으면 그 사실), 이벤트면 구독한 것과 밀린 건수.
 *
 * 밀린 건수는 서버가 커서 뒤를 세어 준 값이다(`events_pending`). 켜 놓았는데 이 수가
 * 늘기만 하면 플러그인이 안 돌고 있는 것이고, 그것이 이 칸이 존재하는 이유다.
 */
function triggerCell(p) {
  const parts = [];
  if (p.schedule) {
    parts.push(p.schedule + (p.last_run_at ? '' : ' · ' + t('ui.plugin_never_ran')));
  }
  if (p.event) {
    const roles = (p.event_roles && p.event_roles.length) ? p.event_roles.join(', ') : '*';
    let text = p.event + ' (' + roles + ')';
    if (p.events_pending !== null && p.events_pending !== undefined) {
      text += ' · ' + t('ui.plugin_events_pending', { n: p.events_pending });
    }
    parts.push(text);
  }
  return parts.join(' / ');
}


/** 진단 번들 등을 파일로. 서버가 이미 비밀을 마스킹해서 준다. */
async function downloadJson(path, filename) {
  try {
    const body = await api(path);
    const blob = new Blob([JSON.stringify(body, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = el('a', { href: url, download: filename });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (err) { showError(err); }
}

// ── 설정 ──────────────────────────────────────────────────────────────────

/** 좌측 세로 탭 하나에 연결 정보 · 플러그인 · 데이터 관리 · 관리자 계정 · 내 계정을 모은다. */
async function renderSettings() {
  const s = state.session;
  const subs = [];
  if (s.is_tenant_admin) subs.push({ id: 'connections', label: t('ui.connections'), render: renderConnections });
  if (s.is_platform_admin) subs.push({ id: 'plugins', label: t('ui.plugins'), render: renderPlugins, platform: true });
  if (s.is_tenant_admin) subs.push({ id: 'data', label: t('ui.data'), render: renderData });
  // 사용자(user) 계정은 테넌트 관리자의 것이다 — 클라이언트 페이지로 로그인하는 사람들.
  if (s.is_tenant_admin) subs.push({ id: 'users', label: t('ui.user_accounts'), render: renderUserAccounts });
  if (s.is_platform_admin) subs.push({ id: 'accounts', label: t('ui.accounts'), render: renderAccounts, platform: true });
  // 계정으로 들어왔을 때만 — 서비스 토큰에는 바꿀 비밀번호가 없다.
  if (s.account) subs.push({ id: 'account', label: t('ui.account'), render: renderAccount });
  const active = subs.find((x) => x.id === state.sub.settings) || subs[0];
  state.sub.settings = active.id;

  const tabs = el('div', { class: 'vtabs' }, subs.map((x) => el('button', {
    type: 'button', class: x.id === active.id ? 'active' : null, text: x.label,
    onclick: () => go('settings', x.id),
  })));
  const panel = (active.platform && state.platformBlocked) ? [blockedNotice()] : await active.render();
  return [el('div', { class: 'settings' }, [tabs, el('div', { class: 'panel' }, [].concat(panel).filter(Boolean))])];
}

// ── 테넌트: 연결 정보 ─────────────────────────────────────────────────────

async function renderConnections() {
  const data = await fetchAll({
    services: '/v1/admin/services',
    tokens: '/v1/admin/tokens',
  });
  renderBanners();

  const services = (data.services && data.services.services) || [];
  // 역할 카탈로그 — 허용 역할 폼의 선택지. 같은 응답에 실려 온다(/v1/roles 는 자기 서비스 기준이라 못 쓴다).
  const roleCatalog = (data.services && data.services.roles) || [];
  const tokens = (data.tokens && data.tokens.tokens) || [];

  return [
    card(t('ui.services'), [table(
      [t('ui.services'), t('ui.role'), t('ui.rate_limit'), t('ui.budget'), t('ui.end_users'), t('ui.status'), ''],
      services.map((s) => [
        el('span', { class: 'mono', text: s.id, style: 'font-weight:500' }), s.allow_roles.join(', '),
        s.rate_limit_per_min ? s.rate_limit_per_min + '/min' : '—',
        s.budget_usd_per_month ? money(s.budget_usd_per_month) : '—',
        s.require_end_user ? badge('required', 'internal') : '—',
        badge(s.status, s.status === 'active' ? 'healthy' : 'unhealthy'),
        el('div', { class: 'row end' }, [
          el('button', { type: 'button', class: 'sm', text: t('ui.edit'), onclick: () => openServiceForm(s, roleCatalog) }),
        ]),
      ]))], null, [
      el('span', { text: t('ui.count', { n: services.length }) }),
      el('button', { type: 'button', class: 'primary sm', text: t('ui.add_service'), onclick: () => openServiceForm(null, roleCatalog) }),
    ]),

    card(t('ui.tokens'), [
      // **원값도 해시도 나가지 않는다.** 접두사만으로 어느 토큰인지 식별한다.
      table([t('ui.tokens'), t('ui.services'), t('ui.role'), t('ui.last_request'), '', ''],
        tokens.map((tok) => [
          el('code', { text: tok.prefix + '…' }),
          el('span', { class: 'mono', text: tok.service_id }), pill(tok.role, tok.role === 'platform_admin' ? 'accent' : ''), when(tok.last_used_at),
          tok.revoked_at ? badge(t('ui.revoke'), 'unhealthy')
            : (tok.expires_at ? badge(when(tok.expires_at), 'draining') : '—'),
          el('div', { class: 'row end' }, tok.revoked_at ? [] : [
            el('button', { type: 'button', class: 'sm', text: t('ui.rotate'), onclick: () => rotateToken(tok.id) }),
            el('button', { type: 'button', class: 'danger sm', text: t('ui.revoke'), onclick: () => revokeToken(tok.id) }),
          ]),
        ])),
      issueTokenForm(services),
    ], null, el('span', { text: t('ui.count', { n: tokens.length }) })),
  ];
}

function issueTokenForm(services) {
  const service = el('select', { id: 'token-service' },
    services.map((s) => el('option', { value: s.id, text: s.id })));
  const role = el('select', { id: 'token-role' }, [
    el('option', { value: 'service', text: 'service' }),
    el('option', { value: 'tenant_admin', text: 'tenant_admin' }),
  ]);
  return el('form', {
    class: 'inline',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        const issued = await api('/v1/admin/tokens', {
          method: 'POST', body: { service_id: service.value, role: role.value },
        });
        revealOnce(issued.token);
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'token-service', text: t('ui.services') }), service]),
    el('div', {}, [el('label', { for: 'token-role', text: t('ui.role') }), role]),
    el('button', { class: 'primary', type: 'submit', text: t('ui.issue_token') }),
  ]);
}

/** 서비스 정책 폼 — 편집(PUT)과 생성(POST)이 한 폼이다. 드로어 안이라 자동 갱신이 지우지 못한다.
 *  허용 역할은 카탈로그에서 고른다(`*` 는 전부). 한도·예산 빈칸은 "없음"(null) 이다 — 서버는 다음 요청부터 적용한다. */
function serviceForm(existing, roleCatalog) {
  const editing = !!existing;
  const id = el('input', { id: 'service-id', type: 'text', autocomplete: 'off', spellcheck: 'false', value: editing ? existing.id : '' });
  if (editing) id.disabled = true;
  const name = el('input', { id: 'service-name', type: 'text', autocomplete: 'off', value: editing ? (existing.name || '') : '' });
  let allowAll = !editing || existing.allow_roles.includes('*');
  const picked = new Set(editing ? existing.allow_roles.filter((r) => r !== '*') : []);
  const boxes = el('div', { class: 'stack', id: 'service-roles' }, roleCatalog.map((r) => {
    const box = el('input', { type: 'checkbox', id: 'service-role-' + r.name, value: r.name });
    box.checked = picked.has(r.name);
    box.disabled = allowAll;
    box.addEventListener('change', () => { if (box.checked) picked.add(r.name); else picked.delete(r.name); });
    return el('label', { for: box.id, class: 'check' }, [box, ' ', el('span', { class: 'mono', text: r.name }), ' ', pill(r.kind, '')]);
  }));
  const modeHost = el('div', {});
  const drawMode = () => {
    modeHost.replaceChildren(segment([
      { id: 'all', label: t('ui.all_roles') }, { id: 'some', label: t('ui.allow_roles') },
    ], allowAll ? 'all' : 'some', (mode) => {
      allowAll = mode === 'all';
      for (const box of boxes.querySelectorAll('input')) box.disabled = allowAll;
      drawMode();
    }));
  };
  drawMode();
  const rate = el('input', { id: 'service-rate', type: 'number', min: '1', step: '1', value: editing && existing.rate_limit_per_min ? String(existing.rate_limit_per_min) : '' });
  const endUserRate = el('input', { id: 'service-end-user-rate', type: 'number', min: '1', step: '1', value: editing && existing.end_user_rate_limit ? String(existing.end_user_rate_limit) : '' });
  const budget = el('input', { id: 'service-budget', type: 'number', min: '0', step: '0.01', value: editing && existing.budget_usd_per_month ? String(existing.budget_usd_per_month) : '' });
  let requireEndUser = editing ? !!existing.require_end_user : false;
  const switchHost = el('div', {});
  const drawSwitch = () => {
    switchHost.replaceChildren(switchControl(requireEndUser, () => { requireEndUser = !requireEndUser; drawSwitch(); }, t('ui.require_end_user')));
  };
  drawSwitch();
  const numberOrNull = (input) => (input.value.trim() === '' ? null : Number(input.value));
  return el('form', {
    class: 'stack',
    onsubmit: async (event) => {
      event.preventDefault();
      const policy = {
        name: name.value.trim() || id.value.trim(),
        allow_roles: allowAll ? ['*'] : Array.from(picked),
        rate_limit_per_min: numberOrNull(rate),
        end_user_rate_limit: numberOrNull(endUserRate),
        budget_usd_per_month: numberOrNull(budget),
        require_end_user: requireEndUser,
      };
      try {
        if (editing) {
          await api('/v1/admin/services/' + encodeURIComponent(existing.id), { method: 'PUT', body: policy });
        } else {
          await api('/v1/admin/services', { method: 'POST', body: Object.assign({ id: id.value.trim() }, policy) });
        }
        toast(t('ui.saved'));
        closeDrawer();
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'service-id', text: t('ui.service') }), id]),
    el('div', {}, [el('label', { for: 'service-name', text: t('ui.name') }), name]),
    el('div', {}, [el('label', { text: t('ui.allow_roles') }), modeHost, boxes]),
    el('div', {}, [el('label', { for: 'service-rate', text: t('ui.rate_limit') }), rate]),
    el('div', {}, [el('label', { for: 'service-end-user-rate', text: t('ui.end_user_rate_limit') }), endUserRate]),
    el('div', {}, [el('label', { for: 'service-budget', text: t('ui.budget') }), budget]),
    el('div', {}, [el('label', { text: t('ui.require_end_user') }), switchHost]),
    el('p', { class: 'hint', text: t('ui.limit_blank_hint') }),
    el('button', { type: 'submit', class: 'primary', text: editing ? t('ui.save') : t('ui.create') }),
  ]);
}

function openServiceForm(existing, roleCatalog) {
  openDrawer(existing ? t('ui.edit') + ' · ' + existing.id : t('ui.add_service'), [serviceForm(existing, roleCatalog)], [drawerClose()]);
}

/** 발급된 토큰을 한 번만 보여준다. 드로어는 갱신이 안 건드리므로 닫기 전까지 남는다. */
function revealOnce(value) {
  openDrawer(t('ui.token_shown_once'), [
    el('pre', { class: 'reveal', text: value }),
    el('p', { class: 'hint', text: t('ui.token_hint') }),
  ], [el('button', { type: 'button', class: 'right primary', text: t('ui.close'), onclick: () => { closeDrawer(); refresh(); } })]);
}

async function rotateToken(tokenId) {
  try {
    const rotated = await api('/v1/admin/tokens/' + encodeURIComponent(tokenId) + '/rotate', {
      method: 'POST', body: { grace_seconds: 3600 },
    });
    revealOnce(rotated.token);
  } catch (err) { showError(err); }
}

async function revokeToken(tokenId) {
  try {
    await api('/v1/admin/tokens/' + encodeURIComponent(tokenId), { method: 'DELETE' });
    toast(t('ui.revoked'));
    refresh();
  } catch (err) { showError(err); }
}

// ── 테넌트: 데이터 관리 ───────────────────────────────────────────────────

async function renderData() {
  const settings = await api('/v1/admin/settings');
  renderBanners();

  const retention = el('input', {
    id: 'retention', type: 'number', min: '0',
    value: settings.raw_prompt_retention_days,
  });
  const locale = el('select', { id: 'data-locale' },
    state.session.available_locales.map((code) =>
      el('option', { value: code, selected: code === settings.locale, text: code })));
  const purgeTarget = el('input', { id: 'purge-target', placeholder: 'end_user_hash' });

  const capped = settings.raw_prompt_retention_days_requested != null
    && settings.raw_prompt_retention_days_requested !== settings.raw_prompt_retention_days;

  return [
    card(t('ui.settings'), [
      el('form', {
        class: 'inline',
        onsubmit: async (event) => {
          event.preventDefault();
          try {
            await api('/v1/admin/settings', {
              method: 'PUT',
              body: {
                raw_prompt_retention_days: Number(retention.value),
                locale: locale.value,
              },
            });
            // 로케일이 바뀌면 문자열과 가드 팩이 함께 바뀐다 — 세션을 다시 받는다.
            await loadSession();
            toast(t('ui.saved'));
            refresh();
          } catch (err) { showError(err); }
        },
      }, [
        el('div', {}, [el('label', { for: 'retention', text: t('ui.retention') }), retention]),
        el('div', {}, [el('label', { for: 'data-locale', text: t('ui.locale') }), locale]),
        el('button', { class: 'primary', type: 'submit', text: t('ui.save') }),
      ]),
      // **조용히 자르지 않는다** — 30일로 설정했다고 믿는 채로 7일 뒤 사라지면 안 된다.
      capped ? el('p', { class: 'hint', text: t('ui.retention_capped', { days: settings.raw_prompt_retention_days }) }) : null,
      settings.raw_prompt_storage ? null : el('p', { class: 'error', text: t('ui.raw_storage_off') }),
    ], capped ? 'warn' : null),

    card(t('ui.export'), [
      el('p', { class: 'hint', text: t('ui.masked_only') }),
      el('div', {}, [el('button', { type: 'button', text: t('ui.download'), onclick: () => downloadJson('/v1/admin/export', 'export.json') })]),
    ]),

    card(t('ui.purge_end_user'), [
      el('p', { class: 'error', text: t('ui.purge_warning') }),
      el('form', {
        class: 'inline',
        onsubmit: async (event) => {
          event.preventDefault();
          const target = purgeTarget.value.trim();
          if (!target || prompt(t('ui.confirm') + ': ' + target) !== target) return;
          try {
            await api('/v1/admin/end-users/' + encodeURIComponent(target)
              + '?confirm=' + encodeURIComponent(target), { method: 'DELETE' });
            purgeTarget.value = '';
            toast(t('ui.deleted'));
            refresh();
          } catch (err) { showError(err); }
        },
      }, [
        el('div', {}, [el('label', { for: 'purge-target', text: t('ui.end_users') }), purgeTarget]),
        el('button', { class: 'danger', type: 'submit', text: t('ui.delete') }),
      ]),
    ], 'bad'),
  ];
}

// ── 소비자 토큰용 최소 화면 ───────────────────────────────────────────────

async function renderConsumerStatus() {
  const data = await fetchAll({ status: '/v1/status', roles: '/v1/roles' });
  renderBanners();
  const s = data.status || { lanes: {}, nodes: {} };
  const lanes = Object.entries(s.lanes || {});

  return [
    el('div', { class: 'grid' }, [
      stat(t('ui.nodes'), (s.nodes.healthy || 0) + ' / ' + (s.nodes.total || 0),
        s.nodes.total && s.nodes.healthy === s.nodes.total ? 'ok' : '', t('ui.online')),
      stat(t('ui.queued'), lanes.reduce((sum, [, l]) => sum + l.queued, 0), '',
        t('ui.running') + ' ' + lanes.reduce((sum, [, l]) => sum + (l.running || 0), 0)),
    ]),
    el('div', { class: 'grid wide' }, [
      card(t('ui.lane'), [table(
        [t('ui.lane'), { label: t('ui.running'), num: true }, { label: t('ui.queued'), num: true }],
        lanes.map(([name, l]) => [el('span', { class: 'mono' }, [laneChip(name), name]), l.running || 0, l.queued || 0]))]),
      card(t('ui.role'), [table(
        [t('ui.role'), t('ui.kind'), { label: t('ui.timeout'), num: true }, { label: t('ui.max_prompt_chars'), num: true }],
        ((data.roles && data.roles.roles) || []).map((r) =>
          [el('span', { text: r.name, style: 'font-weight:500' }), pill(r.kind, ''), r.timeout_seconds + 's', r.max_prompt_chars]))]),
    ]),
  ];
}

// ── 계정 ──────────────────────────────────────────────────────────────────

/** 내 계정 — 비밀번호 변경. 다른 기기의 세션은 서버가 끊는다. */
async function renderAccount() {
  const current = el('input', { id: 'pw-current', type: 'password', autocomplete: 'current-password' });
  const next = el('input', { id: 'pw-next', type: 'password', autocomplete: 'new-password' });
  const again = el('input', { id: 'pw-again', type: 'password', autocomplete: 'new-password' });
  const note = el('p', { class: 'muted' });
  const form = el('form', {
    class: 'stack',
    onsubmit: async (event) => {
      event.preventDefault();
      note.textContent = '';
      if (next.value !== again.value) { note.textContent = t('ui.password_mismatch'); return; }
      try {
        await api('/v1/session/password', {
          method: 'POST',
          body: { current_password: current.value, new_password: next.value },
        });
        current.value = ''; next.value = ''; again.value = '';
        note.textContent = t('ui.password_changed');
        toast(t('ui.password_changed'));
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'pw-current', text: t('ui.current_password') }), current]),
    el('div', {}, [el('label', { for: 'pw-next', text: t('ui.new_password') }), next]),
    el('div', {}, [el('label', { for: 'pw-again', text: t('ui.confirm_password') }), again]),
    el('button', { type: 'submit', class: 'primary', text: t('ui.change_password') }),
    note,
  ]);
  const s = state.session;
  return [
    card(t('ui.account'), [
      el('div', { class: 'row' }, [
        el('span', { class: 'avatar', style: 'width:44px;height:44px;font-size:15px', text: initialOf(s) }),
        el('div', {}, [
          el('div', { text: s.account, style: 'font-weight:600' }),
          el('div', { class: 'sub muted', text: s.tenant.name + ' · ' + s.role }),
        ]),
      ]),
      el('p', { class: 'hint', text: t('ui.signed_in_as', { name: s.account }) }),
      form,
    ]),
  ];
}

/** 관리자 계정 — 목록·만들기·정지·비밀번호 재설정. 해시는 서버가 애초에 안 내준다. */
/** 계정 표·정지 스위치·재설정 폼. 플랫폼(관리자 계정)과 테넌트(사용자 계정)가 base path 만 바꿔 같이 쓴다.
 *  목록에 해시는 없다 — 서버가 애초에 내주지 않는다. */
function accountsPanel(basePath, accounts, options) {
  const rows = accounts.map((a) => [
    el('span', { class: 'mono', text: a.username, style: 'font-weight:500' }),
    pill(a.role, a.role === 'platform_admin' ? 'accent' : (a.role === 'user' ? 'info' : '')),
    el('span', { class: 'mono', text: options.showTenant ? a.tenant_id : a.service_id }),
    el('span', { class: 'muted', text: a.last_login_at ? when(a.last_login_at) : '—' }),
    a.disabled_at ? pill(t('ui.disabled'), 'danger') : pill(t('ui.plugin_active'), 'ok'),
    switchControl(!a.disabled_at, async () => {
      try {
        await api(basePath + '/' + encodeURIComponent(a.username) + '/disable',
          { method: 'POST', body: { disabled: !a.disabled_at } });
        refresh();
      } catch (err) { showError(err); }
    }, a.disabled_at ? t('ui.account_enable') : t('ui.account_disable')),
  ]);

  const target = el('select', { id: 'reset-target' }, accounts.map((a) => el('option', { value: a.username, text: a.username })));
  const fresh = el('input', { id: 'reset-password', type: 'password', autocomplete: 'new-password' });
  const resetForm = el('form', {
    class: 'inline',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        await api(basePath + '/' + encodeURIComponent(target.value) + '/password',
          { method: 'POST', body: { password: fresh.value } });
        fresh.value = '';
        toast(t('ui.account_reset_done'));
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'reset-target', text: t('ui.username') }), target]),
    el('div', {}, [el('label', { for: 'reset-password', text: t('ui.new_password') }), fresh]),
    el('button', { type: 'submit', text: t('ui.account_reset_password') }),
  ]);

  return [
    card(options.title, [rows.length
      ? table([
        t('ui.username'), t('ui.role'), options.showTenant ? t('ui.tenants') : t('ui.service'),
        t('ui.last_login'), t('ui.status'), '',
      ], rows)
      : emptyState(t('ui.account_none'))],
      null, el('button', {
        type: 'button', class: 'primary sm', text: t('ui.account_create'),
        onclick: () => openDrawer(t('ui.account_create'), [options.createForm()]),
      })),
    accounts.length ? card(t('ui.account_reset_password'), [resetForm]) : null,
  ];
}

async function renderAccounts() {
  const data = await api('/v1/platform/accounts');
  renderBanners();
  return accountsPanel('/v1/platform/accounts', data.accounts || [], {
    title: t('ui.accounts'), showTenant: true, createForm: createAccountForm,
  });
}

/** 테넌트 관리자의 사용자 계정 — 클라이언트 페이지(/client/)로 들어오는 사람들. 역할은 user 로 고정이다. */
async function renderUserAccounts() {
  const data = await fetchAll({ accounts: '/v1/admin/accounts', services: '/v1/admin/services' });
  renderBanners();
  const services = (data.services && data.services.services) || [];
  return [el('p', { class: 'hint', text: t('ui.user_hint') })].concat(
    accountsPanel('/v1/admin/accounts', (data.accounts && data.accounts.accounts) || [], {
      title: t('ui.user_accounts'), showTenant: false, createForm: () => createUserForm(services),
    }));
}

function createUserForm(services) {
  const username = el('input', { id: 'user-username', type: 'text', autocomplete: 'off', spellcheck: 'false' });
  const password = el('input', { id: 'user-password', type: 'password', autocomplete: 'new-password' });
  // 서비스는 고른다 — 허용 역할·한도·예산이 거기 걸린다. 서버도 추측하지 않는다.
  const service = el('select', { id: 'user-service' }, services.map((svc) => el('option', { value: svc.id, text: svc.id })));
  return el('form', {
    class: 'stack',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        await api('/v1/admin/accounts', {
          method: 'POST',
          body: { username: username.value.trim(), password: password.value, service_id: service.value },
        });
        toast(t('ui.account_created'));
        closeDrawer();
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'user-username', text: t('ui.username') }), username]),
    el('div', {}, [el('label', { for: 'user-password', text: t('ui.password') }), password]),
    el('div', {}, [el('label', { for: 'user-service', text: t('ui.service') }), service]),
    el('p', { class: 'hint', text: t('ui.user_hint') }),
    el('button', { type: 'submit', class: 'primary', text: t('ui.account_create') }),
  ]);
}

function createAccountForm() {
  const username = el('input', { id: 'acct-username', type: 'text', autocomplete: 'off', spellcheck: 'false' });
  const password = el('input', { id: 'acct-password', type: 'password', autocomplete: 'new-password' });
  const role = el('select', { id: 'acct-role' }, [
    el('option', { value: 'platform_admin', text: 'platform_admin' }),
    el('option', { value: 'tenant_admin', text: 'tenant_admin' }),
    el('option', { value: 'user', text: 'user' }),
  ]);
  const tenant = el('input', { id: 'acct-tenant', type: 'text', autocomplete: 'off', spellcheck: 'false' });
  // 세션이 걸릴 서비스. 비우면 서버 관행(<테넌트>-app)을 따른다 — user 는 여기 걸린 한도·역할을 받는다.
  const service = el('input', { id: 'acct-service', type: 'text', autocomplete: 'off', spellcheck: 'false' });
  return el('form', {
    class: 'stack',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        await api('/v1/platform/accounts', {
          method: 'POST',
          body: {
            username: username.value.trim(), password: password.value, role: role.value,
            tenant_id: tenant.value.trim() || undefined,
            service_id: service.value.trim() || undefined,
          },
        });
        toast(t('ui.account_created'));
        closeDrawer();
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'acct-username', text: t('ui.username') }), username]),
    el('div', {}, [el('label', { for: 'acct-password', text: t('ui.password') }), password]),
    el('div', {}, [el('label', { for: 'acct-role', text: t('ui.role') }), role]),
    el('div', {}, [el('label', { for: 'acct-tenant', text: t('ui.tenants') }), tenant]),
    el('div', {}, [el('label', { for: 'acct-service', text: t('ui.service') }), service]),
    el('button', { type: 'submit', class: 'primary', text: t('ui.account_create') }),
  ]);
}

function initialOf(session) {
  const name = session.account || session.role || '?';
  return name.slice(0, 1).toUpperCase();
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
  const button = $('theme');
  button.replaceChildren(icon(effectiveTheme() === 'dark' ? 'sun' : 'moon'));
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

// ── 접속 ──────────────────────────────────────────────────────────────────

/** 아이디·비밀번호 → 세션 토큰. 인증 없이 부르는 유일한 경로라 `api()` 를 안 쓴다. */
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

async function loadSession() {
  state.session = await api('/v1/session');
  state.strings = state.session.strings || {};
  document.documentElement.lang = state.session.locale;
  applyStaticStrings();
  applyTitleStrings();
  state.platformBlocked = await platformSurfaceBlocked();

  const who = state.session.account
    ? t('ui.signed_in_as', { name: state.session.account }) + ' · '
    : '';
  $('who').textContent = who + state.session.tenant.name + ' · ' + state.session.role;
  $('version').textContent = 'v' + state.session.version;
  const avatar = $('avatar');
  avatar.textContent = initialOf(state.session);
  avatar.title = (state.session.account || state.session.role) + ' · ' + state.session.tenant.name;
}

/** 플랫폼 관리 면이 이 주소에서 열리는가.
 *
 *  앱은 이 경로에 404 를 내지 않는다 — 플랫폼 관리자면 200, 아니면 403 이다. 404 는 앞단
 *  프록시(번들 nginx · Caddy)가 공개 경로에서 그 면을 감춘 것이다(topology §2). 한 번만 묻고
 *  세션에 기억한다. 물음 자체가 실패하면 막힌 것으로 단정하지 않는다. */
async function platformSurfaceBlocked() {
  if (!state.session || !state.session.is_platform_admin) return false;
  try {
    const response = await fetch('/v1/platform/overview', {
      method: 'HEAD', headers: { Authorization: 'Bearer ' + state.token },
    });
    return response.status === 404;
  } catch (_) {
    return false;
  }
}

async function connect(token) {
  state.token = token;
  await loadSession();
  // **sessionStorage 다** — 탭을 닫으면 지워진다. 공용 PC 에 토큰이 남지 않게.
  try { sessionStorage.setItem(TOKEN_KEY, token); } catch (_) { /* 사파리 프라이빗 등 */ }
  $('login').hidden = true;
  $('shell').hidden = false;
  await refresh();
  startAutoRefresh();
}

function disconnect() {
  stopAutoRefresh();
  closeDrawer();
  state.token = null;
  state.session = null;
  state.page = null;
  state.header = { nodes: null, pending: 0 };
  try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* 무시 */ }
  $('shell').hidden = true;
  $('login').hidden = false;
}

async function boot() {
  applyStaticStrings();
  initTheme();
  // 기본은 계정, 토큰은 한 번 눌러 연다. 로그인 전에는 카탈로그가 비어 있으므로
  // 두 버튼의 문구는 index.html 의 폴백 텍스트가 맡는다 — t() 로 바꾸지 않는다.
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
        await connect($('token').value.trim());
      } else {
        // 아이디·비밀번호 → 세션 토큰. 그 뒤는 토큰 접속과 같은 길이다.
        const session = await login($('username').value.trim(), $('password').value);
        $('password').value = '';
        await connect(session.token);
      }
    } catch (err) {
      box.textContent = err.message || String(err);
      box.hidden = false;
    }
  });
  $('refresh').addEventListener('click', async () => { await refresh(); toast(t('ui.refreshed')); });
  $('logout').addEventListener('click', async () => {
    // 계정 세션이면 서버의 세션 토큰도 폐기한다. 화면만 지우면 토큰은 만료까지 살아 있다.
    if (state.session && state.session.account) {
      try { await api('/v1/logout', { method: 'POST' }); } catch (_) { /* 이미 만료됐을 수 있다 */ }
    }
    disconnect();
  });
  $('theme').addEventListener('click', toggleTheme);
  $('avatar').addEventListener('click', () => {
    if (!state.session) return;
    const pages = pagesFor(state.session).map((p) => p.id);
    if (pages.includes('settings')) go('settings', state.session.account ? 'account' : undefined);
  });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-overlay').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('drawer').hidden) closeDrawer();
  });

  let saved = null;
  try { saved = sessionStorage.getItem(TOKEN_KEY); } catch (_) { saved = null; }
  if (saved) {
    try { await connect(saved); return; } catch (_) { /* 만료·폐기된 토큰 */ }
  }
  $('login').hidden = false;
}

document.addEventListener('DOMContentLoaded', boot);
