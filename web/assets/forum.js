/* 论坛页（智能体 C）
 * 数据源：GET /api/posts（契约 api.md 第 4 节）+ GET /api/posts/facets
 * 三条硬要求：
 *   1. 前台只渲染 9 个公开字段，author_real_id / created_ip / review_status 一个都不出现；
 *   2. 举报要防滥用并给反馈（服务端 deduped=true，界面显示「你已举报过」）；
 *   3. 页面上写明「后台实名、前台匿名代号」。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  /* 契约第 4 节的字段表：一个不多一个不少。
   * body_truncated 是 B 的实现为了「全文归订阅」加的截断标记（见 server/forum.py），
   * 不是内容字段，所以单独列出来。 */
  const PUBLIC_FIELDS = ['id', 'unit', 'tag', 'title', 'body',
                         'author_display', 'created_at', 'reply_count', 'report_count'];
  const EXTRA_FIELDS = ['body_truncated'];
  const FORBIDDEN_FIELDS = ['author_real_id', 'created_ip', 'review_status'];
  const REPORTED_KEY = 'tuimian_reported_ids';

  const state = {
    posts: null, facets: null,
    unit: 'ALL', tag: 'ALL', q: '',
    reported: null, submitMsg: null, reportMsg: null
  };

  async function boot() {
    try {
      const posts = await liveOrFixture(
        () => apiGet('/api/posts', userQuery({ size: 50 })),
        'posts.list.json'
      );

      // facets 没有对应的 fixture：拿不到就从帖子自身派生，页面照样能用
      let facets = null;
      if (useLive()) facets = await apiGet('/api/posts/facets').catch(() => null);

      state.posts = posts;
      state.facets = facets;
      state.reported = loadReported();

      markSource();
      renderCompliance(posts.items || []);
      renderPostBox();
      renderToolbar();

      const results = el('results');
      results.addEventListener('click', onResultsClick);
      render();
      el('state').hidden = true;

      // 「我的举报」是**独立一趟**：它查不到不该把论坛整页打掉（和新鲜度同一个原则）
      renderMyReports();
      const rf = el('my-reports-refresh');
      if (rf) rf.addEventListener('click', renderMyReports);
    } catch (err) {
      showError(el('state'), err);
    }
  }

  /* ---------- 我的举报：把「举报之后怎么样了」变成界面上查得到的东西 ----------
   *
   * 合规闭环的四句话是「收得到、处理得掉、留得下痕、说得出依据」。
   * 前三句是服务端的事，**第四句的最后一环在界面上** —— 举报人得能查到结果，
   * 否则"我们处理了"就只是我们自己说的。
   *
   * 两条硬约束（都是契约 §13.6 定的，不是我自己加的）：
   *   ① **按关系裁剪**：举报人只拿得到 `handled` + `message`，看不到依据类别、
   *      也看不到别的举报人。作者才看得到 `reason_label`。
   *      这不是"接口没做好"，是匿名要求本身。所以界面上要**说明为什么你看到的信息少**，
   *      否则用户会以为页面漏了东西。
   *   ② **查不到要说查不到**：403 / 404 都不是空白，得写成一句人话。
   */

  async function renderMyReports() {
    const wrap = el('my-reports-wrap');
    const box = el('my-reports');
    const cnt = el('my-reports-count');
    if (!wrap || !box) return;

    const ids = Array.from(state.reported || []);
    if (!ids.length) { wrap.hidden = true; return; }
    wrap.hidden = false;

    const user = currentUser();
    // 逐条查（接口是"按帖查"，没有"我的举报列表"这种接口，也不该为它加一个）
    const rows = await Promise.all(ids.slice(0, 20).map(async (id) => {
      try {
        const d = await apiGet('/api/posts/' + encodeURIComponent(id) + '/moderation', { user: user });
        return { id: id, data: d };
      } catch (e) {
        return { id: id, err: e };
      }
    }));

    /* ---------- 404 的举报记录要剔掉（2026-10-18）----------
     *
     * **404 = 这条帖在库里已经查不到了**（被删，或者换了一批演示数据）。
     * 处置记录是挂在帖子上的，帖子没了它就永远是 404 ——
     * 留着只会每次进这一页都白打一次请求，控制台还堆两条红字。
     * 之前那两条（`p_20261017_0003` / `_0004`）就是这么来的。
     *
     * ⛔ **只剔 404，403 一定不能剔**：403 是"帖子还在，但你不是作者也不是举报人"，
     *    换一个 `user` 参数就会变成 403 —— 把它当"过期"删掉，等于**删掉一条真实存在的举报**。
     *    这两个码长得像，含义正相反。
     *
     * ⚠️ 剔掉之后**必须说一句为什么少了**。悄悄消失比留着更糟：
     *    用户会以为自己的举报被撤销了，实际上只是那条帖没了。
     */
    const stale = rows.filter((r) => r.err && r.err.status === 404);
    const staleIds = stale.map((r) => r.id);
    const kept = rows.filter((r) => !(r.err && r.err.status === 404));
    if (staleIds.length) {
      staleIds.forEach((id) => state.reported.delete(id));
      saveReported();
    }

    if (!kept.length) {
      // 全被剔掉：不能直接 `hidden = true` —— 那样等于"举报记录凭空消失"。
      if (!staleIds.length) { wrap.hidden = true; return; }
      if (cnt) cnt.textContent = '0 条';
      box.innerHTML = staleNoteHTML(staleIds.length);
      return;
    }

    if (cnt) cnt.textContent = kept.length + ' 条';

    box.innerHTML = '<ul class="rep-list">' + kept.map(reportRowHTML).join('') + '</ul>' +
      (staleIds.length ? staleNoteHTML(staleIds.length) : '') +
      '<p class="section-note">举报人只能看到<strong>「已处理 / 正在处理」</strong>，' +
      '看不到依据类别、也看不到别的举报人 —— 这是<strong>匿名要求</strong>，不是页面没做全。' +
      '依据类别只有<strong>作者</strong>看得到。</p>';
  }

  /** 陈旧记录的说明。措辞只说**我们知道的那件事**（查不到），不替服务端解释原因。 */
  function staleNoteHTML(n) {
    return '<p class="section-note rep-stale">另有 <strong>' + n + '</strong> 条举报记录已经' +
      '<strong>在库里查不到了</strong>（那条帖被删除，或者我们换了一批数据），' +
      '处置记录跟着帖子一起没了，所以已从本地记录里清掉 —— ' +
      '<strong>这不是你的举报被撤销</strong>。</p>';
  }

  function reportRowHTML(r) {
    // 帖子里还查得到的就带标题，查不到（已下架/已删）就只写 id —— 不编一个标题出来
    const found = state.posts && state.posts.items
      ? state.posts.items.find((x) => x.id === r.id) : null;
    const name = found && found.title
      ? esc(found.title)
      : '<span class="muted">这条帖现在在前台看不到（可能已下架或删除）</span>';

    let chip = '<span class="pill-unknown">查不到</span>';
    let why = '';
    if (r.data) {
      const d = r.data;
      if (d.your_role === 'reporter') {
        // 举报人：只有 handled + message
        chip = d.handled
          ? '<span class="pill-ok">已处理</span>'
          : '<span class="pill-pending">正在处理</span>';
        why = esc(d.message || '');
        if (!d.handled && d.due_at) why += '　<span class="muted">承诺在 ' + esc(String(d.due_at).slice(0, 16).replace('T', ' ')) + ' 前给结果</span>';
      } else {
        // 作者：状态 + 依据类别（不是举报人）
        const cls = d.status === 'visible' || d.status === 'rejected' ? 'pill-ok'
          : d.status === 'pending' ? 'pill-pending' : 'pill-suspect';
        chip = '<span class="' + cls + '">' + esc(d.status_label || d.status || '') + '</span>';
        why = '你是<strong>作者</strong>　' + (d.reason_label ? '依据：' + esc(d.reason_label) : '未给依据')
          + (d.moderated_at ? '　<span class="muted">' + esc(String(d.moderated_at).slice(0, 16).replace('T', ' ')) + '</span>' : '');
      }
    } else if (r.err) {
      const code = r.err.status || 0;
      why = code === 403 || code === 404
        ? '<span class="muted">看不到这条的处置信息（只有作者和举报人可以查）</span>'
        : '<span class="muted">查询失败：' + esc(r.err.message || ('HTTP ' + code)) + '</span>';
    }

    return '<li class="rep-row">' +
      '<div class="rep-main">' + name + '</div>' +
      '<div class="rep-meta">' + chip + '　' + why + '</div>' +
    '</li>';
  }

  /* ---------- 已举报记录（本地）：服务端也去重，这里只是让按钮别再点 ---------- */

  function loadReported() {
    const set = new Set();
    try {
      (JSON.parse(localStorage.getItem(REPORTED_KEY) || '[]') || []).forEach((id) => set.add(id));
    } catch (e) { /* 没有 localStorage 也不算错，本次会话内仍会去重 */ }
    return set;
  }

  function saveReported() {
    try { localStorage.setItem(REPORTED_KEY, JSON.stringify(Array.from(state.reported))); } catch (e) {}
  }

  /* ---------- 合规说明 + 泄露自检（硬要求 1、3） ---------- */

  function renderCompliance(items) {
    const leaks = [];
    items.forEach((it) => {
      FORBIDDEN_FIELDS.forEach((k) => { if (it && Object.prototype.hasOwnProperty.call(it, k)) leaks.push(it.id + ' → ' + k); });
    });

    // 合规声明常驻（不因为报警而被替换掉），报警作为额外一段追加
    const fieldCheck = '<p class="field-check">本页只渲染 ' + PUBLIC_FIELDS.length + ' 个公开字段（' +
      PUBLIC_FIELDS.join(' · ') + '），实名与 IP 类字段一个都不在前台。</p>';
    const leakAlert = leaks.length
      ? '<div class="alert alert-strong"><strong>检测到不该出现在前台的字段：</strong>' +
        esc(leaks.join('、')) +
        '<span class="alert-sub">这些字段已被拒绝渲染，请立刻报告给后端。</span></div>'
      : '';

    el('compliance').innerHTML =
      '<div class="notice-box">' +
        '<strong>后台实名、前台匿名代号。</strong>' +
        '<span>发帖时要带实名 ID（后台存档，用于《民法典》通知—删除规则下定位责任主体），' +
        '但前台只显示服务端派生的匿名代号；同一个人的代号是固定的，不会每次都不一样。' +
        '实名 ID 和 IP 从不出现在任何公开响应里。</span>' +
      '</div>' + fieldCheck + leakAlert;

    if (state.reportMsg) {
      el('compliance').innerHTML += '<p class="report-msg">' + esc(state.reportMsg) + '</p>';
    }
  }

  /* ---------- 发帖表单 ---------- */

  function facetValues(list) {
    return (list || []).map((x) => (typeof x === 'string' ? x : (x && (x.value || x.name || x.label)) || ''))
      .filter(Boolean);
  }

  function suggestions() {
    const items = (state.posts.items || []);
    const uniq = (a) => Array.from(new Set(a.filter(Boolean)));
    const f = state.facets || {};
    const units = facetValues(f.units).length ? facetValues(f.units) : uniq(items.map((p) => p.unit));
    const tags = facetValues(f.tags).length ? facetValues(f.tags) : uniq(items.map((p) => p.tag));
    return { units: units, tags: tags };
  }

  function renderPostBox() {
    // 公网演示版：论坛只读（服务端也会拒绝发帖）
    if (isPublicMode()) {
      el('postbox').innerHTML =
        '<div class="pb-head">发一条</div>' +
        '<p class="pb-note">公开演示版<strong>暂不开放发帖</strong>：匿名内容无法审核，' +
        '这里只展示论坛的读、举报、审核和审计链路。本地运行项目即可发帖。</p>';
      el('postbox-wrap').hidden = false;
      return;
    }
    const s = suggestions();
    const opt = (v) => '<option value="' + esc(v) + '"></option>';
    el('postbox').innerHTML =
      '<div class="pb-head">发一条</div>' +
      '<div class="pb-grid">' +
        '<label>院系<input id="pb-unit" type="text" list="pb-units" placeholder="如 物理学院"></label>' +
        '<label>标签<input id="pb-tag" type="text" list="pb-tags" placeholder="如 推免经验"></label>' +
      '</div>' +
      '<label class="pb-wide">标题<input id="pb-title" type="text" placeholder="一句话说清你想问什么"></label>' +
      '<label class="pb-wide">正文<textarea id="pb-body" rows="3" placeholder="讲清楚背景，别人才答得上来"></textarea></label>' +
      '<datalist id="pb-units">' + s.units.map(opt).join('') + '</datalist>' +
      '<datalist id="pb-tags">' + s.tags.map(opt).join('') + '</datalist>' +
      '<div class="pb-foot">' +
        '<button type="button" class="btn-primary" id="pb-submit">发布</button>' +
        '<span class="pb-note">实名 ID <code>' + esc(currentUser()) + '</code> 只给后台存档，' +
          '展示出来的是服务端派生的匿名代号。</span>' +
      '</div>' +
      (state.submitMsg ? '<p class="pb-msg">' + esc(state.submitMsg) + '</p>' : '');

    el('pb-submit').addEventListener('click', submitPost);
    el('postbox-wrap').hidden = false;
  }

  const val = (id) => { const n = el(id); return n && n.value ? String(n.value).trim() : ''; };

  async function submitPost() {
    const payload = {
      unit: val('pb-unit'), tag: val('pb-tag'),
      title: val('pb-title'), body: val('pb-body'),
      author_real_id: currentUser()
    };
    if (!payload.title || !payload.body) {
      state.submitMsg = '标题和正文都要填。';
      renderPostBox();
      return;
    }
    try {
      if (useLive()) {
        const p = await apiPost('/api/posts', {}, payload);
        state.posts.items = [p].concat(state.posts.items || []);
        state.posts.total = (state.posts.total || 0) + 1;
        state.submitMsg = '已发布。你在前台的代号是「' + (p.author_display || '匿名') + '」。';
      } else {
        state.submitMsg = '当前是样例模式（?api=fixtures），不会真的发帖。';
      }
      renderToolbar();
      render();
      renderPostBox();
    } catch (err) {
      state.submitMsg = '发布失败：' + err.message;
      renderPostBox();
    }
  }

  /* ---------- 筛选 ---------- */

  function options(allLabel, values) {
    return ['<option value="ALL">' + esc(allLabel) + '</option>']
      .concat(values.map((v) => '<option value="' + esc(v) + '">' + esc(v) + '</option>'))
      .join('');
  }

  function renderToolbar() {
    const s = suggestions();
    el('unit').innerHTML = options('全部院系', s.units);
    el('tag').innerHTML = options('全部标签', s.tags);
    el('unit').value = state.unit;
    el('tag').value = state.tag;

    el('unit').addEventListener('change', (e) => { state.unit = e.target.value; render(); });
    el('tag').addEventListener('change', (e) => { state.tag = e.target.value; render(); });
    el('q').addEventListener('input', (e) => { state.q = e.target.value || ''; render(); });
    el('toolbar').hidden = false;
  }

  function filtered() {
    const q = state.q.trim().toLowerCase();
    return (state.posts.items || []).filter((p) => {
      if (state.unit !== 'ALL' && p.unit !== state.unit) return false;
      if (state.tag !== 'ALL' && p.tag !== state.tag) return false;
      if (!q) return true;
      return (String(p.title || '') + ' ' + String(p.body || '')).toLowerCase().indexOf(q) >= 0;
    });
  }

  /* ---------- 列表 ---------- */

  const fmtTime = (s) => String(s || '').replace('T', ' ').slice(0, 16);

  function render() {
    const items = filtered();
    const total = (state.posts.items || []).length;
    el('result-count').textContent = items.length === total
      ? '共 ' + total + ' 条'
      : items.length + ' / ' + total + ' 条';

    el('results').innerHTML = items.length
      ? items.map(postHTML).join('')
      : emptyHTML(total);
  }

  function emptyHTML(total) {
    if (!total) {
      return '<div class="state">还没有人发帖。' +
        (useLive() ? '上面那个表单可以现场发一条——发布后前台只显示服务端派生的匿名代号。' : '样例数据里是空的。') +
        '</div>';
    }
    return '<div class="state">没有符合条件的帖子，换个筛选条件。</div>';
  }

  function postHTML(p) {
    const truncated = p.body_truncated === true || p.truncated === true;
    const reported = state.reported && state.reported.has(p.id);

    const meta = [
      '<span class="pauthor">' + esc(p.author_display || '匿名') + '</span>',
      p.unit ? '<span class="sep">·</span><span>' + esc(p.unit) + '</span>' : '',
      p.tag ? '<span class="pill-tag">' + esc(p.tag) + '</span>' : '',
      p.created_at ? '<span class="sep">·</span><span class="date">' + esc(fmtTime(p.created_at)) + '</span>' : '',
      // **`reply_count` 恒为 0 时不显示。** 契约 §4 里有这个字段，但回复功能**还没做**
    // （B 说要做约 2 小时，我没排）。显示一个永远是 0 的「回复 0」比不显示更坏 ——
    // 用户会点进去找回复框，找不到。**字段留着（契约不变），只是不展示。**
    '<span class="pstat">' + (p.reply_count > 0 ? '回复 ' + esc(String(p.reply_count)) + '　' : '') + '举报 ' + esc(String(p.report_count || 0)) + '</span>'
    ].filter(Boolean).join('');

    return '<article class="pcard">' +
      '<div class="pmeta">' + meta + '</div>' +
      '<h2 class="ptitle">' + esc(p.title) + '</h2>' +
      '<p class="pbody">' + esc(p.body || '') + '</p>' +
      '<div class="pactions">' +
        '<button type="button" class="report-btn" data-id="' + esc(p.id) + '"' +
          (reported ? ' disabled' : '') + '>' + (reported ? '你已举报过' : '举报') + '</button>' +
        (truncated
          ? '<span class="phint">非订阅者只能看到前 200 字，<a href="pricing.html">订阅</a>后可看全文</span>'
          : '') +
      '</div>' +
    '</article>';
  }

  /* ---------- 举报（硬要求 2：防滥用 + 明确反馈） ---------- */

  function onResultsClick(e) {
    const btn = e.target && e.target.closest ? e.target.closest('.report-btn') : null;
    if (!btn || btn.disabled) return;
    doReport(btn.dataset.id);
  }

  async function doReport(id) {
    if (!id || (state.reported && state.reported.has(id))) return;
    if (useLive()) {
      try {
        // 服务端保证同一 reporter_id 对同一帖只计一次，重复时返回 deduped=true（不静默 +1）
        const res = await apiPost('/api/posts/' + encodeURIComponent(id) + '/report', {},
          { reason: '用户举报', reporter_id: currentUser() });
        state.reported.add(id);
        saveReported();
        state.reportMsg = '举报已提交：' + ((res && res.note) || '已记录');
        const it = (state.posts.items || []).find((p) => p.id === id);
        if (it && res && typeof res.report_count === 'number') it.report_count = res.report_count;
      } catch (err) {
        state.reportMsg = '举报失败：' + err.message;
      }
    } else {
      state.reported.add(id);
      state.reportMsg = '样例模式：只在本页标记为已举报，没有真的提交。';
    }
    renderCompliance(state.posts.items || []);
    render();
  }

  boot();
})();
