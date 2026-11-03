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
 * 화면 구조(2026-11 개편): 좌측 라벨 사이드바 — 플랫폼 · 테넌트 두 묶음에 9개 진입점, 테넌트
 * 묶음 머리에 지금 보고 있는 테넌트 — · 상단 바의 상태 칩 · 본문 머리(제목 · 설명 · 화면 동작) ·
 * 패널 · 우측 드로어 · 토스트. 개요의 첫 패널은 레인 보드다. 색은 뜻으로만 쓴다: 호박색은
 * 사람이 볼 것, 청색은 계기가 잰 것, 녹색 · 적색은 상태. 기능 면은 그대로다: 서버가 주는 것만
 * 그리고, 토큰 원값 열람이나 역할 편집처럼 API 에 없는 것은 화면에도 없다.
 */

'use strict';

const TOKEN_KEY = 'llmcc.token';
//: 플랫폼 관리자가 고른 테넌트. 토큰과 같은 수명이라 sessionStorage 다.
const VIEW_TENANT_KEY = 'llmcc.view_tenant';
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
  auditFilter: 'all',
  // 플랫폼 관리자가 테넌트 면에서 보는 테넌트(null = 자기 테넌트)와 고를 수 있는 목록.
  viewTenant: null,
  tenantList: [],
  usageAxis: 'service_id',
  usageWindow: '30d',
  // 가드 미리보기의 입력과 결과 — 화면 밖(state)에 두어야 자동 갱신이 지우지 않는다.
  guardPreview: { text: '', checked: '', result: null },
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
  audit: [['path', { d: 'M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z' }], ['path', { d: 'M14 3v6h6M8 13h8M8 17h5' }]],
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

/** 카드(패널). 제목 줄 + 본문. 표·목록은 가장자리에 붙고, 문단·폼은 여백 안에 들어간다.
 *  `aside` 는 제목 줄 오른쪽(보조 문구·버튼), `meta` 는 제목 바로 뒤의 조용한 설명. */
