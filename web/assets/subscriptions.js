/* 我的订阅（智能体 C，C12，2026-10-19）
 *
 * 数据源：`GET /api/watch?user=` / `POST /api/watch` / `DELETE /api/watch/{id}?user=`
 *
 * ## 这一页在三件事上不许含糊
 *
 * 1. **关注规则在服务端**，不是 localStorage。判据原文是"刷新后还记得"，
 *    localStorage 也能"记得"，但它只是在浏览器里记得 —— 换台机器就没了，
 *    而规则本来是要拿去做每日推送的。所以这里**只用服务端的数据**。
 *
 * 2. **「提醒」开关只在本机**。服务端 `/api/watch` 没有 enabled 字段
 *    （那是 A/B 的模块，我不改），所以这个开关只影响页头通知里要不要出现这条规则
 *    命中的东西。页面上必须写明范围 —— 一个看起来很服务端的开关，其实只在这台机器上有效，
 *    是最容易让人误判的那类界面。
 *
 * 3. **取关失败必须说出来**。契约 §11：取关不存在的 id 返回 404 而不是 200，
 *    就是为了不让「点了取消、其实还在收提醒」悄悄发生。所以这里 404 也要说清楚，
 *    并且**重新拉一次**列表，让屏幕和服务器对齐。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const state = {
    watches: [],
    schools: [],
    units: [],
    muted: localList('muted'),   // 本机静音的规则 id（见 common.js localList）
    loaded: false,
    loadErr: null
  };

  /* ---------- 本机的静音开关 ---------- */

  function mutedSet() { return new Set(state.muted); }

  function setMuted(id, muted) {
    const s = mutedSet();
    if (muted) s.add(id); else s.delete(id);
    state.muted = Array.from(s);
    localSet('muted', state.muted);
  }

  function mutedCount() {
    const ids = new Set(state.watches.map((w) => w.id));
    return state.muted.filter((id) => ids.has(id)).length;
  }

  /* ---------- 读 ---------- */

  async function loadWatch() {
    try {
      const d = await apiGet('/api/watch', userQuery());
      applyWatchPayload(d);
      state.loadErr = null;
      dataSource = 'live';
    } catch (e) {
      state.loadErr = e;
      dataSource = 'none';
    }
    markSource();
    state.loaded = true;
    render();
  }

  function applyWatchPayload(d) {
    state.watches = (d && d.watches) || [];
    // 学校/院系的去重清单**从 watches 自己算**，不依赖接口有没有给 `schools`/`units`：
    // `GET /api/watch` 会给这两个字段，`POST`/`DELETE` 的响应**只给 watches** ——
    // 第一版直接读 `d.schools`，于是加完一条规则后摘要显示「覆盖 0 所学校」，
    // 而列表里明明有两条。同一个数字不许两种来源，这里只留一种。
    const uniq = (arr) => {
      const out = [];
      arr.forEach((v) => { const s = String(v == null ? '' : v).trim(); if (s && out.indexOf(s) === -1) out.push(s); });
      return out;
    };
    state.schools = uniq(state.watches.map((w) => w.school));
    state.units = uniq(state.watches.map((w) => w.unit));
  }

  /* 建议值：学校和学院从**真数据**里来，不让用户瞎猜拼写（子串匹配虽然宽松，
   * 但「北大」这种简称匹配不到「北京大学」—— 他能填错，我们就该给提示）。 */
  async function loadSuggest() {
    const [fresh, facets] = await Promise.all([
      apiGet('/api/freshness').catch(() => null),
      apiGet('/api/facets').catch(() => null)
    ]);
    const schools = new Set();
    const units = new Set();
    if (fresh && Array.isArray(fresh.schools)) {
      fresh.schools.forEach((s) => {
        if (s && s.school) schools.add(String(s.school));
        if (s && s.unit) units.add(String(s.unit));
      });
    }
    if (facets) {
      (facets.schools || []).forEach((x) => x && x.value && schools.add(String(x.value)));
      (facets.units || []).forEach((x) => x && x.value && units.add(String(x.value)));
    }
    fillDatalist('school-list', Array.from(schools).sort());
    fillDatalist('unit-list', Array.from(units).sort());
    const scope = el('f-scope');
    if (scope) {
      scope.textContent = schools.size
        ? '可选的学校 ' + schools.size + ' 所、单位 ' + units.size + ' 个（下拉框里是抓到的真名字）。'
        : '';
    }
  }

  function fillDatalist(id, values) {
    const node = el(id);
    if (!node) return;
    node.innerHTML = values.map((v) => '<option value="' + esc(v) + '"></option>').join('');
  }

  /* ---------- 画 ---------- */

  function render() {
    const list = el('subs-list');
    const sum = el('subs-summary');
    const hint = el('subs-list-hint');
    if (!list || !sum) return;

    if (state.loadErr) {
      sum.textContent = '关注规则没读到';
      list.innerHTML = '';
      el('state').innerHTML =
        '<div class="state err"><h3>关注规则读不到</h3>' +
        '<p>这一页要问服务端的 <code>/api/watch</code>：' + esc((state.loadErr && state.loadErr.message) || state.loadErr) + '</p>' +
        '<p>读不到就<strong>不知道你现在关注了什么</strong> —— 我不会拿"空列表"顶上去，那看起来像"你没关注过"。</p>' +
        '<div class="state-actions"><button type="button" class="btn-ghost" onclick="location.reload()">重试</button></div></div>';
      el('state').hidden = false;
      if (hint) hint.hidden = true;
      return;
    }

    el('state').hidden = true;

    const n = state.watches.length;
    sum.textContent = n
      ? '关注 ' + n + ' 条规则 · 覆盖 ' + state.schools.length + ' 所学校、' + state.units.length + ' 个院系或单位'
      : '还没有关注任何规则';

    if (!n) {
      list.innerHTML = '<li class="subs-row"><div class="subs-what">' +
        '<div class="subs-title">还没有关注任何学校 / 学院</div>' +
        '<div class="subs-meta">下面加一条；或者在公告详情页点「关注这个学院」，一步到位（学校 + 学院一起关注）。</div>' +
        '</div></li>';
    } else {
      const muted = mutedSet();
      list.innerHTML = state.watches.map((w) => {
        const off = muted.has(w.id);
        const kw = (w.keywords || []).filter(Boolean);
        const title = (w.school || '（只按关键词）') + (w.unit ? ' · ' + w.unit : '');
        return '<li class="subs-row' + (off ? ' subs-off' : '') + '" data-id="' + esc(w.id) + '">' +
          '<div class="subs-what">' +
            '<div class="subs-title">' + esc(title) +
              (kw.length ? '<span class="subs-kw">' + kw.map((k) => '<span class="why-chip">' + esc(k) + '</span>').join('') + '</span>' : '') +
            '</div>' +
            '<div class="subs-meta">关注于 ' + esc(shortTime(w.created_at)) + ' · 规则 <code>' + esc(w.id) + '</code>' +
              (off ? ' · 本机已关提醒' : '') + '</div>' +
          '</div>' +
          '<div class="subs-act">' +
            '<label class="subs-toggle" title="只影响这台浏览器的页头通知；规则本身不会删"><input type="checkbox" data-mute="' +
              esc(w.id) + '"' + (off ? '' : ' checked') + '> 提醒</label>' +
            '<button type="button" class="btn-ghost btn-mini" data-del="' + esc(w.id) + '">取消关注</button>' +
          '</div>' +
        '</li>';
      }).join('');
    }

    if (hint) {
      const k = mutedCount();
      hint.hidden = !k;
      hint.textContent = k ? '有 ' + k + ' 条规则在这台浏览器上关了提醒（服务端不知道，也不影响它的每日推送口径）。' : '';
    }
  }

  function shortTime(iso) {
    const s = String(iso == null ? '' : iso).replace('T', ' ').slice(0, 16);
    return s || '（时间没记）';
  }

  /* ---------- 写 ---------- */

  async function addWatch() {
    const school = (el('f-school') || {}).value || '';
    const unit = (el('f-unit') || {}).value || '';
    const kwRaw = (el('f-kw') || {}).value || '';
    const msg = el('f-msg');
    const btn = el('f-add');

    const keywords = kwRaw.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    if (!school.trim() && !unit.trim() && !keywords.length) {
      if (msg) { msg.textContent = '至少要填一样：学校、学院，或关键词。'; msg.className = 'pf-hint pf-hint-warn'; }
      return;
    }

    if (btn) btn.disabled = true;
    if (msg) { msg.textContent = '正在关注…'; msg.className = 'pf-hint'; }
    try {
      // body 里叫 user_id（查询参数里才叫 user）—— 写错会静默落到 u_demo
      const d = await apiPost('/api/watch', {}, {
        user_id: currentUser(),
        school: school.trim() || undefined,
        unit: unit.trim() || undefined,
        keywords: keywords.length ? keywords : undefined
      });
      applyWatchPayload(d);
      if (msg) {
        msg.textContent = (d && d.created) ? '已关注。' : '这条已经关注过了（服务端幂等，没有重复）。';
        msg.className = 'pf-hint pf-hint-ok';
      }
      if (el('f-kw')) el('f-kw').value = '';
      render();
    } catch (e) {
      // 400 是"你填的不能这么关注"（服务端有明确一句），超时/没通是另一回事
      if (msg) {
        msg.textContent = '关注失败：' + ((e && e.message) || e);
        msg.className = 'pf-hint pf-hint-warn';
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /* 从画像一键生成关注。
   *
   * 画像里的字段是 **preferred_schools**（不是 intended_schools —— 那个名字早改了，
   * 写错不会报错，只会安静地读到空数组，然后用户以为"我的画像没保存"）。
   * 画像存在本机，关注规则存在服务端：所以这一步是"把本机的东西推到服务端"，
   * 每所学校一条规则，**逐条发、逐条报成败** —— 不要 Promise.all 全成功才刷新，
   * 那样一条失败就不知道是哪条、也不知道其实已经成功了几条。
   */
  async function addFromProfile() {
    const p = (typeof loadProfile === 'function') ? loadProfile() : {};
    const schools = (p && p.preferred_schools) || [];
    const msg = el('f-msg');
    const btn = el('f-from-profile');

    if (!schools.length) {
      if (msg) {
        msg.textContent = '画像里还没有「意向学校」——去「我的画像」填一下，或者直接在上面填学校名。';
        msg.className = 'pf-hint pf-hint-warn';
      }
      return;
    }

    if (btn) btn.disabled = true;
    if (msg) { msg.textContent = '正在按画像里的 ' + schools.length + ' 所学校生成…'; msg.className = 'pf-hint'; }
    let ok = 0, dup = 0, fail = 0;
    for (const s of schools) {
      const name = String(s || '').trim();
      if (!name) continue;
      try {
        const d = await apiPost('/api/watch', {}, { user_id: currentUser(), school: name });
        applyWatchPayload(d);
        if (d && d.created) ok++; else dup++;
      } catch (e) { fail++; }
    }
    if (btn) btn.disabled = false;
    render();
    if (msg) {
      const bits = ['新关注 ' + ok + ' 所'];
      if (dup) bits.push('已经关注过 ' + dup + ' 所');
      if (fail) bits.push(fail + ' 所失败');
      msg.textContent = bits.join('，') + '。';
      msg.className = 'pf-hint' + (fail ? ' pf-hint-warn' : ' pf-hint-ok');
    }
  }

  async function removeWatch(id) {
    const row = document.querySelector('.subs-row[data-id="' + id + '"]');
    const btn = row ? row.querySelector('[data-del]') : null;
    if (btn) { btn.disabled = true; btn.textContent = '取消中…'; }
    try {
      const r = await fetchWithTimeout('/api/watch/' + encodeURIComponent(id) + qs(userQuery()),
        { method: 'DELETE', cache: 'no-store' }, FETCH_TIMEOUT_MS, '/api/watch/{id}');
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        const err = new Error((d && d.error && d.error.message) || ('HTTP ' + r.status));
        err.status = r.status;
        throw err;
      }
      applyWatchPayload(d);
      setMuted(id, false);          // 规则都没了，本机那条静音记录也一起清掉
      render();
      flash('已取消关注。', 'ok');
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = '取消关注'; }
      if (e && e.status === 404) {
        // 服务端说它不存在 —— 屏幕和服务器不一致，以服务器为准重新拉一遍
        flash('这条规则服务端说已经没有了（可能刚在别处取消过）。列表已按服务端刷新。', 'warn');
        await loadWatch();
      } else {
        flash('取消关注失败：' + ((e && e.message) || e) + ' —— 这条规则**可能还在**，刷新确认一下。', 'warn');
      }
    }
  }

  /* ---------- 预览：关注了到底会不会收到东西 ----------
   *
   * 这一页现在能加关注、能取消，但用户看不到「所以我到底会收到什么」——
   * 那正是「订阅了看不到动静等于没订阅」的另一半。
   *
   * 数据**不在这里重新算**：直接问 `window.tuimianNotify`（notify.js 的那套匹配），
   * 保证「预览里有的，铃铛里一定有」。订阅页先加载 notify.js，所以它一定在。
   */
  async function previewNotifications() {
    const btn = el('preview-btn');
    const msg = el('preview-msg');
    const body = el('preview-body');
    if (!body) return;

    if (!state.watches.length) {
      body.hidden = false;
      body.innerHTML = '<p class="subs-hint subs-hint-warn">还没有关注规则 —— 先加一条，才谈得上「会收到什么」。</p>';
      return;
    }

    if (btn) btn.disabled = true;
    if (msg) { msg.textContent = '按你现在的关注算一遍…'; msg.className = 'pf-hint'; }

    const api = window.tuimianNotify;
    if (!api) {
      if (msg) { msg.textContent = '通知模块没加载（assets/notify.js），这一页算不出预览。'; msg.className = 'pf-hint pf-hint-warn'; }
      if (btn) btn.disabled = false;
      return;
    }

    try {
      // 铃铛那边是延迟 1.2 秒才拉；这里点了按钮就**立刻拉一次**（用户明确要了）
      await api.reload();
    } catch (e) { /* 内部已把错误记在 api.error() 里 */ }

    if (btn) btn.disabled = false;
    body.hidden = false;

    const err = api.error && api.error();
    if (err) {
      // 「没读到」和「没有」是两句话，这里绝不能说成"最近很安静"
      body.innerHTML = '<p class="subs-hint subs-hint-warn">预览没算出来：' + esc(String(err)) +
        '　—— 这不等于「你没有新东西」。</p>';
      if (msg) { msg.textContent = '没算出来'; msg.className = 'pf-hint pf-hint-warn'; }
      return;
    }

    const items = api.top(3);
    const total = api.count ? api.count() : items.length;
    if (!items.length) {
      body.innerHTML = '<p class="subs-hint">关注了 ' + (api.watchCount ? api.watchCount() : state.watches.length) +
        ' 条规则，最近 7 天没有跟它们有关的新公告。<br>' +
        '<span class="subs-meta">这是"看过了，确实没有"，不是"没算"。</span></p>';
      if (msg) { msg.textContent = ''; msg.className = 'pf-hint'; }
      return;
    }

    body.innerHTML = '<p class="subs-lead">按现在的关注，最近会有 <strong>' + total +
      '</strong> 条进通知。下面是最新的 ' + items.length + ' 条：</p>' +
      '<ul class="subs-list">' + items.map((it) =>
        '<li class="subs-row"><div class="subs-what">' +
          '<div class="subs-title"><a href="detail.html?id=' + encodeURIComponent(it.notice_id) + '">' +
            esc(it.title) + '</a></div>' +
          '<div class="subs-meta">' + esc(it.why) + ' · ' + esc(it.when) + '</div>' +
        '</div></li>').join('') + '</ul>' +
      '<p class="subs-hint">这 3 条和页头铃铛里的是<strong>同一批</strong>（同一套匹配，不是另算一份）。' +
        '点标题进详情，详情页里能看原文。</p>';
    if (msg) { msg.textContent = ''; msg.className = 'pf-hint'; }
  }

  function flash(text, tone) {
    const hint = el('subs-list-hint');
    if (!hint) return;
    hint.hidden = false;
    hint.textContent = text;
    hint.className = 'subs-hint' + (tone === 'warn' ? ' subs-hint-warn' : '');
  }

  /* ---------- 事件 ---------- */

  function wire() {
    document.addEventListener('change', (e) => {
      const cb = e.target && e.target.getAttribute ? e.target.getAttribute('data-mute') : null;
      if (!cb) return;
      setMuted(cb, !e.target.checked);
      render();
      flash(e.target.checked ? '这台浏览器上恢复提醒。' : '这台浏览器上关掉提醒（规则还在，服务端不知道）。', 'ok');
    });

    document.addEventListener('click', (e) => {
      const del = e.target && e.target.closest ? e.target.closest('[data-del]') : null;
      if (!del) return;
      removeWatch(del.getAttribute('data-del'));
    });

    const add = el('f-add');
    if (add) add.addEventListener('click', addWatch);
    const fromProfile = el('f-from-profile');
    if (fromProfile) fromProfile.addEventListener('click', addFromProfile);
    const preview = el('preview-btn');
    if (preview) preview.addEventListener('click', previewNotifications);
    ['f-school', 'f-unit', 'f-kw'].forEach((id) => {
      const n = el(id);
      if (n) n.addEventListener('keydown', (e) => { if (e.key === 'Enter') addWatch(); });
    });
  }

  /* 从别的页面深链过来（详情页的「同类公告」走公告页，这里给表单预填用） */
  function prefillFromQuery() {
    let q = null;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    const s = q.get('school');
    const u = q.get('unit');
    if (s && el('f-school')) el('f-school').value = s;
    if (u && el('f-unit')) el('f-unit').value = u;
  }

  async function boot() {
    prefillFromQuery();
    wire();
    await loadWatch();       // 先把"我现在关注了什么"落屏，再补建议值
    loadSuggest();           // 推荐值可以慢，不阻塞
  }

  boot();
})();
