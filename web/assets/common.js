/* 推免雷达 · 前端公共层（智能体 C）
 *
 * 两种数据源：
 *   live     —— B 的真接口（同源 /api/*；server/app.py 把 web/ 挂载在一起，所以没有跨域）
 *   fixtures —— contracts/fixtures/ 里的样例（离线演示、回归对照用）
 *
 * 默认 live。?api=fixtures 强制切回样例；?user=xxx 切身份（演示付费墙要两个身份）。
 *
 * 规矩来自 contracts/api.md 第 10 节：接口没通就落回样例，**别为了等接口把能用的页面停了**。
 * 所以回退是允许的 —— 但页面上必须**看得见当前用的是哪种数据源**，不能悄悄回退：
 * 一个"看起来正常、其实在读样例"的页面，比一个白屏的页面更坏。
 */

/* ---------- 数据源模式 ---------- */

const API_MODE = (function () {
  try {
    const q = new URLSearchParams(location.search).get('api');
    return (q === 'fixtures' || q === 'live') ? q : 'live';
  } catch (e) {
    return 'live';
  }
})();

const useLive = () => API_MODE === 'live';

/* live 失败要不要回退。默认回退（演示友好），但回退一定会在页面上标出来。 */
const ALLOW_FALLBACK = true;

let dataSource = null;      // 'live' | 'fixtures'
let lastLiveError = null;

/* ---------- 身份（假登录态） ---------- */

const DEFAULT_USER = 'u_demo';        // 免费身份
const SUB_USER = 'u_demo_sub';        // 订阅身份（演示付费墙用）

/** 公网演示版：服务端在响应里种 `tuimian_public=1`。
 *  这时**演示身份是只读的**（服务端也会拒绝写入），访客拿到的是 `guest_xxx`，
 *  各改各的，不会把评委看到的演示状态改坏。 */
function isPublicMode() {
  try { return /(?:^|;\s*)tuimian_public=1(?:;|$)/.test(document.cookie); }
  catch (e) { return false; }
}

function _guestId() {
  const rnd = Math.random().toString(36).slice(2, 10);
  return 'guest_' + rnd + Date.now().toString(36).slice(-4);
}

function currentUser() {
  try {
    const q = new URLSearchParams(location.search).get('user');
    if (q) return q;
    const saved = localStorage.getItem('tuimian_user');
    if (saved) return saved;
    if (isPublicMode()) {
      // 第一次访问：分配一个只属于这台浏览器的访客身份
      const id = _guestId();
      try { localStorage.setItem('tuimian_user', id); } catch (e) { /* 无 localStorage 就每次新身份 */ }
      return id;
    }
    return DEFAULT_USER;
  } catch (e) {
    return DEFAULT_USER;
  }
}

function setCurrentUser(id) {
  try { localStorage.setItem('tuimian_user', id); } catch (e) { /* 无 localStorage 就算了 */ }
}

/** 所有要带用户态的接口都走这里，免得某个页面漏传 user 导致额度算到别人头上 */
function userQuery(extra) {
  return Object.assign({ user: currentUser() }, extra || {});
}

/* ---------- 本机的「已读 / 静音」记录（C13 通知中心 / C12 订阅页）
 *
 * 这两样**都不是服务端状态**：`/api/watch` 没有 enabled 字段，也没有"已读"接口
 * （这些是 A/B 的地盘，我不改 server/）。所以它们只在这台浏览器上有效 ——
 * 页面里必须把这句话说出来，不能让人以为换台机器还一样。
 *
 * 键都带 user：换个演示身份不该继承另一个身份"已读"的东西，
 * 否则切身份演示时红点会莫名其妙不亮。
 */
function localKey(name) {
  return 'tuimian_' + name + '_' + currentUser();
}

function localList(name) {
  try {
    const v = JSON.parse(localStorage.getItem(localKey(name)) || '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
  } catch (e) {
    return [];
  }
}

/** 只留最近 300 条：已读集合会随时间无限长，而 localStorage 是有配额的。 */
function localSet(name, arr) {
  try {
    localStorage.setItem(localKey(name), JSON.stringify((arr || []).slice(-300)));
  } catch (e) { /* 无 localStorage 就算了：红点会重新亮，不影响用 */ }
}

/* ---------- 真接口 ---------- */

function qs(params) {
  const p = new URLSearchParams();
  Object.keys(params || {}).forEach((k) => {
    const v = params[k];
    if (v !== undefined && v !== null && v !== '') p.set(k, v);
  });
  const s = p.toString();
  return s ? '?' + s : '';
}

function httpError(r, data) {
  const msg = (data && data.error && data.error.message) || ('HTTP ' + r.status);
  const err = new Error(msg);
  err.status = r.status;
  err.payload = data;                    // 402 时 body 里有 upgrade_hint
  err.headers = r.headers || null;
  return err;
}

/* ---------- 超时（**每一页的出路**） ----------
 *
 * 没有超时的 fetch 在演示现场是致命的：服务在跑但某个接口卡住时，
 * 页面会永远停在「正在读取数据…」—— 既没有报错，也没有重试，用户只能干等。
 * 2026-10-17 的四态走查里，这一条 **9 个页面全中**。
 *
 * 所以每个请求都有上限，超时按"接口没通"处理（可以回退），而不是无限等。 */
const FETCH_TIMEOUT_MS = 8000;      // 接口
const FIXTURE_TIMEOUT_MS = 5000;    // 本地样例：读不到就是读不到，不值得等这么久

function timeoutError(ms, path) {
  const err = new Error('超过 ' + Math.round(ms / 1000) + ' 秒没有响应');
  err.timeout = true;
  err.path = path || '';
  return err;
}

async function fetchWithTimeout(url, opts, ms, path) {
  const ctl = (typeof AbortController === 'function') ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), ms) : null;
  try {
    return await fetch(url, Object.assign({ cache: 'no-store' }, opts || {}, ctl ? { signal: ctl.signal } : {}));
  } catch (e) {
    if (ctl && ctl.signal.aborted) throw timeoutError(ms, path);
    const err = new Error('连不上（' + (e && e.message ? e.message : e) + '）');
    err.network = true;
    err.path = path || '';
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function apiGet(path, params, timeoutMs) {
  const r = await fetchWithTimeout(path + qs(params), {}, timeoutMs || FETCH_TIMEOUT_MS, path);
  const data = await r.json().catch(() => null);
  if (!r.ok) { const e = httpError(r, data); e.path = path; throw e; }
  return data;
}

async function apiPost(path, params, body) {
  const r = await fetchWithTimeout(path + qs(params), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  }, FETCH_TIMEOUT_MS, path);
  const data = await r.json().catch(() => null);
  if (!r.ok) { const e = httpError(r, data); e.path = path; throw e; }
  return data;
}

/** 4xx 是**接口给的答复**（404 = 这条不存在，402 = 额度用完），
 *  不是"接口没通"。这两种情况下拿样例顶上去，就是**用样例冒充真数据**。 */
function isClientError(err) {
  return !!(err && typeof err.status === 'number' && err.status >= 400 && err.status < 500);
}

/* ---------- 样例文件 ---------- */

const API_BASES = ['/contracts/fixtures', '../contracts/fixtures'];
let API_BASE = null;