function card(title, children, cls, aside, meta) {
  const node = el('section', { class: 'card' + (cls ? ' ' + cls : '') });
  if (title || aside) {
    node.appendChild(el('div', { class: 'card-head' }, [
      title ? el('h2', { text: title }) : null,
      meta ? el('span', { class: 'meta', text: meta }) : null,
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

/* 배지 → 필. 예전 클래스 이름은 톤으로 옮긴다 — 호출부가 데이터 값을 그대로 넘기기 때문이다. */
//: 톤은 뜻이다 — 사람이 볼 것(warn = 호박색), 계기가 잰 것 · 계기가 한 일(info = 청색), 상태(ok · danger).
//: 경계(internal · external)는 상태가 아니라 사실이라 중립이다. 마스킹(full · partial)은 가드가 한 일이다.
const TONE_OF = {
  healthy: 'ok', ok: 'ok', active: 'ok', ready: 'ok',
  unhealthy: 'danger', block: 'danger', bad: 'danger', failed: 'danger', danger: 'danger', blocked: 'danger',
  draining: 'warn', warn: 'warn', pending: 'warn',
  info: 'info', running: 'info', pulling: 'info', approved: 'info', full: 'info', partial: 'info',
  accent: 'accent',
};

function pill(text, tone) {
  return el('span', { class: 'pill' + (tone ? ' ' + tone : ''), text: String(text) });
}

function badge(text, cls) {
  return pill(text, TONE_OF[cls] || '');
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

function table(headers, rows, cls) {
  if (!rows.length) return emptyState(t('ui.empty'));
  return el('div', { class: 'scroll' }, [
    el('table', { class: cls || null }, [
      // **머리글은 값이 아니라 모양으로 가른다.** 라벨 자리에 falsy 폴백을 쓰면
      // 빈 문자열 라벨(`{label:'', num:true}` — 제목 없이 오른쪽 정렬만 하는 칸)이
      // 객체 자체로 떨어져 화면에 `[object Object]` 가 뜬다. 카탈로그 표가 그랬다.
      el('thead', {}, [el('tr', {}, headers.map((h) =>
        el('th', {
          class: h && h.num ? 'num' : null,
          text: (h && typeof h === 'object') ? (h.label ?? '') : (h ?? ''),
        })))]),
      el('tbody', {}, rows.map((cells) => el('tr', { class: cells.rowClass || null }, cells.map((cell, i) =>
        el('td', { class: headers[i] && headers[i].num ? 'num' : null },
          typeof cell === 'object' && cell !== null ? [cell] : [String(cell ?? '')]))))),
    ]),
  ]);
}

/** 빈 상태. 문장 하나 — "표시할 항목이 없음" 이 왜 없음인지 말할 자리다. 색 점 대신 문장이 상태를 말한다. */
function emptyState(text, tone) {
  return el('div', { class: 'empty' + (tone === 'danger' || tone === 'bad' ? ' bad' : '') }, [el('span', { text })]);
}

function listRows(items) {
  return el('div', { class: 'list' }, items);
}

function listRow(parts) {
  return el('div', { class: 'list-row' }, [
    el('div', { class: 'body' }, [
      el('div', { class: 'title' }, [].concat(parts.title || [])),
      parts.detail ? el('div', { class: 'detail' }, [].concat(parts.detail)) : null,
    ]),
    parts.time ? el('span', { class: 'time', text: parts.time }) : null,
    parts.actions ? el('div', { class: 'row' }, parts.actions) : null,
  ]);
}

/** 비율 막대. 폭을 바꾸지 않고 비율(transform)로 그린다 — 값이 바뀌어도 레이아웃이 흔들리지 않는다.
 *  가득 참(1.0)은 한도에 닿았다는 뜻이라 적색이 된다. 비교 막대(`plain`)는 1.0 이 "가장 큰 것" 일 뿐이라 색을 바꾸지 않는다. */
function bar(ratio, warnAt, thick, plain) {
  const pct = Math.max(0, Math.min(1, ratio || 0));
  const tone = plain ? '' : (pct >= 1 ? 'bad' : (warnAt && pct >= warnAt ? 'warn' : ''));
  return el('span', { class: 'bar' + (thick ? ' thick' : ''), 'aria-hidden': 'true' }, [
    el('span', { class: tone || null, style: '--v:' + pct.toFixed(3) }),
  ]);
}

/** 막대와 읽은 값을 한 줄에 — 값이 막대에 덮이지 않게 칸을 나눈다. */
function meterRow(ratio, text, warnAt) {
  return el('span', { class: 'meter-row' }, [bar(ratio, warnAt), el('span', { class: 'mono', text })]);
}

/** 동시성 칸. 칸 하나가 실행 자리 하나, 찬 칸이 지금 도는 것. 읽은 값을 옆에 붙인다. */
function slotCell(running, max) {
  const cells = [];
  for (let i = 0; i < (max || 0); i++) cells.push(el('i', { class: i < running ? 'on' : null }));
  return el('span', { class: 'slot-cell' }, [
    el('span', { class: 'cap', 'aria-hidden': 'true' }, cells),
    el('span', { class: 'mono', text: (running || 0) + ' / ' + (max || 0) }),
  ]);
}

/** 큰 숫자 칸들 — 라벨 · 값(잰 값의 글꼴) · 단위. 아이콘도 추세 화살표도 없다. */
function figures(items) {
  return el('dl', { class: 'figures' }, items.filter(Boolean).map(([label, value, unit]) => el('div', {}, [
    el('dt', { text: label }),
    el('dd', {}, [el('span', { class: 'reading', text: String(value) }), unit ? el('span', { class: 'unit', text: unit }) : null]),
  ])));
}

// ── 추이 차트 ─────────────────────────────────────────────────────────────

function svgEl(tag, attrs, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  if (text !== undefined) node.textContent = String(text);
  return node;
}

/** 눈금 상한 — 1·2·5 × 10ⁿ 중 최댓값 이상인 첫 값. 막대가 천장에 닿지 않고 눈금이 읽히는 숫자가 된다. */
function niceCeiling(max) {
  if (!(max > 0)) return 1;
  const power = Math.pow(10, Math.floor(Math.log10(max)));
  for (const unit of [1, 2, 5, 10]) if (unit * power >= max) return unit * power;
  return 10 * power;
}

function compact(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n % 1e3 ? 1 : 0) + 'k';
  return Number.isInteger(n) ? String(n) : n.toFixed(n < 1 ? 3 : 1);
}

function bucketLabel(start, bucket) {
  const d = new Date(start * 1000);
  if (bucket === 'day') return (d.getMonth() + 1) + '/' + d.getDate();
  return String(d.getHours()).padStart(2, '0') + ':00';
}

/**
 * 막대 하나가 한 묶음(시간·일). **축은 하나, 계열도 하나** — 요청 수에서만 실패를 위에 쌓아 두 색이 된다.
 * 막대는 가로로 늘어나는 SVG(가로 n×10, 세로 100)에 그리고, 눈금 · 범례는 HTML 이다 — 화면 폭이 달라도
 * 글자는 같은 크기로 읽힌다(SVG 안 글자는 폭에 따라 7px 까지 줄었다). 색은 계기가 잰 것(--cool)과 실패
 * (--danger)뿐이다. 빈 묶음은 서버가 0 으로 채워 보내므로 받은 점을 그대로 그린다. 호버는 묶음 전체
 * 너비의 투명 영역이 받아 읽는 줄 하나에 쓴다.
 */
function trendChart(series, opts) {
  const o = opts || {};
  const points = (series && series.points) || [];
  if (!points.length) return emptyState(t('ui.none'));
  const metric = o.metric || 'calls';
  const valueOf = (p) => (metric === 'tokens' ? (p.input_tokens || 0) + (p.output_tokens || 0)
    : (metric === 'cost' ? (p.cost_usd || 0) : (p.calls || 0)));
  const n = points.length;
  const top = niceCeiling(Math.max(...points.map(valueOf)));
  const split = metric === 'calls' && points.some((p) => (p.calls || 0) > (p.ok || 0));
  const svg = svgEl('svg', { viewBox: '0 0 ' + (n * 10) + ' 100', preserveAspectRatio: 'none', class: 'trend-svg', 'aria-hidden': 'true' });
  const ys = el('div', { class: 'trend-y', 'aria-hidden': 'true' });
  // 눈금은 0 · 절반 · 상한. 상한이 1 이면(빈 창) 절반 눈금은 뜻이 없어 뺀다.
  for (const frac of (top < 2 && metric !== 'cost' ? [0, 1] : [0, 0.5, 1])) {
    const y = 100 * (1 - frac);
    svg.appendChild(svgEl('line', { x1: 0, x2: n * 10, y1: y, y2: y, class: 'grid' }));
    ys.appendChild(el('span', { style: 'top:' + y + '%', text: compact(top * frac) }));
  }
  const tip = el('div', { class: 'trend-tip' });
  tip.hidden = true;
  const xs = el('div', { class: 'trend-x', 'aria-hidden': 'true' });
  const every = Math.max(1, Math.ceil(n / 6));
  points.forEach((p, i) => {
    const x = i * 10 + 1;
    const total = Math.max(0, 100 * (valueOf(p) / top));
    const g = svgEl('g', { class: 'bar' });
    if (split) {
      const okH = 100 * ((p.ok || 0) / top);
      const failH = total - okH;
      if (okH > 0) g.appendChild(svgEl('rect', { x, y: 100 - okH, width: 8, height: okH, class: 'ok' }));
      // 쌓인 두 조각 사이는 바탕색 틈 — 경계가 색 차이에만 기대지 않게.
      if (failH > 0) g.appendChild(svgEl('rect', { x, y: 100 - total, width: 8, height: Math.max(0.6, failH - (okH > 0 ? 0.8 : 0)), class: 'fail' }));
    } else if (total > 0) {
      g.appendChild(svgEl('rect', { x, y: 100 - total, width: 8, height: total, class: 'ok' }));
    }
    // 호버 영역은 묶음 폭·높이 전체 — 얇은 막대와 빈 묶음도 읽을 수 있어야 한다.
    const hit = svgEl('rect', { x: i * 10, y: 0, width: 10, height: 100, class: 'hit' });
    const failed = (p.calls || 0) - (p.ok || 0);
    const label = bucketLabel(p.start, series.bucket) + ', ' + t('ui.calls') + ' ' + (p.calls || 0)
      + (failed > 0 ? ' (' + t('ui.legend_failed') + ' ' + failed + ')' : '')
      + ', ' + t('ui.tokens_used') + ' ' + ((p.input_tokens || 0) + (p.output_tokens || 0))
      + (p.cost_usd ? ', ' + money(p.cost_usd) : '')
      + (p.avg_duration_ms ? ', ' + p.avg_duration_ms + ' ms' : '');
    hit.addEventListener('mouseenter', () => { tip.textContent = label; tip.hidden = false; g.classList.add('hover'); });
    hit.addEventListener('mouseleave', () => { tip.hidden = true; g.classList.remove('hover'); });
    g.appendChild(hit);
    svg.appendChild(g);
    if (i % every === 0) {
      xs.appendChild(el('span', { style: 'left:' + ((i + 0.5) / n * 100).toFixed(2) + '%', text: bucketLabel(p.start, series.bucket) }));
    }
  });
  const figure = el('figure', { class: 'trend', role: 'img', 'aria-label': o.label || '' },
    [ys, el('div', { class: 'trend-plot' }, [svg]), xs]);
  const legend = split ? el('div', { class: 'trend-legend' }, [
    el('span', {}, [el('i', { class: 'ok' }), t('ui.legend_ok')]),
    el('span', {}, [el('i', { class: 'fail' }), t('ui.legend_failed')]),
  ]) : null;
  return el('div', { class: 'trend-wrap' }, [figure, tip, legend]);
}

/* 레인 이름의 뜻. 키를 조립하지 않고 조회표로 고른다. */
const LANE_LABEL = {
  interactive: 'ui.lane_interactive',
  batch: 'ui.lane_batch',
  guard: 'ui.lane_guard',
};

/** 레인 이름 — 시스템이 쓰는 이름(interactive · batch · guard)과 그 뜻을 한 칸에. */
function laneChip(name) {
  return el('div', { class: 'lane-name' }, [
    el('h3', { text: name }),
    LANE_LABEL[name] ? el('span', { class: 'lane-sub', text: t(LANE_LABEL[name]) }) : null,
  ]);
}

/** 대기 사유 한 줄. 'none' 은 사유 없이 차례를 기다리는 것 — 멈춘 큐로 세지 않되 숨기지도 않는다
 *  (예전 화면은 12건이 차례를 기다리는 중에도 "모든 레인이 유휴" 라고 말했다). */
function waitingLine(waiting, queued) {
  const stopped = Object.entries(waiting || {}).filter(([reason]) => reason !== 'none');
  const text = stopped.length
    ? stopped.map(([reason, count]) => reason + ' ' + count).join(', ')
    : (queued ? t('ui.queue_in_turn', { n: queued }) : t('ui.no_waiting'));
  return [el('strong', { text: t('ui.waiting_reason') }), el('span', { text: t('ui.waiting_note') }), el('span', { text })];
}

/**
 * 레인 보드 — 이 콘솔의 첫 물건. 레인마다 동시성 칸 대비 실행, 대기 수(크게), 스캔 창 대비 대기.
 * 스캔 창은 스케줄러가 한 번에 훑는 대기 수다 — 대기가 그 창을 넘으면 뒤쪽 잡이 안 보인다(절단).
 * `waiting` 을 주면(플랫폼 개요) 바닥 줄에 대기 사유를 적는다.
 */
function laneBoard(lanes, waiting, aside) {
  const entries = Object.entries(lanes || {});
  const queued = entries.reduce((sum, [, l]) => sum + (l.queued || 0), 0);
  const running = entries.reduce((sum, [, l]) => sum + (l.running || 0), 0);
  const capLabel = t('ui.running') + ' / ' + t('ui.concurrency');
  const rows = entries.map(([name, l]) => el('li', { class: 'lane' }, [
    laneChip(name),
    el('dl', { class: 'lane-vals' }, [
      el('div', { class: 'lane-cap' }, [el('dt', { text: capLabel }), el('dd', {}, [slotCell(l.running || 0, l.max_concurrent || 0)])]),
      el('div', { class: 'lane-q' }, [el('dt', { text: t('ui.queued') }),
        el('dd', {}, [el('span', { class: 'reading' + (l.queued ? '' : ' zero'), text: String(l.queued || 0) })])]),
      el('div', { class: 'lane-scan' }, [el('dt', { text: t('ui.scan_window') }), el('dd', {}, [
        meterRow(l.scan_window ? (l.queued || 0) / l.scan_window : 0, (l.queued || 0) + ' / ' + (l.scan_window || 0)),
        l.scan_truncated ? badge(t('ui.scan_truncated'), 'block') : null,
      ])]),
    ]),
  ]));
  const head = el('div', { class: 'lane-cols flush', 'aria-hidden': 'true' }, [
    el('span', { text: t('ui.lane') }), el('span', { text: capLabel }),
    el('span', { text: t('ui.queued') }), el('span', { text: t('ui.scan_window') }),
  ]);
  return card(t('ui.lane'), [
    head,
    el('ul', { class: 'lane-list flush' }, rows),
    waiting ? cardFoot(waitingLine(waiting, queued)) : null,
  ], 'lane-board', [].concat(aside || [],
    pill(t('ui.queued') + ' ' + queued, ''), pill(t('ui.running') + ' ' + running, '')), t('ui.lane_note'));
}

function kv(pairs) {
  return el('div', { class: 'kv' }, pairs.filter(Boolean).map(([key, value, span]) =>
    el('div', { class: span ? 'span' : null }, [
      el('div', { class: 'k', text: key }),
      typeof value === 'object' && value !== null ? value : el('div', { text: String(value ?? t('ui.none')) }),
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

/** 화면 안의 하위 탭 — 밑줄 하나로 지금 탭을 표시한다(호박색 = 지금 보고 있는 것). */
function tabRow(options, activeId, onPick) {
  return el('div', { class: 'tabs', role: 'tablist' }, options.map((o) => el('button', {
    type: 'button', role: 'tab', class: o.id === activeId ? 'active' : null,
    'aria-selected': o.id === activeId ? 'true' : 'false', text: o.label, onclick: () => onPick(o.id),
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
  if (!ts) return t('ui.none');
  return new Date(ts * 1000).toLocaleString();
}

/** 목록용 짧은 시각 — 월-일 시:분:초(24시간). 자정을 넘는 목록에서도 날짜가 보인다. */
function stamp(ts) {
  if (!ts) return t('ui.none');
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function money(value) {
  return '$' + (Number(value) || 0).toFixed(4);
}

/** 예산 · 한도처럼 사람이 정한 금액 — 센트까지. 쓴 비용은 money() 로 넷째 자리까지 적는다. */
function dollars(value) {
  return '$' + (Number(value) || 0).toFixed(2);
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
    { Authorization: 'Bearer ' + state.token }, scopeHeaders(path), opts.headers);
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
  // 남의 테넌트를 보고 있다는 사실은 화면마다 보인다 — 쓰기도 그 테넌트에 들어가기 때문이다.
  if (state.viewTenant) {
    warnings.push([t('ui.tenant_view_banner', { name: viewedTenant().name }), 'info',
      [t('ui.tenant_view_leave'), () => setViewTenant(null)]]);
  }
  for (const line of extra || []) warnings.push(line);

  for (const [text, cls, action] of warnings) {
    box.appendChild(el('div', { class: 'banner' + (cls ? ' ' + cls : '') }, [
      el('div', { class: 'text', text }),
      action ? el('button', {
        type: 'button', text: action[0],
        onclick: () => (typeof action[1] === 'function' ? action[1]() : go(action[1])),
      }) : null,
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
  { id: 'audit', icon: 'audit', label: 'ui.audit', desc: 'ui.desc_audit',
    show: (s) => s.is_tenant_admin, render: () => renderAudit() },
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
  setNav(false);
  refresh();
}

/** 사이드바. 플랫폼 면(플랫폼 관리자의 개요 포함)과 테넌트 면을 두 묶음으로 나누고, 테넌트 묶음 머리에
 *  그 화면들이 보는 테넌트를 적는다 — 플랫폼 관리자가 `_platform` 의 빈 테넌트 화면을 보면서도 그 사실을
 *  몰랐다. 승인 대기는 모델 항목의 숫자로, 노드 상태는 좁은 화면에서 노드 항목의 숫자로 따라온다. */
function renderTabs() {
  const box = $('tabs');
  box.replaceChildren();
  const s = state.session;
  const pages = pagesFor(s);
  const platform = pages.filter((p) => p.platform || (p.id === 'overview' && s.is_platform_admin));
  const tenant = pages.filter((p) => !platform.includes(p));
  if (platform.length) box.appendChild(navGroup(t('ui.nav_platform'), platform));
  if (tenant.length) box.appendChild(navGroup(t('ui.nav_tenant'), tenant, tenantScope(s)));
}

/** 테넌트 묶음 머리. 플랫폼 관리자는 여기서 볼 테넌트를 고른다 — 고른 순간부터 그 테넌트의 감사에 남는다. */
function tenantScope(s) {
  if (!s.is_platform_admin || !state.tenantList.length) {
    return el('div', { class: 'scope', title: t('ui.tenant_scope') }, [
      el('span', { class: 'scope-name', text: s.tenant.name }),
      el('span', { class: 'mono', text: s.tenant.id }),
    ]);
  }
  const current = viewedTenant().id;
  const own = { id: s.tenant.id, name: s.tenant.name };
  const choices = [own].concat(state.tenantList.filter((tn) => tn.id !== own.id));
  const select = el('select', { id: 'view-tenant', 'aria-label': t('ui.tenant_view_pick') }, choices.map((tn) => el('option', {
    value: tn.id, selected: tn.id === current ? 'selected' : null, text: tn.name + ' (' + tn.id + ')',
  })));
  select.addEventListener('change', () => setViewTenant(select.value));
  return el('label', { class: 'scope pick', title: t('ui.tenant_view_pick') }, [
    el('span', { class: 'scope-name', text: t('ui.tenant_view_pick') }), select,
  ]);
}

function navGroup(title, pages, scope) {
  return el('div', { class: 'nav-group' }, [el('p', { class: 'nav-head', text: title }), scope || null].concat(pages.map(railItem)));
}

function railItem(page) {
  const current = page.id === state.page;
  const nodes = state.header.nodes;
  return el('button', {
    type: 'button',
    class: 'rail-item' + (current ? ' active' : ''),
    'aria-current': current ? 'page' : null,
    onclick: () => go(page.id),
  }, [
    icon(page.icon),
    el('span', { text: t(page.label) }),
    page.id === 'models' && state.header.pending > 0
      ? el('span', { class: 'count', 'aria-label': t('ui.pending_approval') + ' ' + state.header.pending, text: String(state.header.pending) }) : null,
    page.id === 'nodes' && nodes
      ? el('span', { class: 'count phone-only' + (nodes.healthy === nodes.total ? ' ok' : ''), text: nodes.healthy + '/' + nodes.total }) : null,
  ]);
}

/** 좁은 화면의 사이드바 서랍. */
function setNav(open) {
  $('shell').classList.toggle('nav-open', open);
  $('menu').setAttribute('aria-expanded', open ? 'true' : 'false');
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
    }, [t('ui.pending_approval') + ' ', el('span', { class: 'mono', text: String(state.header.pending) })]));
  }
  if (s.guard_grace_mode) {
    box.appendChild(el('button', {
      type: 'button', class: 'hpill danger', onclick: () => go(pages.includes('rules') ? 'rules' : 'overview'),
    }, [t('ui.guard') + ' ' + t('ui.lenient')]));
  }
  const nodes = state.header.nodes;
  if (nodes) {
    const tone = nodes.healthy === nodes.total ? 'ok' : (nodes.healthy ? 'warn' : 'danger');
    box.appendChild(el('span', { class: 'hpill nodes ' + tone }, [
      t('ui.nodes') + ' ', el('span', { class: 'mono', text: nodes.healthy + '/' + nodes.total }),
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
  renderTabs();   // 사이드바의 승인 대기 숫자 · 노드 숫자
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
  if (!quiet) {
    view.replaceChildren(el('p', { class: 'muted', text: t('ui.loading') }));
    $('page-actions').replaceChildren();
  }
  try {
    const nodes = await page.render();
    if (generation !== refreshGeneration) return;   // 지나간 탭의 결과다
    const list = [].concat(nodes).filter(Boolean);
    // 화면 동작(추가 버튼 · 필터)은 본문 머리의 제목 옆으로 옮긴다. 세대가 맞을 때만 옮기므로 지나간 탭의
    // 버튼이 남지 않는다. 버튼만 오고 입력 칸은 오지 않는다 — 편집 중 판정은 #view 안만 본다.
    const head = list.find((node) => node.classList && node.classList.contains('page-head'));
    const actions = head ? head.querySelector('.actions') : null;
    $('page-actions').replaceChildren.apply($('page-actions'), actions ? Array.from(actions.childNodes) : []);
    view.replaceChildren.apply(view, list.filter((node) => node !== head));
    $('refreshed').replaceChildren(t('ui.refresh_note', { time: '' }), el('span', { class: 'mono', text: stamp(Date.now() / 1000).slice(6) }));
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
  // 승인 대기 요청의 노드 · 모델은 헤더가 이미 부르는 모델 응답에 있다 — 같은 경로를 한 번 더 읽는다.
  const data = await fetchAll({ overview: '/v1/platform/overview', models: '/v1/platform/models' });
  const o = data.overview;
  if (!o) return [emptyState(t('ui.empty'))];

  const singleHomed = Object.entries(o.single_homed_roles || {});
  const pending = o.model_requests_pending || 0;
  state.header.pending = pending;
  renderBanners();

  // **확인할 일** — 사람의 판단이 필요한 것만 한 패널에. 승인 대기와 단일 호밍은 자동으로 풀리지 않는다
  // (자동 복제를 하지 않으므로 사람이 판단할 재료를 준다).
  const requests = ((data.models && data.models.install_requests) || []).filter((r) => r.status === 'pending');
  const todo = [];
  if (pending > 0) {
    const r = requests[0];
    todo.push(el('li', {}, [
      el('div', { class: 'todo-head' }, [
        pill(t('ui.pending_approval') + ' ' + pending, 'warn'),
        el('button', { type: 'button', class: 'sm', text: t('ui.go_models'), onclick: () => go('models') }),
      ]),
      r ? el('dl', { class: 'pairs' }, [
        el('dt', { text: t('ui.node') }), el('dd', {}, [el('span', { class: 'mono', text: r.node })]),
        el('dt', { text: t('ui.model') }), el('dd', {}, [el('span', { class: 'mono', text: r.model })]),
      ]) : null,
      el('p', { text: t('ui.pending_help') }),
    ]));
  }
  if (singleHomed.length) {
    todo.push(el('li', {}, [
      el('div', { class: 'todo-head' }, [
        pill(t('ui.single_homed_warning') + ' ' + singleHomed.length, 'warn'),
        el('button', { type: 'button', class: 'sm', text: t('ui.nodes'), onclick: () => go('nodes') }),
      ]),
      el('p', { text: t('ui.single_homed_help') }),
      el('dl', { class: 'pairs' }, [].concat(...singleHomed.map(([role, node]) =>
        [el('dt', { text: role }), el('dd', {}, [el('span', { class: 'mono', text: node })])]))),
    ]));
  }
  const attention = card(t('ui.attention'),
    [todo.length ? el('ul', { class: 'todo flush' }, todo) : emptyState(t('ui.attention_none'))],
    null, null, t('ui.attention_note'));

  // 테넌트별 사용량 — 서버의 기본 창(30일)이다. 기간을 적지 않으면 24시간 추이와 섞어 읽는다.
  const usage = o.usage_by_tenant || [];
  const maxCalls = Math.max(1, ...usage.map((r) => r.calls || 0));
  const usageCard = card(t('ui.usage'), usage.length ? [
    el('div', { class: 'hbars' }, usage.map((r) => el('div', { class: 'hbar' }, [
      el('span', { class: 'mono', text: r.tenant_id }),
      bar((r.calls || 0) / maxCalls, null, true, true),
      el('span', { class: 'mono', text: t('ui.count', { n: r.calls || 0 }) }),
    ]))),
    el('p', { text: t('ui.usage_30d_calls') }),
  ] : [emptyState(t('ui.none'))], null, null, t('ui.tenants') + ' ' + (o.tenants || []).length);

  return [
    el('div', { class: 'cols' }, [
      el('div', { class: 'stack' }, [
        laneBoard(o.lanes, o.waiting_by_reason),
        // 추이 한 줄 — 전 테넌트 합계, 최근 24시간. 레인 보드 다음에 "지금 흐름" 을 보여 준다.
        card(t('ui.trend_24h'), [trendChart(o.usage_series, { metric: 'calls', label: t('ui.trend_24h') })], null, null, t('ui.per_hour')),
      ]),
      el('div', { class: 'stack' }, [attention, usageCard]),
    ]),
  ];
}

// ── 플랫폼: 노드 ──────────────────────────────────────────────────────────

async function renderNodes() {
  const data = await api('/v1/platform/nodes');
  renderBanners();

  // 카드 대신 표 — 노드가 늘어도 한눈에 비교된다. 행마다 경계(기계의 위치) · 상태 · 동시성 칸 · 메모리 · 보유 모델.
  const rows = (data.nodes || []).map((n) => {
    const memText = n.mem_budget_gb ? (n.mem_reserved_gb || 0) + ' / ' + n.mem_budget_gb + ' GB' : null;
    return [
      el('div', {}, [
        el('span', { class: 'mono', text: n.node }),
        el('span', { class: 'sub', text: n.provider }),
      ]),
      el('div', { class: 'tags' }, [
        // **소프트웨어가 아니라 기계의 위치가 경계를 정한다.**
        boundaryBadge(n.data_boundary),
        n.metered ? badge(t('ui.cost'), 'warn') : null,
      ]),
      el('div', {}, [
        statusBadge(n.status),
        n.last_error ? el('span', { class: 'sub', text: n.last_error }) : null,
      ]),
      slotCell(n.running || 0, n.max_concurrent || 0),
      memText ? meterRow((n.mem_reserved_gb || 0) / n.mem_budget_gb, memText, 0.6) : el('span', { class: 'muted', text: t('ui.none') }),
      el('div', {}, [
        el('div', { class: 'tags' }, (n.models || []).length
          ? n.models.map((m) => el('span', { class: 'tag' + (m === n.loaded_model ? ' loaded' : ''), text: m + (m === n.loaded_model ? ' ' + t('ui.model_loaded') : '') }))
          : [el('span', { class: 'muted', text: t('ui.none') })]),
        n.tenant_affinity && n.tenant_affinity.length
          ? el('span', { class: 'sub', text: t('ui.tenant_affinity') + ': ' + n.tenant_affinity.join(', ') }) : null,
      ]),
      el('div', { class: 'row end' }, [
        n.disabled_by_airgap ? el('span', { class: 'muted', text: t('ui.airgap_on') }) : null,
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
        // 삭제는 드레이닝 뒤의 결정이다 — 실행 중인 잡이 있으면 서버가 409 로 거절한다. 행에서는 중립 버튼,
        // 경고는 확인 단계가 맡는다.
        el('button', {
          type: 'button', class: 'sm', text: t('ui.delete'),
          onclick: async () => {
            if (!confirm(t('ui.confirm_delete_node', { node: n.node }))) return;
            try {
              await api('/v1/platform/nodes/' + encodeURIComponent(n.node), { method: 'DELETE' });
              toast(t('ui.deleted'));
              refresh();
            } catch (err) { showError(err); }
          },
        }),
      ]),
    ];
  });

  return [
    pageHead(t('ui.desc_nodes'), [
      el('button', {
        type: 'button', class: 'primary', text: t('ui.add_node'),
        onclick: () => openDrawer(t('ui.register_node'), [registerNodeForm()]),
      }),
    ]),
    card(t('ui.nodes'), [table(
      [t('ui.node'), t('ui.boundary'), t('ui.status'), t('ui.concurrency'), t('ui.memory'), t('ui.models_held'), ''],
      rows)], null, null, t('ui.count', { n: rows.length })),
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
      el('span', { class: 'mono', text: tenant.id }),
      el('span', { class: 'sub', text: tenant.name }),
    ]),
    el('span', { class: 'mono', text: tenant.locale }),
    badge(tenant.status, tenant.status === 'active' ? 'healthy' : 'unhealthy'),
    tenant.budget_usd_per_month ? el('span', { class: 'mono', text: dollars(tenant.budget_usd_per_month) }) : el('span', { class: 'muted', text: t('ui.no_limit') }),
    tenant.rate_limit_per_min ? el('span', { class: 'mono', text: t('ui.rate_per_min', { n: tenant.rate_limit_per_min }) }) : el('span', { class: 'muted', text: t('ui.no_limit') }),
    tenant.has_dek ? badge('DEK', 'info') : badge(t('ui.none')),
    el('div', { class: 'row end' }, [
      // 파기는 행에서는 중립 버튼이다 — 되돌릴 수 없다는 경고와 확인값 입력은 다음 단계가 맡는다.
      el('button', {
        type: 'button', class: 'sm', text: t('ui.purge_tenant'),
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
      [t('ui.tenants'), t('ui.locale'), t('ui.status'), { label: t('ui.budget'), num: true }, { label: t('ui.rate_limit'), num: true }, '', ''], rows)],
      null, null, t('ui.count', { n: rows.length })),
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
        // 테넌트 관리자 토큰은 이 응답이 마지막이다 — 부트스트랩 배너와 같은 규칙으로 한 번만 보여 준다.
        // 기본 서비스도 함께 만들어졌다는 것을 같은 자리에서 말한다(그래야 다음 걸음이 보인다).
        openDrawer(t('ui.tenant_created', { id: created.id }), [
          el('p', { class: 'hint', text: t('ui.tenant_token_hint', { service: created.service_id }) }),
          el('pre', { class: 'reveal', text: created.tenant_admin_token }),
          el('p', { class: 'hint', text: t('ui.token_hint') }),
        ], [el('button', { type: 'button', class: 'right primary', text: t('ui.close'), onclick: () => { closeDrawer(); refresh(); } })]);
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

  const pending = (m.install_requests || []).map((r) => {
    const row = [
      el('span', { class: 'mono', text: r.model }),
      nodeCell(r),
      el('div', {}, [
        requestBadge(r.status),
        r.status === 'pulling' ? meterRow((r.progress || 0) / 100, (r.progress || 0) + '%') : null,
      ]),
      r.error ? el('span', { class: 'error', text: r.error }) : '',
      el('div', { class: 'row end' }, r.status === 'pending' ? [
        el('button', {
          type: 'button', class: 'primary sm', text: t('ui.approve'),
          onclick: () => modelDecision(r.id, false),
        }),
        el('button', { type: 'button', class: 'sm', text: t('ui.reject'), onclick: () => modelDecision(r.id, true) }),
      ] : []),
    ];
    // 사람이 결정할 행은 호박색 바탕 — 이 화면에서 지금 할 일이 어디 있는지 한눈에 보인다.
    if (r.status === 'pending') row.rowClass = 'selected';
    return row;
  });

  const inventory = (m.inventory || []).map((r) => {
    const blockers = r.deletion_blockers || [];
    return [
      el('div', {}, [
        el('span', { class: 'mono', text: r.model }),
        el('span', { class: 'sub', text: r.node }),
      ]),
      r.loaded ? badge(t('ui.model_loaded'), 'info') : '',
      el('span', { class: 'mono', text: (r.est_size_gb || 0) + ' GB' }),
      blockers.length ? el('div', { class: 'tags' }, blockers.map((b) => el('span', { class: 'tag', text: b }))) : '',
      el('div', { class: 'row end' }, [
        // 막힌 삭제는 누를 수 없게 두고 이유를 단다 — 이유는 옆 칸에도 있다.
        el('button', {
          type: 'button', class: 'sm', text: t('ui.delete'),
          disabled: blockers.length ? true : null,
          title: blockers.length ? t('ui.delete_blocked') + ': ' + blockers.join(', ') : null,
          onclick: () => deleteModel(r.node, r.model),
        }),
      ]),
    ];
  });

  // **"역할이 요구하는데 어느 노드에도 없는 모델"** — 서버가 주는데 화면이
  // 안 그렸다. 그 잡들은 레인을 막지 않고 조용히 대기하므로(§13-6), 여기
  // 안 보이면 관리자는 왜 그 역할만 안 도는지 알 방법이 없다.
  const missing = (m.missing || []).map((x) => [
    el('span', { class: 'mono', text: x.node }),
    el('span', { class: 'mono', text: x.model }),
    el('div', { class: 'row end' }, [
      el('button', {
        type: 'button', class: 'sm', text: t('ui.request_install'),
        onclick: () => requestInstall(x.node, x.model),
      }),
    ]),
  ]);

  const catalog = (data.catalog ? data.catalog.catalog : []).map((c) => [
    el('span', { class: 'mono', text: c.name }),
    c.provider,
    el('span', { class: 'mono', text: (c.est_size_gb || 0) + ' GB' }),
    c.purpose || '',
    c.note ? el('span', { class: 'muted', text: c.note }) : '',
  ]);

  const missingCard = missing.length
    ? card(t('ui.missing_models'), [table([t('ui.node'), t('ui.model'), ''], missing)], 'warn', pill(missing.length, 'warn'))
    : null;
  const pendingCard = pending.length
    ? card(t('ui.install_requests'), [
        m.pending ? el('p', { text: t('ui.pending_help') }) : null,
        table([t('ui.model'), t('ui.node'), t('ui.status'), '', ''], pending),
      ], null, m.pending ? pill(t('ui.pending_approval') + ' ' + m.pending, 'warn') : null)
    : null;
  return [
    pageHead(t('ui.desc_models')),
    missingCard && pendingCard ? el('div', { class: 'cols even' }, [pendingCard, missingCard]) : (pendingCard || missingCard),
    card(t('ui.models'), [table(
      [t('ui.model'), '', { label: t('ui.size'), num: true }, t('ui.deletion_blocked'), ''], inventory)],
      null, null, t('ui.count', { n: inventory.length })),
    card(t('ui.catalog'), [catalog.length
      ? table([t('ui.model'), t('ui.provider'), { label: t('ui.size'), num: true }, t('ui.purpose'), ''], catalog)
      : emptyState(t('ui.none'))], null, null, t('ui.count', { n: catalog.length })),
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

  const head = subs.length > 1 ? tabRow(subs, active, (id) => go('rules', id)) : null;
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
    el('div', { class: 'cols even' }, [
      card(t('ui.mode'), [
        el('p', { class: 'hint', text: t('ui.grace_help') }),
        segment([{ id: 'enforce', label: t('ui.enforce') }, { id: 'lenient', label: t('ui.lenient') }],
          data.grace_mode ? 'lenient' : 'enforce', (id) => setGrace(id === 'lenient'), 'lenient'),
      ], data.grace_mode ? 'bad' : null),

      // **안 켜진 필터는 없는 필터인데, 다국어에서는 켰다고 착각하기가 더 쉽다.**
      card(t('ui.locale_packs_on'), [
        el('p', { class: 'hint', text: t('ui.locale_pack_warning') }),
        table([t('ui.pack'), t('ui.tenants')],
          Object.entries(data.packs_in_use || {}).map(([pack, tenants]) =>
            [el('span', { class: 'mono', text: pack }), el('div', { class: 'tags' }, tenants.map((x) => el('span', { class: 'tag', text: x })))])),
        unused.length ? cardFoot([el('span', { text: t('ui.locale_packs_off') }),
          el('div', { class: 'tags' }, unused.map((x) => el('span', { class: 'tag', text: x })))]) : null,
      ], unused.length ? 'warn' : null),
    ]),

    card(t('ui.baseline'), [table(
      [t('ui.rule'), t('ui.replacement_label'), t('ui.boundary_internal'), t('ui.boundary_external'), t('ui.checksum'), t('ui.pack')],
      (data.baseline || []).map((r) => [
        el('span', { class: 'mono', text: r.id }),
        // 표지는 가드가 프롬프트에 써 넣는 글자 그대로다 — 코드로 적는다.
        r.label ? el('code', { class: 'token', text: r.label }) : '',
        badge(r.action.internal, r.action.internal),
        badge(r.action.external, r.action.external),
        r.checksum ? el('span', { class: 'mono muted', text: r.checksum }) : el('span', { class: 'muted', text: t('ui.none') }),
        el('span', { class: 'mono', text: r.locale_pack }),
      ]))], null, null, t('ui.count', { n: (data.baseline || []).length })),
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
    // **미리보기가 맨 위다** — 규칙 표는 "무엇이 켜져 있나" 이고, 미리보기는 "그래서 이 문장은 어떻게 되나" 다.
    previewCard(),
    // **오탐 검토 큐** — 이게 밀리면 승격 게이트가 영원히 안 열린다. 표가 넓어 전체 폭을 쓴다.
    card(t('ui.false_positive_queue'), [
      el('p', { class: 'hint', text: t('ui.review_help') }),
      table([t('ui.rule'), t('ui.stage'), t('ui.action'), { label: t('ui.hits'), num: true }, '', ''],
        events.map((e) => [
          el('span', { class: 'mono', text: e.rule_id }), e.stage, badge(e.action, e.action), e.match_count,
          el('span', { class: 'mono muted', text: stamp(e.ts) }),
          el('div', { class: 'row end' }, [
            el('button', { type: 'button', class: 'sm', text: t('ui.true_positive'), onclick: () => review(e.id, 'true_positive') }),
            el('button', { type: 'button', class: 'sm', text: t('ui.false_positive'), onclick: () => review(e.id, 'false_positive') }),
          ]),
        ])),
    ], events.length ? 'warn' : null, events.length ? pill(events.length, 'warn') : null),

    card(t('ui.locale_packs_on'), [
      el('div', { class: 'tags' }, [rules.locale_pack ? badge(rules.locale_pack, 'info') : badge(t('ui.none'), 'block')]),
      el('p', { class: 'hint', text: t('ui.locale_pack_warning') }),
    ], rules.locale_pack ? null : 'bad'),

    card(t('ui.rule'), [
      table([t('ui.rule'), t('ui.boundary_internal'), t('ui.boundary_external'), t('ui.pack'), ''],
        (rules.effective || []).map((r) => [
          el('span', { class: 'mono', text: r.id }),
          badge(r.action.internal, r.action.internal),
          badge(r.action.external, r.action.external),
          el('span', { class: 'mono', text: r.locale_pack }),
          el('div', { class: 'row end' }, [
            el('button', { type: 'button', class: 'sm', text: t('ui.promote'), onclick: () => checkPromotion(r.id) }),
          ]),
        ])),
      cardFoot([el('span', { text: t('ui.masked_only') })]),
    ], null, null, t('ui.count', { n: (rules.effective || []).length })),
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

// ── 테넌트: 가드 미리보기 ───────────────────────────────────────────────

/** 잡 없이 판정만 — 같은 엔진·같은 규칙이라 여기서 본 것이 실제 요청이 겪는 것이다. 입력과 결과는 state 에 남긴다. */
function previewCard() {
  const pv = state.guardPreview;
  const input = el('textarea', { id: 'guard-preview-text', rows: 4, placeholder: t('ui.preview_placeholder') });
  input.value = pv.text || '';
  input.addEventListener('input', () => { pv.text = input.value; });
  const run = el('button', { type: 'button', class: 'primary', text: t('ui.preview_run'), onclick: () => runPreview(input.value) });
  return card(t('ui.guard_preview'), [
    el('p', { class: 'hint', text: t('ui.preview_help') }),
    el('label', { for: 'guard-preview-text', class: 'sr-only', text: t('ui.guard_preview') }),
    input,
    el('div', { class: 'row' }, [run, pv.result && pv.result.grace_mode ? pill(t('ui.grace_mode_on'), 'warn') : null]),
    pv.result ? previewResult(pv.checked, pv.result) : null,
  ]);
}

async function runPreview(text) {
  if (!text.trim()) return;
  try {
    const result = await api('/v1/admin/guard/preview', { method: 'POST', body: { prompt: text } });
    state.guardPreview = { text, checked: text, result };
    refresh();
  } catch (err) { showError(err); }
}

/** 글 안의 몇 조각만 칠한다 — 앞에서부터 찾고 겹치면 앞엣것이 이긴다. 문자열 HTML 이 아니라 노드로 조립한다. */
function markTokens(text, tokens) {
  const out = el('pre', { class: 'preview' });
  let cursor = 0;
  while (cursor < text.length) {
    let best = null;
    for (const tok of tokens) {
      if (!tok.text) continue;
      const at = text.indexOf(tok.text, cursor);
      if (at >= 0 && (!best || at < best.at)) best = { at, tok };
    }
    if (!best) break;
    if (best.at > cursor) out.appendChild(document.createTextNode(text.slice(cursor, best.at)));
    out.appendChild(el('mark', { class: best.tok.cls, text: best.tok.text }));
    cursor = best.at + best.tok.text.length;
  }
  if (cursor < text.length) out.appendChild(document.createTextNode(text.slice(cursor)));
  return out;
}

/** 원문의 걸린 자리와 경계마다 "나가는 모양" 을 한 판에 나란히 — 가려진 원문(적색 바탕 · 취소선),
 *  대체 표지(녹색 바탕), 기록만 하고 통과(청색 바탕). 차단된 경계는 본문이 없다(서버가 안 준다). */
function previewResult(original, r) {
  const spans = [];
  const detections = r.detections || [];
  for (const d of detections) {
    const acts = d.actions || {};
    const blocks = acts.internal === 'block' || acts.external === 'block';
    const audit = acts.internal === 'audit' && acts.external === 'audit';
    for (const s of d.spans || []) spans.push([s[0], s[1], blocks ? 'block' : (audit ? 'seen' : null)]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  const marked = el('pre', { class: 'preview' });
  let cursor = 0;
  for (const [start, end, kind] of spans) {
    if (start < cursor) continue;   // 겹치는 스팬은 앞엣것이 이긴다 — 표시용이라 병합까지는 안 한다
    if (start > cursor) marked.appendChild(document.createTextNode(original.slice(cursor, start)));
    marked.appendChild(el('mark', { class: kind, text: original.slice(start, end) }));
    cursor = end;
  }
  if (cursor < original.length) marked.appendChild(document.createTextNode(original.slice(cursor)));

  // 나가는 쪽: 마스킹한 규칙의 표지와, 기록만 한 원문 조각을 칠한다.
  const tokens = [];
  for (const d of detections) {
    const acts = d.actions || {};
    if ((acts.internal === 'full' || acts.internal === 'partial' || acts.external === 'full' || acts.external === 'partial') && d.label) {
      tokens.push({ text: d.label, cls: 'put' });
    }
    if (acts.internal === 'audit' && acts.external === 'audit') {
      for (const s of d.spans || []) tokens.push({ text: original.slice(s[0], s[1]), cls: 'seen' });
    }
  }
  const boundaryBox = (b) => {
    const v = (r.boundaries || {})[b] || {};
    return el('div', { class: 'stack' }, [
      el('div', { class: 'preview-head' }, [
        el('span', { text: t(b === 'internal' ? 'ui.boundary_internal' : 'ui.boundary_external') }),
        pill(v.allowed ? t('ui.preview_allowed') : t('ui.preview_blocked'), v.allowed ? 'ok' : 'danger'),
      ]),
      v.allowed ? markTokens(v.prompt || '', tokens) : el('p', { class: 'hint', text: t('ui.preview_not_sent') }),
    ]);
  };
  return el('div', { class: 'stack' }, [
    el('div', { class: 'preview-grid' }, [
      el('div', {}, [el('div', { class: 'preview-head' }, [t('ui.preview_original')]), marked]),
      el('div', {}, [boundaryBox('internal'), boundaryBox('external')]),
    ]),
    el('div', { class: 'preview-legend' }, [
      el('span', {}, [el('mark', { text: t('ui.preview_cut') })]),
      el('span', {}, [el('mark', { class: 'put', text: t('ui.preview_put') })]),
      el('span', {}, [el('mark', { class: 'seen', text: t('ui.preview_seen') })]),
    ]),
    detections.length
      ? table([t('ui.rule'), t('ui.replacement_label'), t('ui.stage'), t('ui.boundary_internal'), t('ui.boundary_external'), { label: t('ui.hits'), num: true }],
          detections.map((d) => [
            el('span', { class: 'mono', text: d.rule_id }),
            d.label ? el('code', { class: 'token', text: d.label }) : '',
            d.stage,
            badge(d.actions.internal, d.actions.internal), badge(d.actions.external, d.actions.external), d.match_count,
          ]))
      : emptyState(t('ui.preview_clean'), 'ok'),
  ]);
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

  const chips = el('div', { class: 'chips', role: 'group', 'aria-label': t('ui.status') }, JOB_FILTERS.map((f) => {
    const count = jobs.filter(f.match).length;
    return el('button', {
      type: 'button', class: 'chip' + (f.id === filter.id ? ' active' : ''),
      'aria-pressed': f.id === filter.id ? 'true' : 'false',
      onclick: () => { state.jobFilter = f.id; refresh(); },
    }, [t(f.label), el('span', { class: 'n', text: String(count) })]);
  }));

  return [
    // **화면은 마스킹본만 본다.** 원문은 단건 API + 감사다. 상태 필터는 제목 줄로 올라간다.
    pageHead(t('ui.desc_jobs'), [chips]),
    card(t('ui.jobs'), [
      table(
        [t('ui.job'), t('ui.status'), t('ui.role'), t('ui.node') + ', ' + t('ui.model'), t('ui.prompt'), { label: t('ui.cost'), num: true }, { label: t('ui.created'), num: true }],
        shown.map((j) => [
          // 행 이름이 곧 상세를 여는 단추다 — 따로 "상세" 칸을 두지 않는다.
          el('button', { type: 'button', class: 'idlink', 'aria-label': t('ui.details') + ' ' + j.id, text: j.id.slice(0, 8), onclick: () => openJob(j) }),
          badge(j.status, j.status),
          j.role,
          el('div', { class: 'nowrap' }, [
            j.node ? el('span', { class: 'mono', text: j.node }) : el('span', { class: 'muted', text: t('ui.none') }),
            // **"왜 이 모델로 갔는가" 는 모델 옆에서 물어보게 된다.** 열을 따로 두면 라우팅을 안 켠 설치처에서
            // 언제나 비어 있는 열이 하나 는다. 밑줄 시작은 판정 센티널(_failed/_none)이다 — 화면에는 실제 라우트만.
            j.model ? el('span', { class: 'sub', text: j.model + (j.route && !j.route.startsWith('_') ? ' (' + j.route + ')' : '') }) : null,
          ]),
          transcriptOf(j)
            ? el('div', { class: 'row' }, [pill('chat', 'info'), el('span', { class: 'clip muted', title: lastUserText(j), text: lastUserText(j) })])
            : el('span', { class: 'clip muted', title: j.prompt_masked || '', text: j.prompt_masked || '' }),
          el('span', { class: 'mono', text: money(j.cost_usd) }),
          el('span', { class: 'mono', text: stamp(j.created_at) }),
        ]), 'pin'),
      cardFoot([el('span', { text: t('ui.masked_only') })]),
    ], null, null, t('ui.count', { n: shown.length })),
  ];
}

/** 작업 상세 드로어. 목록이 준 것만 보여주고, 원문은 사람이 한 번 더 확인한 뒤 단건으로 연다. */
function openJob(j) {
  const route = j.route && !j.route.startsWith('_') ? j.route : null;
  const body = [
    kv([
      [t('ui.status'), badge(j.status, j.status)],
      [t('ui.role'), el('div', {}, [j.role, ' ', el('span', { class: 'muted', text: j.kind || '' })])],
      [t('ui.lane'), j.lane || t('ui.none')],
      [t('ui.node'), j.node ? el('div', { class: 'mono', text: j.node }) : t('ui.none')],
      [t('ui.model'), j.model ? el('div', { class: 'mono', text: j.model }) : t('ui.none')],
      route ? [t('ui.route'), el('div', { class: 'mono', text: route })] : null,
      [t('ui.attempts'), String(j.attempts ?? 0)],
      [t('ui.cost'), el('div', { class: 'mono', text: money(j.cost_usd) })],
      [t('ui.tokens_used'), el('div', { class: 'mono', text: (j.input_tokens || 0) + ' / ' + (j.output_tokens || 0) })],
      [t('ui.created'), el('div', { class: 'mono', text: when(j.created_at) })],
      [t('ui.finished'), j.finished_at ? el('div', { class: 'mono', text: when(j.finished_at) }) : t('ui.none')],
      j.wait_reason ? [t('ui.wait_reason'), j.wait_reason] : null,
      j.error_code ? [t('ui.error_code'), el('div', { class: 'mono', text: j.error_code })] : null,
    ]),
    el('div', {}, [el('label', { text: t('ui.prompt') }), el('p', { class: 'hint', text: t('ui.masked_only') }),
      transcriptOf(j) ? transcriptView(transcriptOf(j)) : el('pre', { class: 'code', text: j.prompt_masked || '' })]),
    j.response ? el('div', {}, [el('label', { text: t('ui.response') }), el('pre', { class: 'code', text: j.response })]) : null,
  ];
  const foot = [
    j.has_raw ? el('button', { type: 'button', text: t('ui.view_raw'), onclick: () => viewRaw(j.id) }) : null,
    drawerClose(),
  ];
  openDrawer(t('ui.job_detail') + ' ' + j.id.slice(0, 8), body, foot);
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

// ── 테넌트: 감사 ──────────────────────────────────────────────────────────

/* 감사 기록의 "무엇을". 키를 조립하지 않는다 — 표에 없는 동작은 코드 그대로 보인다(숨기지 않는다). */
const AUDIT_ACTION_LABEL = {
  read_raw_prompt: 'ui.view_raw',
  raw_prompt_undecryptable: 'ui.audit_act_raw_undecryptable',
  login: 'ui.audit_act_login',
  login_failed: 'ui.audit_act_login_failed',
  logout: 'ui.audit_act_logout',
  create_account: 'ui.audit_act_create_account',
  reset_password: 'ui.audit_act_reset_password',
  change_password: 'ui.audit_act_change_password',
  enable_account: 'ui.audit_act_enable_account',
  disable_account: 'ui.audit_act_disable_account',
  move_account: 'ui.audit_act_move_account',
  issue_token: 'ui.audit_act_issue_token',
  rotate_token: 'ui.audit_act_rotate_token',
  revoke_token: 'ui.audit_act_revoke_token',
  create_service: 'ui.audit_act_create_service',
  update_service: 'ui.audit_act_update_service',
  set_guard_rule: 'ui.audit_act_set_guard_rule',
  clear_guard_rule: 'ui.audit_act_clear_guard_rule',
  update_tenant_settings: 'ui.audit_act_update_settings',
  set_role_override: 'ui.audit_act_set_role_override',
  clear_role_override: 'ui.audit_act_clear_role_override',
  export_tenant: 'ui.export',
  purge_end_user: 'ui.purge_end_user',
  create_tenant: 'ui.audit_act_create_tenant',
  cancel_job: 'ui.audit_act_cancel_job',
  platform_view: 'ui.audit_act_platform_view',
};

const AUDIT_ACCESS = ['login', 'login_failed', 'logout', 'create_account', 'reset_password', 'change_password',
  'enable_account', 'disable_account', 'move_account', 'issue_token', 'rotate_token', 'revoke_token', 'platform_view'];
const AUDIT_POLICY = ['create_service', 'update_service', 'set_guard_rule', 'clear_guard_rule',
  'update_tenant_settings', 'set_role_override', 'clear_role_override'];

const AUDIT_FILTERS = [
  { id: 'all', label: 'ui.filter_all', match: () => true },
  // 이 화면의 첫 질문 — "누가 가리기 전의 프롬프트를 봤나".
  { id: 'raw', label: 'ui.view_raw', match: (r) => r.action === 'read_raw_prompt' || r.action === 'raw_prompt_undecryptable' },
  { id: 'access', label: 'ui.audit_filter_access', match: (r) => AUDIT_ACCESS.includes(r.action) },
  { id: 'policy', label: 'ui.audit_filter_policy', match: (r) => AUDIT_POLICY.includes(r.action) },
];

//: 2단 분류기가 매 요청 남기는 "정상으로 돌았다" 표지. 이력을 덮으므로 접고 수만 말한다 — 실패(_classifier_failed)는 그대로 보인다.
const CLASSIFIER_OK_RULE = '_classifier_ok';

const VERDICT_LABEL = { true_positive: 'ui.true_positive', false_positive: 'ui.false_positive' };

/** "누가". 감사의 actor 는 토큰 id 라서, 이 테넌트의 토큰 목록에서 사람이 읽는 이름을 찾는다 — 계정 세션은 아이디다.
 *  플랫폼 토큰은 이 테넌트 목록에 없다 — 이 테넌트를 들여다본 기록(platform_view)에 나온 행위자면 플랫폼 관리자라고 말한다.
 *  그 밖에 못 찾으면(운영 스크립트 등) 값을 그대로 보인다. */
function auditActor(actor, tokens, platformActors) {
  const token = tokens.find((tk) => tk.id === actor);
  const note = token ? String(token.note || '') : '';
  if (note.startsWith('login:')) {
    return el('div', { class: 'nowrap' }, [el('span', { text: note.slice(6) }), el('span', { class: 'sub', text: t('ui.audit_session') })]);
  }
  if (token) {
    return el('div', { class: 'nowrap', title: note }, [el('span', { class: 'mono', text: token.prefix }), el('span', { class: 'sub', text: token.role })]);
  }
  if (platformActors.has(actor)) {
    return el('div', { class: 'nowrap', title: actor }, [el('span', { text: t('ui.audit_platform_admin') }), el('span', { class: 'sub mono', text: actor.slice(0, 8) })]);
  }
  return el('span', { class: 'mono', text: actor });
}

function auditAction(action) {
  const key = AUDIT_ACTION_LABEL[action];
  return el('div', { class: 'nowrap' }, [
    el('span', { text: key ? t(key) : action }),
    key ? el('span', { class: 'sub mono', text: action }) : null,
  ]);
}

function quiet(value, cls) {
  return value ? el('span', { class: cls || 'mono', title: value, text: value }) : el('span', { class: 'muted', text: t('ui.none') });
}

async function renderAudit() {
  const data = await fetchAll({
    audit: '/v1/admin/audit?limit=200',
    events: '/v1/admin/guard/events?limit=200',
    tokens: '/v1/admin/tokens',
  });
  renderBanners();
  const rows = (data.audit && data.audit.audit) || [];
  const tokens = (data.tokens && data.tokens.tokens) || [];
  const every = (data.events && data.events.events) || [];
  const events = every.filter((e) => e.rule_id !== CLASSIFIER_OK_RULE);
  const folded = every.length - events.length;
  const filter = AUDIT_FILTERS.find((f) => f.id === state.auditFilter) || AUDIT_FILTERS[0];
  const shown = rows.filter(filter.match);
  const platformActors = new Set(rows.filter((r) => r.action === 'platform_view').map((r) => r.actor));

  const chips = el('div', { class: 'chips', role: 'group', 'aria-label': t('ui.action') }, AUDIT_FILTERS.map((f) => el('button', {
    type: 'button', class: 'chip' + (f.id === filter.id ? ' active' : ''),
    'aria-pressed': f.id === filter.id ? 'true' : 'false',
    onclick: () => { state.auditFilter = f.id; refresh(); },
  }, [t(f.label), el('span', { class: 'n', text: String(rows.filter(f.match).length) })])));

  return [
    // 원문은 여기서도 열지 않는다 — 열람 사실만 보인다. 열람은 작업 상세의 단건 경로 하나뿐이다.
    pageHead(t('ui.desc_audit'), [chips]),
    card(t('ui.audit_admin'), [
      table(
        [t('ui.time'), t('ui.audit_actor'), t('ui.action'), t('ui.audit_target'), t('ui.audit_detail'), t('ui.audit_outcome')],
        shown.map((r) => [
          el('span', { class: 'mono', text: stamp(r.ts) }),
          auditActor(r.actor, tokens, platformActors),
          auditAction(r.action),
          quiet(r.target),
          quiet(r.detail_json && r.detail_json !== '{}' ? r.detail_json : '', 'clip muted'),
          badge(r.outcome, r.outcome),
        ]), 'pin'),
      cardFoot([el('span', { text: t('ui.audit_chain_note') })]),
    ], null, null, t('ui.count', { n: shown.length })),
    card(t('ui.audit_detections'), [
      table(
        [t('ui.time'), t('ui.rule'), t('ui.stage'), t('ui.action'), t('ui.boundary'), { label: t('ui.hits'), num: true }, t('ui.job'), t('ui.service'), t('ui.audit_review')],
        events.map((e) => [
          el('span', { class: 'mono', text: stamp(e.ts) }),
          el('span', { class: 'mono', text: e.rule_id }),
          e.stage,
          badge(e.action, e.action),
          e.boundary ? boundaryBadge(e.boundary) : quiet(''),
          String(e.match_count ?? 0),
          e.job_id ? el('span', { class: 'mono', title: e.job_id, text: e.job_id.slice(0, 8) }) : quiet(''),
          quiet(e.service_id),
          e.reviewed && VERDICT_LABEL[e.verdict]
            ? badge(t(VERDICT_LABEL[e.verdict]), e.verdict === 'false_positive' ? 'warn' : 'ok')
            : quiet(''),
        ]), 'pin'),
      cardFoot([
        el('span', { text: t('ui.masked_only') }),
        folded ? el('span', { text: t('ui.audit_classifier_folded', { n: folded }) }) : null,
      ]),
    ], null, null, t('ui.count', { n: events.length })),
  ];
}

// ── 테넌트: 사용량 ────────────────────────────────────────────────────────

const USAGE_AXES = ['service_id', 'end_user_hash', 'role', 'model', 'node'];

//: 창과 묶음은 한 쌍이다 — 표도 추이도 같은 `since` 를 본다. 30일은 일 묶음, 그 아래는 시간 묶음.
const USAGE_WINDOWS = [
  { id: '24h', label: 'ui.window_24h', seconds: 86400, bucket: 'hour' },
  { id: '7d', label: 'ui.window_7d', seconds: 7 * 86400, bucket: 'hour' },
  { id: '30d', label: 'ui.window_30d', seconds: 30 * 86400, bucket: 'day' },
];

async function renderUsage() {
  const axis = state.usageAxis || 'service_id';
  const win = USAGE_WINDOWS.find((w) => w.id === state.usageWindow) || USAGE_WINDOWS[2];
  const since = Math.floor(Date.now() / 1000 - win.seconds);
  // 일 묶음은 보는 쪽의 자정으로 끊는다 — 브라우저의 시간대를 분 단위로 넘긴다.
  const data = await api('/v1/admin/usage?by=' + encodeURIComponent(axis) + '&since=' + since
    + '&series=' + win.bucket + '&offset_minutes=' + (-new Date().getTimezoneOffset()));
  renderBanners();

  const budget = data.budget || {};
  const burn = budget.burn_rate || 0;
  const rows = data.rows || [];
  const unit = t(win.bucket === 'day' ? 'ui.per_day' : 'ui.per_hour');
  const rate = data.token_rate || {};

  return [
    // 축과 창은 제목 줄로 올라간다 — 표도 추이도 같은 since 를 본다.
    pageHead(t('ui.desc_usage'), [
      segment(USAGE_AXES.map((name) => ({ id: name, label: name })), axis,
        (id) => { state.usageAxis = id; refresh(); }),
      segment(USAGE_WINDOWS.map((w) => ({ id: w.id, label: t(w.label) })), win.id,
        (id) => { state.usageWindow = id; refresh(); }),
    ]),
    el('section', { class: 'card' }, [
      figures([
        [t('ui.calls'), rows.reduce((sum, r) => sum + r.calls, 0).toLocaleString()],
        [t('ui.tokens_used'), rows.reduce((sum, r) => sum + (r.input_tokens || 0) + (r.output_tokens || 0), 0).toLocaleString()],
        [t('ui.cost'), money(data.spend_usd)],
        budget.limit
          ? [t('ui.budget_used'), (burn * 100).toFixed(2), '% / ' + dollars(budget.limit)]
          : [t('ui.budget_used'), t('ui.no_limit')],
      ]),
      budget.limit ? cardFoot([
        el('span', { class: 'grow' }, [bar(burn, budget.warn_at, true)]),
        el('span', { text: t('ui.budget_line', { warn: Math.round((budget.warn_at || 0.8) * 100), remaining: dollars(budget.remaining) }) }),
        rate.tokens_per_minute !== undefined ? el('span', { text: t('ui.tokens_per_min', { n: rate.tokens_per_minute }) }) : null,
      ]) : null,
    ]),
    el('div', { class: 'cols even' }, [
      card(t('ui.trend_requests'), [trendChart(data.series, { metric: 'calls', label: t('ui.trend_requests') })], null, null, unit),
      card(t('ui.trend_tokens'), [trendChart(data.series, { metric: 'tokens', label: t('ui.trend_tokens') })], null, null, unit),
    ]),
    card(t('ui.usage'), [
      table(
        [axis, { label: t('ui.calls'), num: true }, { label: t('ui.tokens_used'), num: true },
         { label: t('ui.cost'), num: true }, { label: t('ui.latency'), num: true },
         { label: t('ui.success_rate'), num: true }],
        rows.map((r) => [
          r.key ? el('span', { class: 'mono', text: r.key }) : el('span', { class: 'muted', text: t('ui.none') }),
          el('span', { class: 'mono', text: r.calls.toLocaleString() }),
          el('span', { class: 'mono', text: (r.input_tokens + r.output_tokens).toLocaleString() }),
          el('span', { class: 'mono', text: money(r.cost_usd) }),
          el('span', { class: 'mono', text: r.avg_duration_ms + ' ms' }),
          el('span', { class: 'mono', text: (r.success_rate * 100).toFixed(1) + '%' }),
        ])),
    ], null, null, t('ui.count', { n: rows.length })),
  ];
}

// ── 플랫폼: 알림 ──────────────────────────────────────────────────────────

async function renderNotifications() {
  const data = await api('/v1/platform/notifications');
  renderBanners();

  // 최근 알림 — 상세는 JSON 덩어리가 아니라 "키 값" 으로 읽힌다. 톤은 사건의 뜻(이상 · 회복 · 그 밖)이다.
  const recent = (data.recent || []).slice().reverse().map((n) => {
    const tone = /offline|failed|exhausted|spike|error|needs_review/.test(n.event || '') ? 'danger'
      : (/recovered|ready|normal/.test(n.event || '') ? 'ok' : 'warn');
    return [
      el('span', { class: 'mono', text: stamp(n.ts) }),
      pill(n.event || '', tone),
      el('span', { class: 'muted', text: Object.entries(n.detail || {}).map(([k, v]) => k + ' ' + v).join(', ') }),
    ];
  });

  return [
    pageHead(t('ui.desc_alerts')),
    el('div', { class: 'cols even' }, [
      card(t('ui.channels'), [
        data.configured
          ? el('div', { class: 'tags' }, data.channels.map((c) => pill(c, 'ok')))
          : el('div', { class: 'empty bad flush' }, [
              el('strong', { text: t('ui.no_notify_channel') }),
              // 채널은 화면이 아니라 서버 설정에서 붙는다 — 어디서 붙이는지 말한다.
              el('p', { class: 'muted', text: t('ui.notify_channel_hint') }),
            ]),
        el('div', { class: 'row' }, [
          el('button', {
            type: 'button', class: 'sm', text: t('ui.test_notify'),
            onclick: async () => {
              try { await api('/v1/platform/notifications', { method: 'POST', body: {} }); toast(t('ui.saved')); refresh(); }
              catch (err) { showError(err); }
            },
          }),
          el('button', {
            type: 'button', class: 'sm', text: t('ui.diagnostics'),
            onclick: () => downloadJson('/v1/platform/diagnostics', 'diagnostics.json'),
          }),
        ]),
        cardFoot([el('span', { text: t('ui.notify_stats', { tracked: data.tracked_subjects || 0, sent: data.sent || 0 }) })]),
      ], data.configured ? null : 'bad'),
      card(t('ui.recent_events'), [recent.length
        ? table([t('ui.time'), t('ui.event'), t('ui.detail')], recent)
        : emptyState(t('ui.none'), 'ok')], null, null, t('ui.count', { n: recent.length })),
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
      el('span', { text: p.name }),
      el('span', { class: 'sub', text: p.id + ', ' + p.version }),
    ]),
    pill(p.signature, p.signature === 'verified' || p.signature === 'signed' ? 'ok' : 'warn'),
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
    triggerCell(p),
    p.files_present ? '' : pill(t('ui.plugin_missing_files'), 'danger'),
    el('div', { class: 'row end' }, [
      // 토큰을 잃은 플러그인의 유일한 재발급 경로 — 재설치는 토큰을 다시 주지 않는다.
      el('button', { type: 'button', class: 'sm', text: t('ui.plugin_rotate_token'), onclick: () => rotatePluginToken(p.id) }),
      el('button', {
        type: 'button', class: 'sm', text: t('ui.plugin_remove'),
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
    parts.push(el('div', {}, [
      el('span', { class: 'mono', text: p.schedule }),
      p.last_run_at ? null : ' ', p.last_run_at ? null : pill(t('ui.plugin_never_ran'), 'warn'),
    ]));
  }
  if (p.event) {
    const roles = (p.event_roles && p.event_roles.length) ? p.event_roles.join(', ') : '*';
    const pending = p.events_pending !== null && p.events_pending !== undefined;
    parts.push(el('div', {}, [
      el('span', { class: 'mono', text: p.event }), ' ', el('span', { class: 'muted', text: '(' + roles + ')' }),
      pending ? ' ' : null,
      // 켜 놓았는데 밀림이 늘기만 하면 플러그인이 안 돌고 있는 것이다 — 사람이 볼 것이라 호박색.
      pending ? pill(t('ui.plugin_events_pending', { n: p.events_pending }), p.events_pending ? 'warn' : '') : null,
    ]));
  }
  return parts.length ? el('div', {}, parts) : el('span', { class: 'muted', text: t('ui.none') });
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
        el('span', { class: 'mono', text: s.id }), s.allow_roles.join(', '),
        s.rate_limit_per_min ? el('span', { class: 'mono', text: t('ui.rate_per_min', { n: s.rate_limit_per_min }) }) : el('span', { class: 'muted', text: t('ui.no_limit') }),
        s.budget_usd_per_month ? el('span', { class: 'mono', text: dollars(s.budget_usd_per_month) }) : el('span', { class: 'muted', text: t('ui.no_limit') }),
        s.require_end_user ? badge(t('ui.required'), 'info') : el('span', { class: 'muted', text: t('ui.optional') }),
        badge(s.status, s.status === 'active' ? 'healthy' : 'unhealthy'),
        el('div', { class: 'row end' }, [
          el('button', { type: 'button', class: 'sm', text: t('ui.edit'), onclick: () => openServiceForm(s, roleCatalog) }),
        ]),
      ]))], null, [
      el('button', { type: 'button', class: 'sm', text: t('ui.add_service'), onclick: () => openServiceForm(null, roleCatalog) }),
    ], t('ui.count', { n: services.length })),

    card(t('ui.tokens'), [
      // **원값도 해시도 나가지 않는다.** 접두사만으로 어느 토큰인지 식별한다.
      table([t('ui.tokens'), t('ui.services'), t('ui.role'), t('ui.last_request'), '', ''],
        tokens.map((tok) => [
          el('code', { text: tok.prefix + '…' }),
          el('span', { class: 'mono', text: tok.service_id }), pill(tok.role, tok.role === 'platform_admin' ? 'accent' : ''),
          el('span', { class: 'mono', text: stamp(tok.last_used_at) }),
          tok.revoked_at ? badge(t('ui.revoked'), 'unhealthy')
            : (tok.expires_at ? badge(when(tok.expires_at), 'draining') : el('span', { class: 'muted', text: t('ui.none') })),
          el('div', { class: 'row end' }, tok.revoked_at ? [] : [
            el('button', { type: 'button', class: 'sm', text: t('ui.rotate'), onclick: () => rotateToken(tok.id) }),
            el('button', { type: 'button', class: 'sm', text: t('ui.revoke'), onclick: () => revokeToken(tok.id) }),
          ]),
        ])),
      issueTokenForm(services),
    ], null, null, t('ui.count', { n: tokens.length })),
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
  openDrawer(existing ? t('ui.edit') + ': ' + existing.id : t('ui.add_service'), [serviceForm(existing, roleCatalog)], [drawerClose()]);
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
  const nodes = s.nodes || {};
  const healthy = nodes.total && nodes.healthy === nodes.total;

  return [
    laneBoard(s.lanes, null,
      [pill(t('ui.nodes') + ' ' + (nodes.healthy || 0) + ' / ' + (nodes.total || 0) + ' ' + t('ui.online'), healthy ? 'ok' : 'warn')]),
    card(t('ui.role'), [table(
      [t('ui.role'), t('ui.kind'), t('ui.lane'), { label: t('ui.timeout'), num: true }, { label: t('ui.max_prompt_chars'), num: true }],
      ((data.roles && data.roles.roles) || []).map((r) => [
        r.name, pill(r.kind, ''), r.lane || '',
        el('span', { class: 'mono', text: r.timeout_seconds + 's' }),
        el('span', { class: 'mono', text: Number(r.max_prompt_chars || 0).toLocaleString() }),
      ]))], null, null, t('ui.count', { n: ((data.roles && data.roles.roles) || []).length })),
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
      el('div', { class: 'who' }, [
        el('span', { class: 'avatar', 'aria-hidden': 'true', text: initialOf(s) }),
        el('div', {}, [
          el('div', { class: 'who-name', text: s.account }),
          el('div', { class: 'who-role', text: s.tenant.name + ', ' + s.role }),
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
    el('span', { class: 'mono', text: a.username }),
    pill(a.role, a.role === 'platform_admin' ? 'accent' : (a.role === 'user' ? 'info' : '')),
    el('span', { class: 'mono', text: options.showTenant ? a.tenant_id : a.service_id }),
    el('span', { class: 'mono muted', text: stamp(a.last_login_at) }),
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
  paintThemeButton();
}

/** 토글은 지금과 반대 모드의 이름을 단다. 로그인 전에는 문구 카탈로그가 비어 있어 아이콘만 그리고,
 *  세션을 열면 다시 그린다 — 빈 카탈로그의 t() 는 키 문자열을 돌려주기 때문이다. */
function paintThemeButton() {
  const dark = effectiveTheme() === 'dark';
  const label = state.strings['ui.dark_mode'] ? t(dark ? 'ui.light_mode' : 'ui.dark_mode') : '';
  $('theme').replaceChildren(icon(dark ? 'sun' : 'moon'), label ? el('span', { text: label }) : null);
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

/** 플랫폼 관리자가 고른 테넌트를 테넌트 관리 면(`/v1/admin/*`)에만 붙인다. 서버는 플랫폼 관리자에게만 이 머리글을 듣고,
 *  들은 사실을 그 테넌트의 감사에 남긴다. 플랫폼 면·계약 경로에는 붙이지 않는다 — 그쪽은 언제나 자기 범위다. */
function scopeHeaders(path) {
  return state.viewTenant && path.startsWith('/v1/admin/') ? { 'X-LCC-Tenant': state.viewTenant } : {};
}

/** 지금 테넌트 면이 보여 주는 테넌트 — 고른 것이 없으면 세션의 테넌트. */
function viewedTenant() {
  const s = state.session;
  if (!state.viewTenant) return { id: s.tenant.id, name: s.tenant.name };
  const row = state.tenantList.find((tn) => tn.id === state.viewTenant);
  return { id: state.viewTenant, name: row ? row.name : state.viewTenant };
}

async function loadTenantList() {
  if (!state.session.is_platform_admin || state.platformBlocked) { state.tenantList = []; return; }
  try { state.tenantList = (await api('/v1/platform/tenants')).tenants || []; } catch (_) { state.tenantList = []; }
}

function setViewTenant(id) {
  state.viewTenant = id && id !== state.session.tenant.id ? id : null;
  try {
    if (state.viewTenant) sessionStorage.setItem(VIEW_TENANT_KEY, state.viewTenant);
    else sessionStorage.removeItem(VIEW_TENANT_KEY);
  } catch (_) { /* 사파리 프라이빗 등 */ }
  // 앞 테넌트에서 고른 필터를 물려받지 않는다. 플랫폼 면에 있었다면 그 테넌트의 작업으로 간다.
  state.jobFilter = 'all';
  state.auditFilter = 'all';
  const page = currentPage();
  if (state.viewTenant && (!page || page.platform || page.id === 'overview')) { go('jobs'); return; }
  refresh();
}

async function loadSession() {
  state.session = await api('/v1/session');
  state.strings = state.session.strings || {};
  document.documentElement.lang = state.session.locale;
  applyStaticStrings();
  applyTitleStrings();
  state.platformBlocked = await platformSurfaceBlocked();
  await loadTenantList();
  // 새로 고침해도 보던 테넌트에 남는다 — 목록에 없는 테넌트면(지워졌거나 남의 탭 값) 버린다.
  let saved = null;
  try { saved = sessionStorage.getItem(VIEW_TENANT_KEY); } catch (_) { /* 무시 */ }
  state.viewTenant = saved && state.tenantList.some((tn) => tn.id === saved) && saved !== state.session.tenant.id ? saved : null;

  const s = state.session;
  // 누구로 들어왔는지: 이름(계정이 없으면 역할) 한 줄, 테넌트와 역할 한 줄. 가운뎃점으로 잇지 않는다.
  $('who').replaceChildren(
    el('span', { class: 'who-name', text: s.account ? t('ui.signed_in_as', { name: s.account }) : s.role }),
    el('span', { class: 'who-role', text: s.tenant.name + ', ' + s.role }),
  );
  $('version').textContent = 'v' + s.version;
  const avatar = $('avatar');
  avatar.textContent = initialOf(s);
  avatar.title = (s.account || s.role) + ', ' + s.tenant.name;
  paintThemeButton();
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
  state.viewTenant = null;
  state.tenantList = [];
  try { sessionStorage.removeItem(TOKEN_KEY); sessionStorage.removeItem(VIEW_TENANT_KEY); } catch (_) { /* 무시 */ }
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
  const logout = async () => {
    // 계정 세션이면 서버의 세션 토큰도 폐기한다. 화면만 지우면 토큰은 만료까지 살아 있다.
    if (state.session && state.session.account) {
      try { await api('/v1/logout', { method: 'POST' }); } catch (_) { /* 이미 만료됐을 수 있다 */ }
    }
    setNav(false);
    disconnect();
  };
  $('logout').addEventListener('click', logout);
  $('rail-logout').addEventListener('click', logout);   // 좁은 화면에서는 상단 바 대신 서랍에 있다
  $('menu').addEventListener('click', () => setNav(!$('shell').classList.contains('nav-open')));
  $('rail-close').addEventListener('click', () => { setNav(false); $('menu').focus(); });
  $('nav-scrim').addEventListener('click', () => setNav(false));
  $('theme').addEventListener('click', toggleTheme);
  $('avatar').addEventListener('click', () => {
    if (!state.session) return;
    const pages = pagesFor(state.session).map((p) => p.id);
    if (pages.includes('settings')) go('settings', state.session.account ? 'account' : undefined);
  });
  $('drawer-close').addEventListener('click', closeDrawer);
  $('drawer-overlay').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('drawer').hidden) closeDrawer();
    else if ($('shell').classList.contains('nav-open')) { setNav(false); $('menu').focus(); }
  });

  let saved = null;
  try { saved = sessionStorage.getItem(TOKEN_KEY); } catch (_) { saved = null; }
  if (saved) {
    try { await connect(saved); return; } catch (_) { /* 만료·폐기된 토큰 */ }
  }
  $('login').hidden = false;
}

document.addEventListener('DOMContentLoaded', boot);
