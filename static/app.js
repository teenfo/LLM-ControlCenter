/* 관제 UI — 의존성 없는 바닐라 JS.
 *
 * 세 가지가 이 파일의 요지다.
 *
 * ① **화면은 마스킹본만 본다.** 원문은 단건 API 로만 열고, 여는 순간 감사에 남는다.
 * ② **조용한 실패를 시끄럽게 만든다.** 안 켜진 로케일 팩 · 안 붙은 2단 분류기 ·
 *    없는 알림 채널 · 단일 호밍 역할 — 전부 상시 배너로 띄운다. 관제 센터가
 *    안 보여주면 사람이 판단할 수 없고, 다국어에서는 켰다고 착각하기가 더 쉽다.
 * ③ **외부 CDN 도 프레임워크도 없다.** 에어갭에서 그대로 떠야 한다.
 */

'use strict';

const TOKEN_KEY = 'llmcc.token';

const state = {
  token: null,
  session: null,
  tab: null,
  strings: {},
  refreshTimer: null,
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

function card(title, children, cls) {
  return el('section', { class: 'card' + (cls ? ' ' + cls : '') },
    [title ? el('h2', { text: title }) : null].concat(children || []));
}

function stat(label, value, tone) {
  return card(label, [el('div', { class: 'stat' + (tone ? ' ' + tone : ''), text: String(value) })]);
}

function badge(text, cls) {
  return el('span', { class: 'badge' + (cls ? ' ' + cls : ''), text: String(text) });
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

function boundaryBadge(boundary) {
  return badge(t(BOUNDARY_LABEL[boundary] || 'ui.unknown'), boundary);
}

function statusBadge(status) {
  return badge(t(STATUS_LABEL[status] || 'ui.unknown'), status);
}

function table(headers, rows) {
  if (!rows.length) return el('p', { class: 'muted', text: t('ui.empty') });
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

function bar(ratio, warnAt) {
  const pct = Math.max(0, Math.min(1, ratio || 0));
  const tone = pct >= 1 ? 'bad' : (warnAt && pct >= warnAt ? 'warn' : '');
  return el('div', { class: 'bar' }, [
    el('span', { class: tone, style: 'width:' + (pct * 100).toFixed(1) + '%' }),
  ]);
}

function when(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

function money(value) {
  return '$' + (Number(value) || 0).toFixed(4);
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
  const warnings = [];

  if (s.airgap) warnings.push([t('ui.airgap_on'), '']);
  if (!s.guard_classifier_ready) warnings.push([t('ui.classifier_off'), 'bad']);
  if (!s.raw_prompt_storage) warnings.push([t('ui.raw_storage_off'), '']);
  if (!s.guard_locale_pack) {
    warnings.push([t('ui.locale_pack_warning'), 'bad']);
  }
  // 유예를 조용히 두면 그게 더 나쁘다 — 필터가 지키고 있다고 믿게 된다.
  if (s.guard_grace_mode) warnings.push([t('ui.grace_mode'), 'bad']);
  for (const line of extra || []) warnings.push(line);

  for (const [text, cls] of warnings) {
    box.appendChild(el('div', { class: 'banner' + (cls ? ' ' + cls : ''), text }));
  }
}

// ── 탭 ────────────────────────────────────────────────────────────────────

function tabsFor(session) {
  const tabs = [];
  if (session.is_platform_admin) {
    tabs.push(
      { id: 'overview', label: t('ui.overview'), render: renderPlatformOverview },
      { id: 'nodes', label: t('ui.nodes'), render: renderNodes },
      { id: 'tenants', label: t('ui.tenants'), render: renderTenants },
      { id: 'models', label: t('ui.models'), render: renderModels },
      { id: 'baseline', label: t('ui.baseline'), render: renderBaseline },
      { id: 'notify', label: t('ui.notifications'), render: renderNotifications },
      { id: 'plugins', label: t('ui.plugins'), render: renderPlugins },
    );
  }
  if (session.is_tenant_admin) {
    tabs.push(
      { id: 'connections', label: t('ui.connections'), render: renderConnections },
      { id: 'usage', label: t('ui.usage'), render: renderUsage },
      { id: 'guard', label: t('ui.guard'), render: renderGuard },
      { id: 'jobs', label: t('ui.jobs'), render: renderJobs },
      { id: 'data', label: t('ui.data'), render: renderData },
    );
  }
  if (!tabs.length) {
    tabs.push({ id: 'status', label: t('ui.dashboard'), render: renderConsumerStatus });
  }
  return tabs;
}

function renderTabs() {
  const nav = $('tabs');
  nav.replaceChildren();
  for (const tab of tabsFor(state.session)) {
    nav.appendChild(el('button', {
      class: tab.id === state.tab ? 'active' : null,
      text: tab.label,
      onclick: () => { state.tab = tab.id; refresh(); },
    }));
  }
}

//: 자동 갱신 주기(ms). **노드가 죽어도 새로고침 전까지 과거 화면을 본다** —
//: 관제 화면이 과거를 보여주면 그건 관제가 아니다.
const REFRESH_INTERVAL_MS = 15000;

//: 진행 중인 갱신의 세대. 탭을 빠르게 옮기면 **먼저 시작한 요청이 나중에 도착해**
//: 이전 탭의 내용을 현재 탭 위에 그린다 — 세대가 어긋난 결과는 버린다.
let refreshGeneration = 0;

async function refresh(options) {
  const quiet = options && options.quiet;
  const tabs = tabsFor(state.session);
  const tab = tabs.find((x) => x.id === state.tab) || tabs[0];
  state.tab = tab.id;
  renderTabs();

  const generation = ++refreshGeneration;
  const view = $('view');
  // 자동 갱신은 화면을 비우지 않는다 — 15초마다 깜빡이면 읽을 수가 없다.
  if (!quiet) view.replaceChildren(el('p', { class: 'muted', text: t('ui.loading') }));
  try {
    const nodes = await tab.render();
    if (generation !== refreshGeneration) return;   // 지나간 탭의 결과다
    view.replaceChildren.apply(view, [].concat(nodes));
  } catch (err) {
    if (generation !== refreshGeneration) return;
    if (!quiet) {
      showError(err);
      view.replaceChildren(el('p', { class: 'muted', text: t('ui.empty') }));
    }
  }
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
  if (!o) return [el('p', { class: 'muted', text: t('ui.empty') })];

  const singleHomed = Object.entries(o.single_homed_roles || {});
  const waiting = Object.entries(o.waiting_by_reason || {})
    .filter(([reason]) => reason !== 'none');

  const extraBanners = [];
  if (o.model_requests_pending > 0) {
    extraBanners.push([t('ui.pending_approval') + ': ' + o.model_requests_pending, '']);
  }
  renderBanners(extraBanners);

  const nodes = o.nodes || [];
  const lanes = Object.entries(o.lanes || {});

  return [
    el('div', { class: 'grid' }, [
      stat(t('ui.tenants'), (o.tenants || []).length),
      stat(t('ui.nodes'), nodes.filter((n) => n.status === 'healthy').length + ' / ' + nodes.length,
        nodes.some((n) => n.status === 'unhealthy') ? 'bad' : 'ok'),
      stat(t('ui.queued'), lanes.reduce((sum, [, l]) => sum + l.queued, 0)),
      stat(t('ui.pending_approval'), o.model_requests_pending || 0,
        o.model_requests_pending ? 'warn' : ''),
    ]),

    // **1급 카드 ①** — 자동 복제를 하지 않으므로 사람이 판단할 재료를 준다.
    card(t('ui.single_homed_warning'), [
      el('p', { class: 'hint', text: t('ui.single_homed_help') }),
      singleHomed.length
        ? table([t('ui.role'), t('ui.node')], singleHomed.map(([role, node]) => [role, node]))
        : el('p', { class: 'muted', text: t('ui.none') }),
    ], singleHomed.length ? 'warn' : null),

    // **1급 카드 ②** — "노드 정비로 대기 12건" 을 못 보여주면 관리자는 큐가 왜
    // 안 줄어드는지 알 수 없고, 노드를 늘리는 잘못된 대응을 한다.
    card(t('ui.waiting_reason'),
      waiting.length
        ? table([t('ui.waiting_reason'), { label: t('ui.queued'), num: true }], waiting)
        : el('p', { class: 'muted', text: t('ui.none') }),
      waiting.length ? 'warn' : null),

    card(t('ui.lane'), table(
      [t('ui.lane'), { label: t('ui.running'), num: true }, { label: t('ui.queued'), num: true },
       { label: t('ui.concurrency'), num: true }, t('ui.scan_truncated')],
      lanes.map(([name, l]) => [
        name, l.running, l.queued, l.max_concurrent,
        l.scan_truncated ? badge(t('ui.scan_truncated'), 'block') : '—',
      ]))),

    card(t('ui.usage'), table(
      [t('ui.tenants'), { label: t('ui.calls'), num: true },
       { label: t('ui.tokens_used'), num: true }, { label: t('ui.cost'), num: true }],
      (o.usage_by_tenant || []).map((r) => [
        r.tenant_id, r.calls,
        (r.input_tokens || 0) + (r.output_tokens || 0), money(r.cost_usd)]))),
  ];
}

// ── 플랫폼: 노드 ──────────────────────────────────────────────────────────

async function renderNodes() {
  const data = await api('/v1/platform/nodes');
  renderBanners();

  const rows = (data.nodes || []).map((n) => [
    n.node,
    badge(n.provider),
    // **소프트웨어가 아니라 기계의 위치가 경계를 정한다.**
    boundaryBadge(n.data_boundary),
    statusBadge(n.status),
    n.running + ' / ' + n.max_concurrent,
    n.mem_budget_gb ? n.mem_reserved_gb + ' / ' + n.mem_budget_gb + ' GB' : '—',
    n.metered ? badge(t('ui.cost'), 'external') : '—',
    n.tenant_affinity.length ? n.tenant_affinity.join(', ') : '—',
    el('div', { class: 'row' }, [
      el('button', {
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
    ]),
  ]);

  return [
    card(t('ui.nodes'), table(
      [t('ui.node'), '', t('ui.boundary_internal'), t('ui.status'), t('ui.load'),
       t('ui.memory'), t('ui.cost'), t('ui.tenants'), ''], rows)),
    card(t('ui.register_node'), [registerNodeForm()]),
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
    class: 'inline',
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
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    input('name', t('ui.node')),
    input('provider', 'provider'),
    el('div', {}, [el('label', { for: 'node-boundary', text: t('ui.boundary_internal') }), boundary]),
    input('base_url', 'base_url', 'url'),
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
    tenant.id, tenant.name, tenant.locale,
    badge(tenant.status, tenant.status === 'active' ? 'healthy' : 'unhealthy'),
    tenant.budget_usd_per_month ? money(tenant.budget_usd_per_month) : '—',
    tenant.has_dek ? badge('DEK', 'internal') : badge(t('ui.none')),
    el('button', {
      class: 'danger', text: t('ui.purge_tenant'),
      onclick: () => purgeTenant(tenant.id),
    }),
  ]);

  return [
    card(t('ui.tenants'), table(
      [t('ui.tenants'), '', 'locale', t('ui.status'), t('ui.budget'), '', ''], rows)),
    card(t('ui.create'), [createTenantForm(data)]),
  ];
}

function createTenantForm() {
  const id = el('input', { id: 'tenant-id' });
  const name = el('input', { id: 'tenant-name' });
  const locale = el('select', { id: 'tenant-locale' },
    state.session.available_locales.map((code) =>
      el('option', { value: code, text: code })));

  return el('form', {
    class: 'inline',
    onsubmit: async (event) => {
      event.preventDefault();
      try {
        const created = await api('/v1/platform/tenants', {
          method: 'POST',
          body: { id: id.value.trim(), name: name.value.trim(), locale: locale.value },
        });
        // 만들 때부터 어떤 로케일 팩이 켜졌는지 말해 준다.
        alert(t('ui.locale_packs_on') + ': ' + (created.guard_locale_pack || t('ui.none')));
        refresh();
      } catch (err) { showError(err); }
    },
  }, [
    el('div', {}, [el('label', { for: 'tenant-id', text: 'id' }), id]),
    el('div', {}, [el('label', { for: 'tenant-name', text: t('ui.tenants') }), name]),
    el('div', {}, [el('label', { for: 'tenant-locale', text: 'locale' }), locale]),
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
    refresh();
  } catch (err) { showError(err); }
}

// ── 플랫폼: 모델 ──────────────────────────────────────────────────────────

async function renderModels() {
  const data = await fetchAll({ models: '/v1/platform/models', catalog: '/v1/platform/catalog' });
  renderBanners();
  const m = data.models || { inventory: [], install_requests: [], missing: [] };

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
    : r.node;

  const pending = (m.install_requests || []).map((r) => [
    nodeCell(r), r.model,
    badge(r.status, r.status === 'failed' ? 'unhealthy' : (r.status === 'pulling' ? 'healthy' : '')),
    r.status === 'pulling' ? bar((r.progress || 0) / 100) : (r.error || '—'),
    el('div', { class: 'row' }, r.status === 'pending' ? [
      el('button', {
        class: 'primary', text: t('ui.approve'),
        onclick: () => modelDecision(r.id, false),
      }),
      el('button', { text: t('ui.reject'), onclick: () => modelDecision(r.id, true) }),
    ] : []),
  ]);

  const inventory = (m.inventory || []).map((r) => [
    r.node, r.model,
    r.loaded ? badge(t('ui.model_loaded'), 'healthy') : '',
    r.est_size_gb ? r.est_size_gb + ' GB' : '—',
    (r.deletion_blockers || []).join(', '),
    el('div', { class: 'row' }, [
      el('button', {
        class: 'danger', text: t('ui.delete'),
        onclick: () => deleteModel(r.node, r.model),
      }),
    ]),
  ]);

  // **"역할이 요구하는데 어느 노드에도 없는 모델"** — 서버가 주는데 화면이
  // 안 그렸다. 그 잡들은 레인을 막지 않고 조용히 대기하므로(§13-6), 여기
  // 안 보이면 관리자는 왜 그 역할만 안 도는지 알 방법이 없다.
  const missing = (m.missing || []).map((x) => [
    x.node, x.model,
    el('button', {
      class: 'primary', text: t('ui.request_install'),
      onclick: () => requestInstall(x.node, x.model),
    }),
  ]);

  return [
    missing.length
      ? card(t('ui.missing_models'), table([t('ui.node'), t('ui.model'), ''], missing))
      : null,
    pending.length
      ? card(t('ui.install_requests'), table(
          [t('ui.node'), t('ui.model'), t('ui.status'), t('ui.install_progress'), ''], pending))
      : null,
    card(t('ui.models'), table(
      [t('ui.node'), t('ui.model'), '', { label: t('ui.size'), num: true },
       t('ui.deletion_blocked'), ''], inventory)),
    card(t('ui.catalog'), table(
      [t('ui.model'), '', { label: '', num: true }, ''],
      (data.catalog ? data.catalog.catalog : []).map((c) =>
        [c.name, c.provider, c.est_size_gb + ' GB', c.purpose]))),
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
    refresh();
  } catch (err) {
    // **삭제 차단 사유를 그대로 보여준다** — `force` 가 없으므로 사유가 곧 다음 할 일이다.
    showError(new Error(t('ui.delete_blocked') + ': ' + err.message));
  }
}

// ── 플랫폼: 가드 베이스라인 ───────────────────────────────────────────────

async function renderBaseline() {
  const data = await api('/v1/platform/guard/baseline');
  const unused = data.packs_unused || [];
  renderBanners(unused.length ? [[t('ui.locale_packs_off') + ': ' + unused.join(', '), 'bad']] : []);

  return [
    // **안 켜진 필터는 없는 필터인데, 다국어에서는 켰다고 착각하기가 더 쉽다.**
    card(t('ui.locale_packs_on'), [
      el('p', { class: 'hint', text: t('ui.locale_pack_warning') }),
      table([t('ui.locale_packs_on'), t('ui.tenants')],
        Object.entries(data.packs_in_use || {}).map(([pack, tenants]) =>
          [pack, tenants.join(', ')])),
      unused.length ? el('p', { class: 'muted', text: t('ui.locale_packs_off') + ': ' + unused.join(', ') }) : null,
    ], unused.length ? 'warn' : null),

    card(t('ui.grace_mode'), [
      el('div', { class: 'row' }, [
        badge(data.grace_mode ? t('ui.grace_mode') : t('ui.none'),
          data.grace_mode ? 'block' : 'healthy'),
        data.grace_mode ? el('button', {
          class: 'primary', text: t('ui.grace_mode_off'),
          onclick: async () => {
            try {
              await api('/v1/platform/guard/grace-mode', { method: 'POST', body: { enabled: false } });
              await loadSession();
              refresh();
            } catch (err) { showError(err); }
          },
        }) : null,
      ]),
    ], data.grace_mode ? 'bad' : null),

    card(t('ui.baseline'), table(
      [t('ui.rule'), '', t('ui.boundary_internal'), t('ui.boundary_external'), 'checksum', 'pack'],
      (data.baseline || []).map((r) => [
        r.id, r.label || '',
        badge(r.action.internal, r.action.internal),
        badge(r.action.external, r.action.external),
        r.checksum || '—', r.locale_pack,
      ]))),
  ];
}

// ── 플랫폼: 알림 ──────────────────────────────────────────────────────────

async function renderNotifications() {
  const data = await api('/v1/platform/notifications');
  renderBanners(data.configured ? [] : [[t('ui.no_notify_channel'), 'bad']]);

  return [
    card(t('ui.notifications'), [
      el('p', { class: data.configured ? 'muted' : 'error',
        text: data.configured ? data.channels.join(', ') : t('ui.no_notify_channel') }),
      el('div', { class: 'row' }, [
        el('button', {
          text: t('ui.test_notify'),
          onclick: async () => {
            try { await api('/v1/platform/notifications', { method: 'POST', body: {} }); refresh(); }
            catch (err) { showError(err); }
          },
        }),
        el('button', {
          text: t('ui.diagnostics'),
          onclick: () => downloadJson('/v1/platform/diagnostics', 'diagnostics.json'),
        }),
      ]),
    ], data.configured ? null : 'bad'),

    card(t('ui.audit'), table(
      ['', '', ''],
      (data.recent || []).slice().reverse().map((n) =>
        [when(n.ts), n.event, JSON.stringify(n.detail)]))),
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

  const picker = el('input', { type: 'file', accept: '.lccp,.zip' });
  const install = el('button', {
    text: t('ui.plugin_install_go'),
    onclick: async () => {
      const file = picker.files && picker.files[0];
      if (!file) return;
      try {
        const created = await api('/v1/platform/plugins', {
          method: 'POST', body: await file.arrayBuffer(),
        });
        // 토큰 원값은 이 응답이 마지막이다. 갱신으로 지워지기 전에 띄운다.
        window.alert(
          t('ui.plugin_installed_inactive') + '\n\n'
          + t('ui.plugin_token_once') + '\n' + created.token);
        refresh();
      } catch (err) { showError(err); }
    },
  });

  const rows = (data.plugins || []).map((p) => [
    p.name + ' ' + p.version,
    p.signature,
    p.active ? t('ui.plugin_active') : t('ui.plugin_inactive'),
    (p.allow_roles || []).join(', '),
    // 이 플러그인이 만든 잡 수. 트리거가 없는 지금도 "얼마나 쓰고 있나" 를 답한다.
    String(p.jobs_created || 0),
    // 스케줄이 있는데 아직 한 번도 안 돌았으면 그 사실이 보여야 한다.
    p.schedule ? p.schedule + (p.last_run_at ? '' : ' · ' + t('ui.plugin_never_ran')) : '',
    p.files_present ? '' : t('ui.plugin_missing_files'),
    el('div', { class: 'row' }, [
      el('button', {
        text: p.active ? t('ui.plugin_deactivate') : t('ui.plugin_activate'),
        onclick: async () => {
          try {
            await api('/v1/platform/plugins/' + encodeURIComponent(p.id) + '/activate',
              { method: 'POST', body: { active: !p.active } });
            refresh();
          } catch (err) { showError(err); }
        },
      }),
      el('button', {
        text: t('ui.plugin_remove'),
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
          [t('ui.plugin'), t('ui.plugin_signature'), t('ui.status'), t('ui.role'),
           t('ui.plugin_jobs'), t('ui.plugin_schedule'), '', ''],
          rows)]
      : [el('p', { class: 'muted', text: t('ui.plugin_none') })]),
  ];
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

// ── 테넌트: 연결 정보 ─────────────────────────────────────────────────────

async function renderConnections() {
  const data = await fetchAll({
    services: '/v1/admin/services',
    tokens: '/v1/admin/tokens',
    settings: '/v1/admin/settings',
    status: '/v1/status',
  });
  renderBanners();

  const services = (data.services && data.services.services) || [];
  const tokens = (data.tokens && data.tokens.tokens) || [];

  return [
    card(t('ui.services'), table(
      [t('ui.services'), t('ui.role'), t('ui.rate_limit'), t('ui.budget'), t('ui.end_users'), t('ui.status')],
      services.map((s) => [
        s.id, s.allow_roles.join(', '),
        s.rate_limit_per_min ? s.rate_limit_per_min + '/min' : '—',
        s.budget_usd_per_month ? money(s.budget_usd_per_month) : '—',
        s.require_end_user ? badge('required', 'internal') : '—',
        badge(s.status, s.status === 'active' ? 'healthy' : 'unhealthy'),
      ]))),

    card(t('ui.tokens'), [
      // **원값도 해시도 나가지 않는다.** 접두사만으로 어느 토큰인지 식별한다.
      table([t('ui.tokens'), t('ui.services'), t('ui.role'), t('ui.last_request'), '', ''],
        tokens.map((tok) => [
          el('code', { text: tok.prefix + '…' }),
          tok.service_id, tok.role, when(tok.last_used_at),
          tok.revoked_at ? badge(t('ui.revoke'), 'unhealthy')
            : (tok.expires_at ? badge(when(tok.expires_at), 'draining') : '—'),
          el('div', { class: 'row' }, tok.revoked_at ? [] : [
            el('button', { text: t('ui.rotate'), onclick: () => rotateToken(tok.id) }),
            el('button', { class: 'danger', text: t('ui.revoke'), onclick: () => revokeToken(tok.id) }),
          ]),
        ])),
      issueTokenForm(services),
    ]),
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

/** 발급된 토큰을 한 번만 보여준다. 다시 볼 수 없다. */
function revealOnce(value) {
  const view = $('view');
  view.prepend(card(t('ui.token_shown_once'), [
    el('pre', { class: 'reveal mono', text: value }),
    el('button', { text: t('ui.close'), onclick: () => refresh() }),
  ], 'warn'));
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
    refresh();
  } catch (err) { showError(err); }
}

// ── 테넌트: 사용량 ────────────────────────────────────────────────────────

async function renderUsage() {
  const axis = state.usageAxis || 'service_id';
  const data = await api('/v1/admin/usage?by=' + encodeURIComponent(axis));
  renderBanners();

  const budget = data.budget || {};
  const burn = budget.burn_rate || 0;
  const selector = el('select', {
    onchange: (event) => { state.usageAxis = event.target.value; refresh(); },
  }, ['service_id', 'end_user_hash', 'role', 'model', 'node'].map((name) =>
    el('option', { value: name, selected: name === axis, text: name })));

  return [
    el('div', { class: 'grid' }, [
      stat(t('ui.calls'), data.rows.reduce((sum, r) => sum + r.calls, 0)),
      stat(t('ui.cost'), money(data.spend_usd)),
      card(t('ui.budget_used'), [
        el('div', { class: 'stat' + (burn >= 1 ? ' bad' : (burn >= (budget.warn_at || 0.8) ? ' warn' : '')),
          text: budget.limit ? (burn * 100).toFixed(1) + '%' : '—' }),
        budget.limit ? bar(burn, budget.warn_at) : null,
        budget.limit ? el('p', { class: 'hint', text: money(budget.committed) + ' / ' + money(budget.limit) }) : null,
      ]),
    ]),
    card(t('ui.usage'), [
      el('div', { class: 'row' }, [el('label', { text: t('ui.role') }), selector]),
      table(
        ['', { label: t('ui.calls'), num: true }, { label: t('ui.tokens_used'), num: true },
         { label: t('ui.cost'), num: true }, { label: t('ui.latency'), num: true },
         { label: t('ui.success_rate'), num: true }],
        data.rows.map((r) => [
          r.key || '—', r.calls, r.input_tokens + r.output_tokens,
          money(r.cost_usd), r.avg_duration_ms + ' ms',
          (r.success_rate * 100).toFixed(1) + '%',
        ])),
    ]),
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
    card(t('ui.locale_packs_on'), [
      el('p', {}, [rules.locale_pack ? badge(rules.locale_pack, 'internal')
        : badge(t('ui.none'), 'block')]),
      el('p', { class: 'hint', text: t('ui.locale_pack_warning') }),
    ], rules.locale_pack ? null : 'bad'),

    // **오탐 검토 큐** — 이게 밀리면 승격 게이트가 영원히 안 열린다.
    card(t('ui.false_positive_queue'), [
      el('p', { class: 'hint', text: t('ui.review_help') }),
      table([t('ui.rule'), t('ui.stage'), t('ui.action'), t('ui.hits'), '', ''],
        events.map((e) => [
          e.rule_id, e.stage, badge(e.action, e.action), e.match_count,
          when(e.ts),
          el('div', { class: 'row' }, [
            el('button', { text: t('ui.true_positive'), onclick: () => review(e.id, 'true_positive') }),
            el('button', { text: t('ui.false_positive'), onclick: () => review(e.id, 'false_positive') }),
          ]),
        ])),
    ], events.length ? 'warn' : null),

    card(t('ui.rule'), [
      table([t('ui.rule'), t('ui.boundary_internal'), t('ui.boundary_external'), 'pack', ''],
        (rules.effective || []).map((r) => [
          r.id,
          badge(r.action.internal, r.action.internal),
          badge(r.action.external, r.action.external),
          r.locale_pack,
          el('button', { text: t('ui.promote'), onclick: () => checkPromotion(r.id) }),
        ])),
      el('p', { class: 'hint', text: t('ui.masked_only') }),
    ]),
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
    alert((verdict.allowed ? t('ui.promotion_ready') : t('ui.promotion_blocked'))
      + '\n' + verdict.reason
      + '\n' + (verdict.false_positive_rate * 100).toFixed(1) + '% / '
      + (verdict.limit * 100).toFixed(1) + '%'
      + ' (n=' + verdict.reviewed + ')');
  } catch (err) { showError(err); }
}

// ── 테넌트: 작업 ──────────────────────────────────────────────────────────

async function renderJobs() {
  const data = await api('/v1/admin/jobs?limit=100');
  renderBanners();

  return [
    card(t('ui.jobs'), [
      // **화면은 마스킹본만 본다.** 원문은 단건 API + 감사다.
      el('p', { class: 'hint', text: t('ui.masked_only') + ' ' + t('ui.raw_audited') }),
      table(
        [t('ui.status'), t('ui.role'), t('ui.node'), t('ui.model'), '', { label: t('ui.cost'), num: true }, ''],
        (data.jobs || []).map((j) => [
          badge(j.status, j.status === 'ok' ? 'healthy' : (j.status === 'failed' ? 'unhealthy' : '')),
          j.role, j.node || '—',
          // **"왜 이 모델로 갔는가" 는 모델 옆에서 물어보게 된다.** 열을 따로 두면
          // 라우팅을 안 켠 설치처에서 언제나 비어 있는 열이 하나 는다.
          el('span', {}, [
            j.model || '—',
            // 밑줄 시작은 판정 센티널(_failed/_none)이다 — 화면에는 실제 라우트만.
            // 어느 쪽이든 기본 모델로 갔고, 실패율은 메트릭이 답한다.
            j.route && !j.route.startsWith('_')
              ? el('span', { class: 'hint', text: ' ← ' + j.route }) : null,
          ]),
          el('span', { class: 'mono', text: (j.prompt_masked || '').slice(0, 60) }),
          money(j.cost_usd),
          j.has_raw
            ? el('button', { text: t('ui.view_raw'), onclick: () => viewRaw(j.id) })
            : '—',
        ])),
    ]),
  ];
}

async function viewRaw(jobId) {
  if (!confirm(t('ui.raw_audited'))) return;
  try {
    const body = await api('/v1/admin/jobs/' + encodeURIComponent(jobId) + '/raw');
    $('view').prepend(card(t('ui.view_raw'), [
      el('pre', { class: 'reveal', text: body.prompt }),
      el('p', { class: 'hint', text: t('ui.raw_audited') }),
      el('button', { text: t('ui.close'), onclick: () => refresh() }),
    ], 'warn'));
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
            refresh();
          } catch (err) { showError(err); }
        },
      }, [
        el('div', {}, [el('label', { for: 'retention', text: t('ui.retention') }), retention]),
        el('div', {}, [el('label', { for: 'data-locale', text: 'locale' }), locale]),
        el('button', { class: 'primary', type: 'submit', text: t('ui.save') }),
      ]),
      // **조용히 자르지 않는다** — 30일로 설정했다고 믿는 채로 7일 뒤 사라지면 안 된다.
      capped ? el('p', { class: 'hint', text: t('ui.retention_capped', { days: settings.raw_prompt_retention_days }) }) : null,
      settings.raw_prompt_storage ? null : el('p', { class: 'error', text: t('ui.raw_storage_off') }),
    ], capped ? 'warn' : null),

    card(t('ui.export'), [
      el('p', { class: 'hint', text: t('ui.masked_only') }),
      el('button', { text: t('ui.download'), onclick: () => downloadJson('/v1/admin/export', 'export.json') }),
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

  return [
    el('div', { class: 'grid' }, [
      stat(t('ui.nodes'), (s.nodes.healthy || 0) + ' / ' + (s.nodes.total || 0)),
      stat(t('ui.queued'),
        Object.values(s.lanes).reduce((sum, l) => sum + l.queued, 0)),
    ]),
    card(t('ui.role'), table(
      [t('ui.role'), '', { label: t('ui.latency'), num: true }, { label: '', num: true }],
      ((data.roles && data.roles.roles) || []).map((r) =>
        [r.name, r.kind, r.timeout_seconds + 's', r.max_prompt_chars]))),
  ];
}

// ── 접속 ──────────────────────────────────────────────────────────────────

async function loadSession() {
  state.session = await api('/v1/session');
  state.strings = state.session.strings || {};
  document.documentElement.lang = state.session.locale;
  applyStaticStrings();

  $('who').textContent = state.session.tenant.name + ' · ' + state.session.role;
  $('version').textContent = 'v' + state.session.version;
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
  state.token = null;
  state.session = null;
  try { sessionStorage.removeItem(TOKEN_KEY); } catch (_) { /* 무시 */ }
  $('shell').hidden = true;
  $('login').hidden = false;
}

async function boot() {
  applyStaticStrings();
  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const box = $('login-error');
    box.hidden = true;
    try {
      await connect($('token').value.trim());
    } catch (err) {
      box.textContent = err.message || String(err);
      box.hidden = false;
    }
  });
  $('refresh').addEventListener('click', () => refresh());
  $('logout').addEventListener('click', () => disconnect());

  let saved = null;
  try { saved = sessionStorage.getItem(TOKEN_KEY); } catch (_) { saved = null; }
  if (saved) {
    try { await connect(saved); return; } catch (_) { /* 만료·폐기된 토큰 */ }
  }
  $('login').hidden = false;
}

document.addEventListener('DOMContentLoaded', boot);