async function loadFixture(name) {
  const ordered = API_BASE ? [API_BASE].concat(API_BASES.filter((b) => b !== API_BASE)) : API_BASES;
  const errs = [];
  for (const base of ordered) {
    const url = base + '/' + name;
    try {
      const r = await fetchWithTimeout(url, {}, FIXTURE_TIMEOUT_MS, url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      API_BASE = base;
      return data;
    } catch (e) {
      errs.push(url + ' → ' + e.message);
    }
  }
  const err = new Error(errs.join('\n'));
  err.isDataError = true;
  err.attempts = errs;
  throw err;
}

/* 今日更新的数据不是接口产物，是 A 的抓取产出 data/out/changes.{json,md}。
 * （GET /api/changes 还不存在，见 api.md 第 10 节的"C 的过渡做法"。） */
const CHANGES_BASES = ['/data/out', '../data/out'];
let CHANGES_BASE = null;

async function loadChangesFile(name, asText) {
  const ordered = CHANGES_BASE
    ? [CHANGES_BASE].concat(CHANGES_BASES.filter((b) => b !== CHANGES_BASE))
    : CHANGES_BASES;
  const errs = [];
  for (const base of ordered) {
    const url = base + '/' + name;
    try {
      const r = await fetchWithTimeout(url, {}, FIXTURE_TIMEOUT_MS, url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = asText ? await r.text() : await r.json();
      CHANGES_BASE = base;
      return data;
    } catch (e) {
      errs.push(url + ' → ' + e.message);
    }
  }
  const err = new Error(errs.join('\n'));
  err.isDataError = true;
  err.attempts = errs;
  throw err;
}

/** 今日更新：接口优先，没通就用文件（并且页面上会标出来）。 */
async function loadChangesData() {
  let liveErr = null;
  if (useLive()) {
    try {
      const data = await apiGet('/api/changes');
      dataSource = 'live';
      return { data: data, md: null };
    } catch (err) {
      lastLiveError = err;
      liveErr = err;
      if (isClientError(err) || !ALLOW_FALLBACK) throw err;
    }
  }
  try {
    const [data, md] = await Promise.all([
      loadChangesFile('changes.json', false),
      loadChangesFile('changes.md', true).catch(() => null)
    ]);
    dataSource = 'fixtures';
    return { data: data, md: md };
  } catch (fxErr) {
    fxErr.liveError = (liveErr && liveErr !== fxErr) ? liveErr : null;
    throw fxErr;
  }
}

/* ---------- 数据新鲜度（「数据是什么时候的」） ----------
 *
 * 产品最核心的承诺是「每天更新」。在这一层之前，这句话在界面上**不可验证** ——
 * 评委问一句「这数据什么时候抓的」，我们只能去翻文件系统。
 * 接口 GET /api/freshness 是新加的（契约第 12 节），文件回退是 data/out/freshness.json。
 *
 * 三条口径（契约第 12 节「stale 时前端该怎么措辞」）：
 *   1. status = failed  → 「本次抓取失败」+ 原因；**不能说成「没有更新」**
 *   2. status = empty   → 「抓到了页面但没解析出内容，需人工确认」；**不是失败、也不是没变化**
 *   3. status = stale   → 「数据停留在 X（N 小时前）」
 * 「没抓到」和「没有变化」必须长得不一样 —— 显示成同一句话，用户会安心地以为情况已知。
 */
const FRESHNESS_FILE = 'freshness.json';
const AGE_BASES = ['/data/out', '../data/out'];   // 和 changes 同一个目录，共用同一套相对路径基
let AGE_BASE = null;

async function loadAgeFile(name) {
  const ordered = AGE_BASE ? [AGE_BASE].concat(AGE_BASES.filter((b) => b !== AGE_BASE)) : AGE_BASES;
  const errs = [];
  for (const base of ordered) {
    const url = base + '/' + name;
    try {
      const r = await fetchWithTimeout(url, {}, FIXTURE_TIMEOUT_MS, url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      AGE_BASE = base;
      return data;
    } catch (e) {
      errs.push(url + ' → ' + e.message);
    }
  }
  const err = new Error(errs.join('\n'));
  err.isDataError = true;
  err.attempts = errs;
  throw err;
}

/** 接口喜欢用中文字面量，文件版是 A 的字段名（freshness_hours 与 age_hours 不是一个东西）。
 *  把两种形状归一，页面只认一套。 */
function normFreshness(raw, fromApi) {
  if (!raw || typeof raw !== 'object') return null;
  const d = raw;
  const out = {
    from: fromApi ? 'api' : 'file',
    generated_at: d.generated_at || null,
    now: d.now || null,
    generated_age_hours: numOrNull(d.generated_age_hours),
    generated_age_text: d.generated_age_text || null,
    oldest_hours: numOrNull(d.oldest_hours),
    stale_after_hours: numOrNull(d.stale_after_hours) || 30,
    worst: d.worst || 'unknown',
    totals: d.totals || {},
    schools: Array.isArray(d.schools) ? d.schools : [],
    error: d._error || null,
    message: d.message || null,
    note: d._note || null
  };
  // 文件版没有 age_hours / status：按同一套规则现算，免得两个数据源长出两种判断
  out.schools = out.schools.map((s) => {
    const age = numOrNull(s.age_hours);
    const failed = Array.isArray(s.failed) ? s.failed : [];
    const boards = numOrNull(s.boards) || 0;
    const boardsEmpty = numOrNull(s.boards_empty) || 0;
    const records = numOrNull(s.records) || 0;
    const empty = (typeof s.empty === 'boolean') ? s.empty
      : (failed.length === 0 && boards > 0 && boardsEmpty === boards && records === 0);
    let status = s.status;
    if (!status) {
      if (failed.length) status = 'failed';
      else if (empty) status = 'empty';
      else if (age !== null && age > out.stale_after_hours) status = 'stale';
      else status = 'ok';
    }
    return {
      school: s.school || '', unit: s.unit || '', homepage: s.homepage || null,
      last_ok: s.last_ok || null, age_hours: age,
      freshness: s.freshness || (age !== null && age > out.stale_after_hours ? 'stale' : 'fresh'),
      records: records, boards: boards, boards_empty: boardsEmpty,
      failed: failed.map((f) => String(f)), has_failure: !!s.has_failure || failed.length > 0,
      empty: !!empty, status: status
    };
  });
  // ⚠️ `totals.schools` 是**站点数**（`(school, unit)` 的条目数），**不是学校数**。
  // 实测 2026-10-18：freshness 给了 37 个条目、distinct `school` 只有 29 个。
  // 之前页头直接拿 37 说「覆盖 37 所学校」—— 学校数和站点数混成了同一个数，
  // 而 docs/DATA-NUMBERS.md §一 点名不许说这一句。两个数都现算，别信 totals。
  out.site_count = out.schools.length || numOrNull(out.totals && out.totals.schools);
  out.school_count = distinctSchoolCount(out.schools);

  if (!out.worst || out.worst === 'unknown') {
    out.worst = worstOf(out.schools);
  }
  if (out.generated_age_text === null && out.generated_at) {
    out.generated_age_text = relTimeText(out.generated_at, out.now);
  }
  return out;
}

function numOrNull(v) {
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

/** `school` 去重计数。**别用 `schools.length`** —— 那是站点数（一所学校常有多个栏目）。 */
function distinctSchoolCount(schools) {
  const seen = Object.create(null);
  let n = 0;
  (schools || []).forEach((s) => {
    const k = s && s.school;
    if (k && !seen[k]) { seen[k] = 1; n++; }
  });
  return n;
}

const AGE_RANK = { failed: 0, empty: 1, stale: 2, ok: 3, unknown: 4 };

function worstOf(schools) {
  if (!schools.length) return 'unknown';
  return schools.reduce((w, s) => (
    (AGE_RANK[s.status] === undefined ? 4 : AGE_RANK[s.status]) < (AGE_RANK[w] === undefined ? 4 : AGE_RANK[w])
      ? s.status : w
  ), 'ok');
}

/* ---------- 数据心跳（C6，2026-10-18）----------
 *
 * 第一屏要回答的第一个问题是「这数据多新」。原来只有一行 `freshness`（按学校讲抓取健康），
 * 但用户真正想知道的是**心跳**：多久前更新、最近在不在跑、今天多了几条。
 * 接口：`GET /api/health/data`（B6）。
 *
 * ⚠️ **「今天新增」这一格我没有写成"今天"**：接口给的是 `last_run.new_notices`（**最近一轮**新增），
 *    不是当天合计。`/api/changes/history` 只覆盖到 10-17，拿不到今天的合计。
 *    一天可能跑多轮，把"最近一轮"写成"今天新增"会**少算**，所以这里照实写「最近一轮新增 N 条」，
 *    并把原因放进悬停提示。要真要"今天新增"，得让 B 在接口里加一个当天累计字段。
 */
function hbAgeText(minutes) {
  const m = (typeof minutes === 'number' && isFinite(minutes)) ? Math.max(0, Math.round(minutes)) : null;
  if (m === null) return '未知';
  if (m < 1) return '刚刚';
  if (m < 60) return m + ' 分钟前';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' 小时前';
  return Math.floor(h / 24) + ' 天前';
}

/** 心跳的三种脸色。**未知不等于失败**（契约：`ok=null` 是"我们不知道"）。 */
function hbTone(h) {
  if (!h || typeof h !== 'object') return 'hb-unknown';
  const lastOk = h.last_run ? h.last_run.ok : null;
  if (lastOk === false || (h.runs_24h_failed || 0) > 0) return 'hb-bad';
  if (lastOk === null || (h.runs_24h_unknown || 0) > 0) return 'hb-warn';
  return 'hb-ok';
}

function hbTitle(h) {
  const lr = h.last_run || {};
  const bits = [];
  if (typeof lr.seconds === 'number') bits.push('最近一轮耗时 ' + Math.round(lr.seconds) + ' 秒（' + (lr.kind || '?') + '）');
  if (h.last_full_at) bits.push('最近一次全量：' + String(h.last_full_at).replace('T', ' ').slice(0, 16));
  if (typeof h.consecutive_ok === 'number') bits.push('连续成功 ' + h.consecutive_ok + ' 轮');
  if (typeof h.runs_24h === 'number') bits.push('24 小时 ' + h.runs_24h + ' 轮（失败 ' + (h.runs_24h_failed || 0) + ' / 未知 ' + (h.runs_24h_unknown || 0) + '）');
  bits.push('「最近一轮新增」是那一轮抓到的条数，不是当天合计（接口没有当天累计字段）');
  return bits.join('\n');
}

function heartbeatHTML(h) {
  if (!h || typeof h !== 'object') {
    return '<div class="hb hb-unknown"><span class="hb-dot"></span>数据心跳读不到（这个接口没有响应）</div>';
  }
  const tone = hbTone(h);
  const lr = h.last_run || {};
  // `data_age_minutes` 可能是 null（那一轮没留下可判定的痕迹时，B6 报 null 而不是编一个数）。
  // 这时退到 `/api/freshness` 的 `generated_age_text` —— 它量的是同一件事（数据文件什么时候生成），
  // 而且**是接口给的**，不是前端自己算。两边都没有才写"未知"。
  const age = (typeof h.data_age_minutes === 'number')
    ? hbAgeText(h.data_age_minutes)
    : (h.age_text_fallback || '未知');
  const parts = [];
  let lead;

  if (tone === 'hb-bad') {
    // 失败时**先说失败**，再说数据多旧 —— 顺序反了会让人觉得数据是新的。
    const failedAgo = lr.at ? hbAgeText((Date.now() - Date.parse(lr.at)) / 60000) : '未知时间';
    lead = '<strong>最近一轮失败于 ' + esc(failedAgo) + '，正在重试</strong>';
    if (typeof h.runs_24h_failed === 'number') parts.push('24 小时内有 ' + h.runs_24h_failed + ' 轮失败');
    parts.push('现在看到的数据是 ' + esc(age) + '的');
  } else {
    lead = '数据更新于 <strong>' + esc(age) + '</strong>';
    if (typeof h.runs_24h === 'number') parts.push('最近 24 小时自动跑了 ' + h.runs_24h + ' 轮');
    if (lr && typeof lr.new_notices === 'number') parts.push('最近一轮新增 ' + lr.new_notices + ' 条');
    // 未知**不是失败**（契约：ok=null = 我们不知道那一轮成功没有），但也不能不吭声 ——
    // 否则"连续成功 N 轮"和"有一轮不知道"会并排出现而没人解释。
    if (tone === 'hb-warn' || (h.runs_24h_unknown || 0) > 0) {
      parts.push('有一轮结果未知（没留成功标记，不是失败）');
    }
  }

  return '<div class="hb ' + tone + '" title="' + esc(hbTitle(h)) + '">' +
    '<span class="hb-dot" aria-hidden="true"></span>' + lead +
    (parts.length ? '<span class="hb-sep">·</span>' + parts.map((p) => '<span class="hb-item">' + p + '</span>').join('<span class="hb-sep">·</span>') : '') +
    '</div>';
}

/** 有宿主才请求，一个页面一次。失败就照实说，不挡住主内容。
 *
 *  **两件 2026-10-18 23:3x 修的事**（实测见 status/C.md §三）：
 *
 *  ① 没有占位 → 数据到了会把下面的内容顶一下（布局跳动）。
 *     现在先同步画一行"读取中"，高度与最终那一行一致，到货只换字不换高。
 *  ② 它和首屏列表抢带宽。`/api/health/data` 实测 347~363ms，
 *     而公告页首屏那批请求（notices/facets/freshness）全靠同一台服务。
 *     所以**不在 DOMContentLoaded 立刻发**，而是等 `load` 之后用空闲时间发：
 *     列表先出来，心跳后补上。它慢不慢都不再影响"第一屏有没有东西看"。
 */
const HB_PLACEHOLDER = '<div class="hb hb-pending"><span class="hb-dot"></span>数据心跳读取中…</div>';

function initHeartbeat() {
  const host = document.getElementById('heartbeat');
  if (!host) return;
  // 占位先上，避免到货时布局跳动
  host.innerHTML = HB_PLACEHOLDER;
  host.hidden = false;

  const fetchAndFill = () => {
    apiGet('/api/health/data')
      .then((h) => {
        if (!h || typeof h !== 'object') throw new Error('empty');
        // 心跳缺"数据多旧"时，用 freshness 那份补（同一件事，接口给的，不是前端算的）
        if (typeof h.data_age_minutes !== 'number') {
          return apiGet('/api/freshness')
            .then((f) => {
              if (f && f.generated_age_text) h.age_text_fallback = f.generated_age_text;
            })
            .catch(() => {})
            .then(() => h);
        }
        return h;
      })
      .then((h) => { host.innerHTML = heartbeatHTML(h); })
      .catch(() => {
        host.innerHTML = '<div class="hb hb-unknown"><span class="hb-dot"></span>' +
          '数据心跳读不到 —— 不影响下面的内容，但「数据多新」这件事这次没法确认</div>';
      });
  };

  // load 之后再用空闲时间发；`requestIdleCallback` 不可用时退到 setTimeout(60)
  const idle = (fn) => {
    if (typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(fn, { timeout: 1200 });
    } else {
      setTimeout(fn, 60);
    }
  };

  // **第三件（2026-10-19 00:0x）：等第一屏内容落地再发心跳。**
  //
  // 只靠"load + idle"不够。Fast 3G 实测（1.6Mbps / 562.5ms RTT）：
  //   load 2091ms → 心跳 2681ms → 第一张卡 2848ms
  // 心跳比内容**早 167ms**：因为两个请求并行，health 只需 575ms、push/preview 要 589ms，
  // 而且列表那 33KB 还要解析+建 20 张卡。用户先看到的是"数据更新于…"，列表还在转。
  //
  // 所以改成：先等列表出现第一块内容（卡片或空态都算），再发心跳。
  // 没有 `#results` 的页面（今日更新/论坛）不适用这条，直接发 —— 那种页面上心跳本身就是主内容之一。
  const waitForContent = (cb) => {
    const host2 = document.getElementById('results');
    if (!host2) return cb();                       // 没有列表宿主：立刻发
    const hasContent = () => !!host2.querySelector('.ncard, .push-card, .card, .state, .empty');
    if (hasContent()) return cb();
    let ticks = 0;
    const timer = setInterval(() => {
      ticks += 1;
      if (hasContent() || ticks >= 40) {           // 最多等 4s，等不到也要发，不能把心跳弄丢
        clearInterval(timer);
        cb();
      }
    }, 100);
  };

  const go = () => waitForContent(() => idle(fetchAndFill));
  if (document.readyState === 'complete') {
    go();
  } else {
    window.addEventListener('load', go, { once: true });
  }
}

/* ---------- 时效分层（C1，2026-10-18） ----------
 *
 * 为什么要做：1119 条公告里 **944 条超过 30 天**（最老 2013-09-27），286 条已过期。
 * 用户打开第一屏如果看到的是"2013 年的旧文章"或者"博士后奖"，
 * 这个产品「告诉你今天该看什么」的承诺就当场破产 —— 内容再多也白搭。
 *
 * 四档（口径与 B 的 `GET /api/updates`、`GET /api/library` 对齐，逐档对账见 docs/DATA-NUMBERS.md）：
 *   最近 3 天  age_days ≤ 3
 *   本周            ≤ 7
 *   本月            ≤ 30      ← **默认档**（首屏不许出现 30 天以上的记录）
 *   资料库          > 30      ← 必须写"历史参考，非最新"
 *
 * ⚠️ `age_days` 一律从 **`publish_date`** 起算，**不用 `crawled_at`**：
 *    crawled_at 是"我们什么时候抓到的"，拿它算会把 2013 年的文章算成"今天"。
 *    这是这一层最容易出的错，也是 B 那两个接口必须和这里同口径的原因。
 *
 * ⚠️ 日期算得对不对比"看起来差不多"重要：`publish_date` 是 `YYYY-MM-DD`，
 *    用 `Date.parse('2026-10-16')` 会被当成 **UTC 午夜**，在东八区比本地午夜早 8 小时，
 *    跨日时会把"今天发的"算成"1 天前"。所以这里手工拆年月日、两边都按 UTC 拼，
 *    和 deadline.js 那条"别用 new Date(v) 比日期"是同一个坑。
 */
const TIERS = [
  { key: 'd3',    label: '最近 3 天', max: 3 },
  { key: 'week', label: '本周', max: 7 },
  { key: 'month', label: '本月', max: 30 },
  { key: 'library', label: '资料库', min: 30, note: '历史参考，非最新' },
  { key: 'nodate', label: '日期未知', note: '没抓到发布日，不放进任何时效档' }
];
const DEFAULT_TIER = 'month';
const ARCHIVE_DAYS = 180;   // 超过这个天数：卡片默认折叠，带灰色「历史」标记

/** 发布日距今多少天；拿不到日期返回 null（**不许当成 0**）。
 *
 * **优先用接口给的 `age_days`**（A3 的 `collector/timeliness.py` 已经算好了），
 * 拿不到才自己按 `publish_date` 算。
 * 理由是这个项目反复踩的那条：能让接口给的，就别让前端自己算 ——
 * 前端一份、接口一份，两边对不上时没人知道该信哪个。
 *
 * 实测（2026-10-18 10:55）：A 的 `age_days` 与这里按 `publish_date` 推算的
 * **1111 条全部一致**（另 9 条没日期）。所以现在两边等价；
 * 但 `/api/notices`、`/api/match`、`/api/push/preview` 目前都**还没透传 `age_days`**，
 * 一旦透出来就以后端那份为准 —— 口径只留一处。
 */
function ageDaysOf(rec) {
  if (rec && typeof rec.age_days === 'number' && isFinite(rec.age_days)) {
    return Math.max(0, Math.round(rec.age_days));
  }
  const m = String((rec && (rec.publish_date || rec.published_at)) || '').slice(0, 10);
  const p = /^(\d{4})-(\d{2})-(\d{2})$/.exec(m);
  if (!p) return null;
  const pub = Date.UTC(Number(p[1]), Number(p[2]) - 1, Number(p[3]));
  const n = new Date();
  const today = Date.UTC(n.getFullYear(), n.getMonth(), n.getDate());
  const days = Math.round((today - pub) / 86400000);
  return isFinite(days) ? Math.max(0, days) : null;
}

/** 距今多久，人话。`null` 不编成"今天"。 */
function ageText(days) {
  if (days === null || days === undefined) return '日期未知';
  if (!isFinite(days)) return '日期未知';
  if (days < 0) return '未来';                 // 发布日比今天晚：数据有问题，照实说
  if (days === 0) return '今天';
  if (days === 1) return '昨天';
  if (days < 30) return days + ' 天前';
  if (days < 365) return Math.max(1, Math.round(days / 30)) + ' 个月前';
  const y = Math.floor(days / 365);
  return (y <= 1 ? '一年前' : y + ' 年前');
}

/** 天 → 档位 key。**只用于标注单条的归属，别拿它来做筛选**（四档是包含关系，见 inTier）。 */
function tierKeyOf(days) {
  if (days === null || days === undefined || !isFinite(days)) return 'nodate';
  if (days <= 3) return 'd3';
  if (days <= 7) return 'week';
  if (days <= 30) return 'month';
  return 'library';
}

/** 某一天数是否落在某一档。
 *
 * ⛔ **四档是包含关系，不是四个互斥的格子**：
 *    最近 3 天 ≤3 ⊂ 本周 ≤7 ⊂ 本月 ≤30，资料库 >30。
 *    我第一版写成互斥区间（本月 = 8~30 天），实测立刻露馅：
 *    推送页点「本月」只剩 17 条，而这 20 条里有 17 条本来就 ≤30 天 ——
 *    **点"本月"却看不到"本周"的东西，用户只会认为少了**。
 *    派单里写的也是 `≤7` / `≤30`，不是区间。
 */
function inTier(days, key) {
  const d = (days === undefined) ? null : days;
  const has = (d !== null && isFinite(d));
  if (key === 'nodate') return !has;
  if (!has) return false;                 // 日期未知的不进任何时效档（它自己一档）
  if (key === 'd3') return d <= 3;
  if (key === 'week') return d <= 7;
  if (key === 'month') return d <= 30;
  if (key === 'library') return d > 30;
  return true;
}

/** 全部档位的计数（**在完整集合上算**，不是当前那一页）。
 *  `today ⊆ week ⊆ month`，`month + library + nodate = 总数`。 */
function tierCounts(rows) {
  const c = { d3: 0, week: 0, month: 0, library: 0, nodate: 0 };
  (rows || []).forEach((r) => {
    const d = ageDaysOf(r);
    if (d === null || !isFinite(d)) { c.nodate += 1; return; }
    if (d <= 3) c.d3 += 1;
    if (d <= 7) c.week += 1;
    if (d <= 30) c.month += 1; else c.library += 1;
  });
  return c;
}

/** 四档切换条。`counts` 必须来自完整集合，否则数字是骗人的。 */
function tierChipsHTML(counts, active) {
  const c = counts || {};
  const chips = TIERS.filter((t) => t.key !== 'nodate' || c.nodate > 0).map((t) => {
    const n = c[t.key] || 0;
    const title = t.note ? ' title="' + esc(t.note) + '"' : '';
    return '<button type="button" class="fchip" data-tier="' + esc(t.key) + '" aria-pressed="' +
      (t.key === active) + '"' + title + '>' + esc(t.label) +
      '<span class="c">' + n + '</span></button>';
  });
  return '<div class="tier-row" role="group" aria-label="按发布时间筛选">' +
    '<span class="tier-label">时效</span>' + chips.join('') +
    (active === 'library' ? '<span class="tier-note">资料库是<strong>历史参考</strong>，不是最新公告</span>' : '') +
    '</div>';
}

/** 卡片上的「距今多久」。`>=ARCHIVE_DAYS` 额外带灰色「历史」。 */
function ageBadgeHTML(rec, days) {
  const d = (days === undefined) ? ageDaysOf(rec) : days;
  const txt = ageText(d);
  const old = (d !== null && d >= ARCHIVE_DAYS);
  return '<span class="age-badge' + (old ? ' age-old' : '') + '" title="按发布日算：' +
    esc(txt) + '">' + esc(txt) + (old ? '<span class="age-history">历史</span>' : '') + '</span>';
}

/** 超过 ARCHIVE_DAYS 的卡：默认折叠，展开才看详情（灰色「历史」标记跟着 summary 走）。 */
function archiveBlockHTML(innerHTML, days, summaryText) {
  if (days === null || days >= ARCHIVE_DAYS) {
    return '<details class="arch-more"><summary>' + esc(summaryText) + '</summary>' + innerHTML + '</details>';
  }
  return innerHTML;
}

/** 相对时间只在**文件回退**时才需要算（接口已经给了 generated_age_text）。
 *  契约第 12 节说得很清楚：能让接口给的，就不要前端自己算 —— 三个页面会算出三种写法。 */
function relTimeText(iso, nowIso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return null;
  const now = nowIso ? Date.parse(nowIso) : Date.now();
  const mins = Math.max(0, Math.round((now - t) / 60000));
  if (mins < 1) return '刚刚';
  if (mins < 60) return mins + ' 分钟前';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + ' 小时前';
  return Math.floor(hours / 24) + ' 天前';
}

/** 数据停留在哪一天（stale 时的措辞要「停留」而不是「过期」——
 *  「过期」像在说数据坏了，「停留在 10 月 16 日 08:20」说的是事实）。 */
function stampText(iso) {
  if (!iso) return '未知时间';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const p = (n) => (n < 10 ? '0' + n : String(n));
  return (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日 ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function ageHoursText(h) {
  if (h === null || h === undefined) return '';
  if (h < 1) return Math.round(h * 60) + ' 分钟前';
  if (h < 24) return Math.round(h) + ' 小时前';
  return Math.round(h / 24) + ' 天前';
}

/** 接口优先、文件回退。**读不到就返回 null，不抛** ——
 *  新鲜度是页面上的附加信息，它读不到不该把公告列表整页打掉。
 *
 *  但它还有第二个后果：**它不能拖住页面**。第一版直接吃 8 秒的接口超时，
 *  实测「接口 500」那一态下新鲜度块要 13 秒（8 秒接口 + 5 秒文件）才出现 ——
 *  附加信息不该比主内容慢这么多，而且慢到那个程度，量具会以为"没渲染"（我第一次就误判了）。
 *  所以它单独一路超时（6 秒），文件回退照旧。 */
const FRESHNESS_API_TIMEOUT_MS = 6000;

/** 每页只取一次。
 *  页面上有好几处要用新鲜度（顶部那一行、每张卡片的学校标记、今日更新页那一节），
 *  各取各的会发三次请求 —— 更糟的是**三个结果可能不一样**（数据在跑的时候会变），
 *  于是同一个页面上两个数字互相打脸。缓存一个 Promise，谁要都拿同一份。 */
let freshnessPromise = null;

function getFreshness() {
  if (!freshnessPromise) freshnessPromise = loadFreshness();
  return freshnessPromise;
}

/** 强制重取（演示时点了「立即更新」之类的地方可以用；现在还没有这个按钮）。 */
function resetFreshness() {
  freshnessPromise = null;
}

async function loadFreshness() {
  // **接口先试，不通才读文件 —— 不并行。**
  // 第一版是并行的（为了接口卡住时也能快点拿到文件），代价是**每次正常打开页面都先发两个
  // 注定 404 的请求**（真服务不挂 `data/out/`），控制台里每页两条红的。
  // 而并行省下的那点时间只在"接口超时"这一种情况下才有用 —— 那时页面本来就在显示超时错误。
  // 所以按顺序来：接口 200 就结束；接口坏了（500/404）立刻读文件，不额外等；
  // 只有真的卡住时才等满 6 秒，而那一态本来就是"告诉用户超时"。
  if (useLive()) {
    try {
      const fromApi = normFreshness(await apiGet('/api/freshness', null, FRESHNESS_API_TIMEOUT_MS), true);
      // 接口给了完整的学校清单 → 它就是事实来源（age_hours / status 都是它现算的）
      if (fromApi && fromApi.schools.length) return fromApi;
      // 接口说"还没有数据"：**照样信它**，不去文件里捞一份旧的来冒充"有数据"
      if (fromApi && fromApi.worst === 'unknown') return fromApi;
    } catch (err) {
      lastLiveError = err;
      // 4xx 是接口的答复（比如 404），文件兜底也不会长出新事实 —— 那就不显示
      if (isClientError(err)) return null;
    }
  }
  try { return normFreshness(await loadAgeFile(FRESHNESS_FILE), false); } catch (e) { return null; }
}

/** 按学校名找某一所的记录。学校的命名两边可能带「大学」后缀差异，所以做包含匹配兜底。 */
function schoolStatus(fresh, school) {
  if (!fresh || !school) return null;
  const s = String(school).trim();
  let hit = fresh.schools.find((x) => x.school === s);
  if (!hit) hit = fresh.schools.find((x) => x.school.indexOf(s) >= 0 || s.indexOf(x.school) >= 0);
  return hit || null;
}

/* 三种异常各有各的话，一个字都不能混：
 * 「本次抓取失败」= 我们没拿到；「没解析出内容」= 拿到了但不认识；两者都不是「没有更新」。 */
const FRESH_LABEL = {
  failed: { cls: 'fr-failed', text: '本次抓取失败' },
  empty: { cls: 'fr-empty', text: '本次没解析出内容' },
  stale: { cls: 'fr-stale', text: '数据陈旧' },
  unknown: { cls: 'fr-unknown', text: '还没有数据' },
  ok: null
};

function schoolBadgeHTML(fresh, school) {
  const st = schoolStatus(fresh, school);
  if (!st) return '';
  const meta = FRESH_LABEL[st.status];
  if (!meta) return '';
  const why = st.status === 'failed'
    ? '（' + (st.failed.join('、') || '未给原因') + '）'
    : (st.status === 'empty'
      ? '（' + st.boards_empty + '/' + st.boards + ' 个栏目没解析出内容，可能栏目改版）'
      : (st.age_hours !== null ? '（' + ageHoursText(st.age_hours) + '）' : ''));
  const title = st.status === 'failed'
    ? '这所学校本轮抓取失败：' + (st.failed.join('、') || '未给原因') + '。这不是「学校没发通知」。'
    : (st.status === 'empty'
      ? '抓到了页面，但 ' + st.boards_empty + '/' + st.boards + ' 个栏目一条都没解析出来，需要人工确认——不一定是学校没发。'
      : '这所学校的数据已经 ' + ageHoursText(st.age_hours) + ' 没更新（阈值 ' + fresh.stale_after_hours + ' 小时）。');
  return '<span class="fr-badge ' + meta.cls + '" title="' + esc(title) + '">' + esc(meta.text + why) + '</span>';
}

/** 列表页 / 匹配页顶部那一行：「1119 条 · 最近更新 27 分钟前 · 29 所学校 · 配置 37 个站点」。
 *  worst 不是 ok 的时候**必须把话说完**，不能只给一个好数字。 */
function freshnessBarHTML(fresh) {
  if (!fresh) return '';
  const t = fresh.totals || {};
  const n = (v) => (typeof v === 'number' ? v : null);

  if (fresh.worst === 'unknown' || !fresh.schools.length) {
    return '<div class="fbar fr-unknown">' +
      '<strong>还没有数据</strong> —— ' +
      esc(fresh.message || fresh.error || '每日流程还没跑过，跑一次更新这里就会出现数据时间。') +
      '</div>';
  }

  const bits = [];
  // ⚠️ 这一格说的是**"本轮抓取覆盖了多少"**，不是"库里一共有多少"。
  //
  // 2026-10-18 20:14 实测：A18 深抓之后 `/api/freshness` 只剩**最后一批深抓的 2 个单元 / 75 条**
  // （数据侧回归，已报 A）。原来这里写「75 条公告 · 1 所学校」，而库里有 2751 条 ——
  // 页面自己报了一个错数，还是最显眼的那一行。
  //
  // 所以措辞改成它真正在说的东西：**本轮抓取覆盖**。
  // 库里的总量属于 `/api/health/data` 的心跳那一行，不属于这里。
  if (n(t.records) !== null) bits.push('本轮抓取覆盖 <strong>' + t.records + '</strong> 条');
  // 用「本轮抓取于」而不是「数据更新于」：这两句会在同一屏出现，
  // 而它们量的不是一件事（heartbeat 的 data_age_minutes 与 freshness.generated_at
  // 实测能差 2 小时）。同屏写两句「数据更新于 X」、X 还不一样，是最容易被追问的矛盾。
  bits.push('本轮抓取于 <strong>' + esc(fresh.generated_age_text || ageHoursText(fresh.generated_age_hours) || '未知') + '</strong>');
  // ⛔ 不要写「覆盖 37 所学校」。37 是**站点数**，学校只有 29 所 ——
  // 这句话在 docs/DATA-NUMBERS.md §一 是被点名禁止的，而它原本就是从这一行长出来的。
  // 两个数分开说：学校数按 `school` 去重，站点数照配置算。
  //
  // 三层取值，任何一层都不能把两个概念混回去：
  //   ① normFreshness 算好的 school_count / site_count
  //   ② 只有原始 schools 数组（live.js 重渲染时可能就是这种）→ 现场去重
  //   ③ 只有 totals.schools（老接口）→ **只能当站点数说**，不许说成学校
  let sites = (typeof fresh.site_count === 'number') ? fresh.site_count : null;
  let schools = (typeof fresh.school_count === 'number') ? fresh.school_count : null;
  if ((sites === null || schools === null) && Array.isArray(fresh.schools) && fresh.schools.length) {
    if (sites === null) sites = fresh.schools.length;
    if (schools === null) schools = distinctSchoolCount(fresh.schools);
  }
  if (sites === null) sites = n(t.schools);
  if (schools !== null && schools === 0) schools = null;
  if (schools !== null && sites !== null && sites > schools) {
    bits.push('<strong>' + schools + '</strong> 所学校 · <strong>' + sites + '</strong> 个站点');
  } else if (schools !== null) {
    bits.push('<strong>' + schools + '</strong> 所学校');
  } else if (sites !== null) {
    bits.push('<strong>' + sites + '</strong> 个站点');
  }

  const warns = [];
  if (n(t.failed_schools)) {
    const names = fresh.schools.filter((s) => s.status === 'failed').map((s) => s.school + '（' + (s.failed.join('、') || '未给原因') + '）');
    warns.push('<span class="fb-warn">本次抓取失败 ' + t.failed_schools + ' 所：' + esc(names.slice(0, 4).join('、')) +
      (names.length > 4 ? ' 等' : '') + ' —— <strong>这不是「它们没有更新」</strong></span>');
  }
  if (n(t.empty_schools)) {
    warns.push('<span class="fb-warn">另有 ' + t.empty_schools + ' 所抓到了页面但<strong>一条都没解析出来</strong>，需人工确认</span>');
  }
  if (n(t.stale_schools)) {
    warns.push('<span class="fb-warn">' + t.stale_schools + ' 所超过 ' + fresh.stale_after_hours + ' 小时没更新</span>');
  }
  if (fresh.worst === 'stale') {
    // 时间戳和年龄必须说的是**同一件事**：generated_at 配它自己的年龄，
    // oldest_hours 是"最久没更新的那一所学校"的年龄，单独说。
    // 第一版把两者拼在一起，屏幕上出现了「数据停留在 10 月 19 日 12:02（2 天前）」
    // —— 12:02 明明是 10 小时前，2 天是另一所学校的年龄。
    const genAge = fresh.generated_age_text
      || relTimeText(fresh.generated_at, fresh.now) || null;
    warns.unshift('<span class="fb-warn">数据停留在 <strong>' + esc(stampText(fresh.generated_at)) + '</strong>' +
      (genAge ? '（' + esc(genAge) + '）' : '') +
      '，超过 ' + fresh.stale_after_hours + ' 小时的节拍</span>');
    if (fresh.oldest_hours !== null && fresh.oldest_hours !== undefined) {
      warns.push('<span class="fb-warn">最久的一所学校已经 ' + esc(ageHoursText(fresh.oldest_hours)) +
        '没有成功抓取</span>');
    }
  }

  const cls = fresh.worst === 'ok' ? 'ok' : ('fr-' + fresh.worst);
  return '<div class="fbar ' + cls + '">' +
    '<span class="fb-main">' + bits.join(' · ') + '</span>' +
    (warns.length ? '<span class="fb-warns">' + warns.join('') + '</span>' : '') +
    '<span class="fb-src">' + (fresh.from === 'api' ? '接口 /api/freshness' : '文件 data/out/freshness.json') +
      (fresh.stale_after_hours ? '　阈值 ' + fresh.stale_after_hours + ' 小时' : '') + '</span>' +
    '</div>';
}

/** 某校的「本次抓取失败 / 没解析出内容」清单（今日更新页用）。
 *  这一块的存在理由：**失败必须看得见**，不能和「没有变化」混在一起。 */
function freshnessIssuesHTML(fresh) {
  if (!fresh || !fresh.schools.length) return '';
  const bad = fresh.schools.filter((s) => s.status !== 'ok');
  if (!bad.length) return '';

  const rows = bad.map((s) => {
    const meta = FRESH_LABEL[s.status];
    const why = s.status === 'failed'
      ? '原因：<code>' + esc(s.failed.join('</code> <code>') || '未给') + '</code>'
      : (s.status === 'empty'
        ? s.boards_empty + ' / ' + s.boards + ' 个栏目一条都没解析出来'
        : '最后更新 ' + esc(stampText(s.last_ok)) + '（' + esc(ageHoursText(s.age_hours)) + '）');
    const note = s.status === 'failed'
      ? '<strong>这不是「这所学校没有更新」</strong>——是我们这一轮没抓到。'
      : (s.status === 'empty'
        ? '页面抓到了，但内容没解析出来。可能是栏目改版，需人工看一眼——<strong>也不能说成抓取失败</strong>。'
        : '这段时间内它的新公告可能没进库。');
    return '<li class="fi-item ' + meta.cls + '">' +
      '<div class="fi-head"><span class="fi-badge ' + meta.cls + '">' + esc(meta.text) + '</span>' +
      '<span class="fi-school">' + esc(s.school) + (s.unit ? ' · ' + esc(s.unit) : '') + '</span>' +
      '<span class="fi-count">库里 ' + s.records + ' 条</span></div>' +
      '<div class="fi-why">' + why + '　' + note + '</div>' +
    '</li>';
  }).join('');

  return '<div class="panel-head">' +
      '<h2 class="panel-title">哪些没抓到 / 没解析出来</h2>' +
      '<span class="hist-range">' + bad.length + ' / ' + fresh.schools.length + ' 个站点有情况</span>' +
    '</div>' +
    '<p class="section-note">这一节和上面的「新增 / 有变化」是<b>两件不同的事</b>：' +
      '上面说「内容有没有变」，这里说「我们这一轮有没有拿到」。' +
      '<strong>没抓到 ≠ 没有变化</strong>，混在一起说会让用户安心地以为自己了解情况。</p>' +
    '<ul class="fresh-issues">' + rows + '</ul>';
}

/* ---------- 接口优先、样例回退 ---------- */

/** 该接口后端**还没做**（404）时回退的标记。
 *  为什么要单独记一笔：`isClientError` 那条规矩（4xx 不回退）是为了防
 *  「拿样例冒充真数据」——404 是"这条不存在"、402 是"额度用完"，都是答复。
 *  但**新接口还没实现**时的 404 是另一种东西：它不是对数据的答复，是"这个功能还没有"。
 *  把这两种 404 混为一谈，新页面在后端就位前就全是错误态；
 *  反过来不区分就回退，又是在撒谎。所以：**允许回退，但必须标出来**（见 sourceText）。 */
let apiMissing = false;

async function liveOrFixture(liveFn, fixtureName, opts) {
  const missingOk = !!(opts && opts.missingOk);
  let liveErr = null;
  if (useLive()) {
    try {
      const data = await liveFn();
      dataSource = 'live';
      return data;
    } catch (err) {
      lastLiveError = err;
      liveErr = err;
      // 4xx 是接口给的答复，不是"接口没通"——不能拿样例顶上（那等于样例冒充真数据）
      if (isClientError(err)) {
        // 例外：这个接口后端还没实现（404），而调用方明确说"可以先落样例"
        if (missingOk && err.status === 404) apiMissing = true;
        else throw err;
      } else if (!ALLOW_FALLBACK) {
        throw err;
      }
    }
  }
  let data;
  try {
    data = await loadFixture(fixtureName);
  } catch (fxErr) {
    // 把接口那一步的错一起带上：否则页面上只看到"样例 404"，
    // 真正的错因（接口 500 / 超时）被埋掉了。
    fxErr.liveError = (liveErr && liveErr !== fxErr) ? liveErr : null;
    throw fxErr;
  }
  dataSource = 'fixtures';
  return data;
}

/* ---------- 数据源标记（必须可见） ---------- */

function sourceText() {
  if (dataSource === 'live') return '真接口 /api/*';
  if (dataSource === 'none') return '没读到（接口和本地样例都没通）';
  if (dataSource === 'fixtures') {
    if (API_MODE === 'fixtures') return '本地样例（?api=fixtures 指定）';
    // 这一档必须和"接口挂了"分开说：前者是功能还没上线（不可耻），
    // 后者是接口坏了（要修）。混成一句会让人以为后端崩了。
    if (apiMissing) return '本地样例（该接口后端尚未实现）';
    if (!lastLiveError) return '本地样例（该接口尚未实现）';
    return '本地样例（接口没通，已回退）';
  }
  return '读取中…';
}

/** 各页面 footer 里都有一个 #api-source，把数据源如实写进去；顺便给 body 打标记。
 *  **失败路径也必须调它** —— 否则页脚会一直显示 HTML 里那个静态默认值，
 *  等于在一个什么都没读到的页面上宣称"数据源：本地样例"（走查第 1 环抓到过）。 */
function markSource() {
  const node = document.getElementById && document.getElementById('api-source');
  if (node) {
    node.textContent = sourceText();
    node.className = dataSource === 'live' ? 'src-live'
      : (dataSource === 'none' ? 'src-none' : 'src-fixtures');
  }
  const body = document.body;
  if (body && body.classList && body.classList.add) {
    body.classList.add(dataSource === 'live' ? 'data-live'
      : (dataSource === 'none' ? 'data-none' : 'data-fixtures'));
  }
  if (dataSource === 'fixtures' && lastLiveError && API_MODE === 'live') {
    document.querySelectorAll('.foot').forEach((f) => {
      if (f.getAttribute('data-fallback-note')) return;
      const p = document.createElement('p');
      p.setAttribute('data-fallback-note', '1');
      p.className = 'fallback-note';
      // 两种回退的**原因完全不同**，不能共用一句话：
      //   · apiMissing：这个接口后端还没做（不是故障，别让人去重启服务）
      //   · 其余：接口真的没通（这时才该提示怎么把服务起起来）
      if (apiMissing) {
        p.textContent = '这个接口后端还没实现（' + lastLiveError.message +
          '），页面用的是本地样例 —— 数据形状一致，但不是实时数据。接口上线后这里会自动切过去。';
      } else {
        p.textContent = '接口没通，已回退到本地样例：' + lastLiveError.message +
          '（真接口方式：在仓库根目录跑 python -m uvicorn server.app:app --port 8001）';
      }
      f.appendChild(p);
    });
  }
}

/* ---------- 通用小工具 ---------- */

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** 只允许 http/https 变成链接，别把 javascript: 之类的东西渲染成「看原文」。 */
function safeUrl(u) {
  if (!u) return null;
  try {
    const x = new URL(u, location.href);
    return (x.protocol === 'http:' || x.protocol === 'https:') ? u : null;
  } catch (e) {
    return null;
  }
}

const BUCKETS = {
  '立即行动':   { cls: 'b-now',   hint: '7 天内截止' },
  '重点关注':   { cls: 'b-focus', hint: '30 天内截止' },
  '可规划':     { cls: 'b-plan',  hint: '还有一个多月' },
  '需人工确认': { cls: 'b-todo',  hint: '没抽到截止日' },
  '已过期':     { cls: 'b-gone',  hint: '已过期' }
};

function bucketMeta(b) {
  return BUCKETS[b] || { cls: 'b-todo', hint: '' };
}

/** 剩余天数直接用接口给的 days_left，不在前端用「今天」重算——
 *  否则页面上的天数和后端/截图里的对不上，演示时会当场穿帮。 */
function daysLeftText(n) {
  if (typeof n !== 'number') return null;
  if (n < 0) return '已过期 ' + Math.abs(n) + ' 天';
  if (n === 0) return '今天截止';
  return '还剩 ' + n + ' 天';
}

/* ---------- 匹配用的演示画像 ---------- */
/* 真接口 POST /api/match 的 body 就是画像本身（不是 {profile:...}）。
 * 现在还没有画像录入表单，先用这份和 fixtures 一致的示例画像；
 * 以后加了表单，把它换成用户输入即可。 */
const DEMO_PROFILE = {
  name: '示例：浙大物理方向大三',
  school: '浙江大学',
  majors: ['物理学', '力学', '应用物理', '光学', '材料'],
  grade: '大三',
  target_entry_year: 2027,
  gpa_rank: { rank: 5, total: 120 },
  english: { cet6: 580, toefl: null, ielts: null },
  research: ['CUPT 国赛一等奖', '凝聚态实验一段'],
  interest_keywords: ['凝聚态物理', '超导', '量子', '自旋电子学'],
  target_levels: ['直博', '硕士'],
  wanted_modes: ['推免', '预推免', '夏令营'],
  preferred_schools: ['浙江大学', '中国科学技术大学', '复旦大学', '北京大学', '南京大学'],
  preferred_units: ['物理学院', '材料科学与工程学院', '研究生院'],
  exclude_expired: true
};

/** 极简 markdown 行内转换：只把 **粗体** 转成 <strong>。
 *  后端给的口径文字 / warnings 里可能带 markdown 星号，直接 esc 就会在页面上看到 `**`（走查 #7）。 */
function mdInline(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
}

/* ---------- 复制到剪贴板（手机上最容易断的一步） ----------
 *
 * 「把导师邮箱复制走」是套磁信那条路径的最后一米，在手机上最容易断：
 *   · `navigator.clipboard` 只在**安全上下文**里有（https，或者 http 的 localhost）。
 *     演示如果用局域网 IP（http://192.168.x.x:8001）打开，它直接是 undefined；
 *   · 就算有，`writeText` 也可能被权限策略挡掉。
 * 所以两条路都要有：现代 API 不行就退回「选中 + execCommand」，
 * 两条都不行时**如实说**，并且邮箱地址本来就在旁边显示着（长按能选）。
 *
 * 用事件委托挂在 document 上：卡片是 innerHTML 重渲染出来的，逐个绑会漏。 */
async function copyText(text) {
  const s = String(text == null ? '' : text);
  if (!s) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch (e) { /* 落到下面的兜底 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = s;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, s.length);
    const ok = document.execCommand && document.execCommand('copy');
    document.body.removeChild(ta);
    return !!ok;
  } catch (e) {
    return false;
  }
}

document.addEventListener('click', async (e) => {
  const btn = e.target && e.target.closest ? e.target.closest('[data-copy]') : null;
  if (!btn) return;
  e.preventDefault();
  const text = btn.getAttribute('data-copy');
  if (!btn.getAttribute('data-label')) btn.setAttribute('data-label', btn.textContent);
  const label = btn.getAttribute('data-label');
  const ok = await copyText(text);
  btn.textContent = ok ? '已复制 ✓' : '复制失败，长按选中';
  btn.classList.toggle('copy-ok', ok);
  btn.classList.toggle('copy-bad', !ok);
  setTimeout(() => {
    btn.textContent = label;
    btn.classList.remove('copy-ok', 'copy-bad');
  }, 1800);
});

/* ---------- 用户画像（匹配页填、套磁信用，共用一份） ---------- */

const PROFILE_KEY = 'tuimian_profile';

function loadProfile() {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (p && typeof p === 'object') return Object.assign({}, DEMO_PROFILE, p);
    }
  } catch (e) { /* 没有 localStorage 就退回示例画像 */ }
  return Object.assign({}, DEMO_PROFILE);
}

function saveProfile(p) {
  try { localStorage.setItem(PROFILE_KEY, JSON.stringify(p)); } catch (e) {}
}

/* ---------- 错误态 ---------- */

/** 出了事，页面上要说三件事：**哪一步坏了、现在能做什么、原始报错**。
 *  走查第 1 环改过一次，原因是原来的说法会把人带偏：
 *   · 接口 404（这条公告不存在）被说成"连不上接口"，还建议你去重启服务；
 *   · "底层报错"里只有样例文件的 404，真正的 500 被埋掉了；
 *   · 页脚还留着 HTML 静态默认值，宣称在用本地样例。 */

function errTitle(err) {
  const st = err && err.status;
  if (err && err.timeout) return '接口没有响应（超时）';
  if (st === 404) return '接口说：没有这条数据';
  if (st === 402) return '免费额度用完了';
  if (st) return '接口返回了 ' + st;
  if (err && err.network) return '连不上接口';
  if (err && err.isDataError) return '没能读到数据';
  return '页面出错了';
}

function errHint(err) {
  const st = err && err.status;
  if (err && err.timeout) {
    return '<p>请求超过 <strong>' + Math.round(FETCH_TIMEOUT_MS / 1000) + ' 秒</strong>没有返回，页面不再干等。' +
      '服务可能在跑但卡住了，也可能是网络问题。点下面的「重试」再来一次。</p>';
  }
  if (st === 404) {
    return '<p>接口是通的，它明确说这条数据不存在。常见原因：链接里的 id 过期了，' +
      '或者抓取调整后同一条公告换了 id（日报里叫「id 变更」）。</p>' +
      '<p><a class="btn-ghost" href="notices.html">← 回公告列表</a></p>';
  }
  if (st === 402) {
    return '<p>免费身份的<strong>详情页</strong>额度是每天 3 条，今天的用完了（推送额度另算）。' +
      '<a href="pricing.html">去订阅页看看</a>，或者换个身份。</p>';
  }
  if (st) {
    return '<p>服务端返回了 <strong>' + st + '</strong>，不是这一页没写。' +
      '点「重试」；如果一直这样，把下面的原始报错发给后端。</p>';
  }
  return '<p>这一页要么连不上接口，要么找不到本地样例。两条路都给出来：</p>' +
    '<p><strong>① 真接口</strong>（在<strong>仓库根目录</strong>）：<code>python run.py</code>，' +
    '然后访问 <code>http://127.0.0.1:8000/</code>；如果 8000 被占就用 ' +
    '<code>python -m uvicorn server.app:app --port 8001</code>，URL 里的端口跟着换。' +
    '（服务会把 <code>web/</code> 同源挂载，没有跨域。）</p>' +
    '<p><strong>② 完全离线看样例</strong>：在<strong>仓库根目录</strong>跑 ' +
    '<code>python -m http.server 5173</code>，然后打开 ' +
    '<code>http://127.0.0.1:5173/web/</code> 或加 <code>?api=fixtures</code>。' +
    '注意<strong>样例目录不挂在 <code>/api</code> 那个服务上</strong>，' +
    '所以在真接口服务上加 <code>?api=fixtures</code> 是读不到的。</p>';
}

function errDetailHTML(err, liveErr) {
  const lines = [];
  if (liveErr) {
    lines.push('接口 ' + (liveErr.path || '') + ' → ' + liveErr.message);
  }
  if (err && err.attempts && err.attempts.length) {
    err.attempts.forEach((a) => lines.push('样例 ' + a));
  } else if (err && err.message && err !== liveErr) {
    lines.push(err.message);
  }
  if (!lines.length) return '';
  return '<details class="err-detail"><summary>原始报错</summary><pre>' +
    esc(lines.join('\n')) + '</pre></details>';
}

function showError(node, err) {
  if (!node) return;
  const liveErr = (err && err.liveError) || lastLiveError || null;
  const primary = (liveErr && liveErr !== err) ? liveErr : (err || liveErr);
  // 接口那一步如果是"超时 / 连不上 / 明确的状态码"，它才是真正的原因，标题要报它；
  // 否则报手上的错误（多半是"样例也读不到"）。
  const clear = !!(primary && (primary.status || primary.timeout || primary.network));
  const shown = clear ? primary : (err || primary);

  if (dataSource === null) dataSource = 'none';
  markSource();

  node.className = 'state err';
  node.innerHTML =
    '<h3>' + esc(errTitle(shown)) + '</h3>' +
    errHint(shown) +
    '<div class="state-actions">' +
      '<button type="button" class="btn-ghost" onclick="location.reload()">重试</button>' +
      '<a class="btn-ghost" href="notices.html">公告列表</a>' +
      '<a class="btn-ghost" href="/">匹配结果</a>' +
    '</div>' +
    errDetailHTML(err, primary === liveErr ? liveErr : null);
}
/* 心跳自启动：common.js 在每页底部加载，此时 DOM 已就绪；没有宿主就什么都不做。 */
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initHeartbeat);
} else {
  initHeartbeat();
}

/** 最近 3 天为空时的**实情文案**（C7）。
 *
 * ⛔ 不许写「暂无数据」「没有符合条件的公告」。
 *    空档有两种：① 我们没抓（我们的问题）② 官网真没发（事实）。
 *    这一档几乎总是第 ② 种，所以要把"最近 3 天没发"和"本周还有多少条"一起说出来，
 *    否则用户以为整个库是空的。
 */
function d3EmptyHTML(counts) {
  const week = (counts && counts.week) || 0;
  const month = (counts && counts.month) || 0;
  let nudge;
  if (week > 0) nudge = '下面是本周的 <strong>' + week + '</strong> 条';
  else if (month > 0) nudge = '本周也没有新的，本月还有 <strong>' + month + '</strong> 条';
  else nudge = '本月也没有新的，可以切到「资料库」看历史参考';
  return '<div class="state">' +
    '<p><strong>官网近 3 天没有新的 2027 招生公告</strong>，' + nudge + '。</p>' +
    '<p class="muted">这不是「我们没抓」：每一轮都真的抓了，' +
    '只是这几个站点最近 3 天没发出新的 2027 招生类公告。</p>' +
    '</div>';
}