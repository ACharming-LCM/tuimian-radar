/* 今日更新页（智能体 C）
 * 数据源：data/out/changes.json（结构化，带 content_hash）+ data/out/changes.md（口径原文）
 * 三类：新增 / 有变化 / 本轮未抓到 —— 第三类的口径从 A 的报告里原文读，前端不改写。
 * 额外做一件 A 没要求但演示必需的事：识别「新增」与「未抓到」里 content_hash 相同的一批，
 * 说明那是主键变更的一次性现象，不是真的新发或丢失。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  /* 这一页没有筛选项，所以 state 只放两个东西：
   * 重拉去重标记，以及"上一份重拉失败了"的提示 —— 失败时**不换掉已有内容**。 */
  const state = { reloading: false };

  async function boot() {
    try {
      const loaded = await loadChangesData();
      const data = loaded.data;
      const md = loaded.md;
      if (!data || !data.counts) throw new Error('changes 数据里没有 counts，形状和预期不一致');

      markSource();
      renderHead(data);
      renderStats(data);
      renderAlert(data);
      renderSections(data, extractNote(md));
      renderHistory();          // 独立渲染，拿不到就不显示，绝不影响上面的主内容
      renderFreshness();        // 同上：「本轮谁没抓到」和「内容有没有变」是两件事
      renderWindows();          // 时效四档（C1 补）：数字直接来自 /api/updates、/api/library
      el('state').hidden = true;

      // 后台数据更新了 → 这一页整块重画（它没有筛选状态，但**要保住滚动位置**：
      // 用户可能正看到"哪些没抓到"那一节的第 5 所学校）。
      document.addEventListener('tuimian:data-updated', reload);
    } catch (err) {
      showError(el('state'), err);
    }
  }

  /** 重拉整页数据。**保住滚动位置**，否则用户正在读的那一行会跳走。 */
  async function reload() {
    if (state.reloading) return;
    state.reloading = true;
    const y = window.scrollY;
    try {
      const loaded = await loadChangesData();
      const data = loaded.data;
      if (!data || !data.counts) throw new Error('重拉回来的 changes 没有 counts');
      markSource();
      renderHead(data);
      renderStats(data);
      renderAlert(data);
      renderSections(data, extractNote(loaded.md));
      renderFreshness();
    } catch (e) {
      // 重拉失败不换掉已有内容：那些数据仍然有效，只是没更新上。说一句就够。
      const n = el('reload-msg');
      if (n) { n.textContent = '后台数据更新后重拉失败：' + (e && e.message ? e.message : e) + '（下面显示的还是上一份）'; n.hidden = false; }
    } finally {
      state.reloading = false;
      window.scrollTo(0, y);
    }
  }

  /* 口径文字：从 md 原文里抠出来，A 改了口径页面就跟着改 */
  function extractNote(md) {
    if (!md) return null;
    const m = String(md).match(/\*\*这不等于公告被删除\*\*[^\n]*/);
    return m ? m[0] : null;
  }

  /* mdInline 来自 common.js，全站共用一份（走查 #7：后端文案里的 ** 不能在页面上漏出来） */

  /* ---------- 头部 ---------- */

  function renderHead(data) {
    el('generated').textContent = '本轮生成时间：' + (data.generated_at || '未知');
  }

  function renderStats(data) {
    const c = data.counts;
    const cells = [
      { n: c.previous, l: '上次', cls: 'dim' },
      { n: c.current, l: '本次', cls: '' },
      { n: c.added, l: '新增', cls: 'hi' },
      { n: c.changed, l: '有变化', cls: 'hi' },
      { n: c.reidentified, l: 'id 变更（不计入新增）', cls: 'dim' },
      { n: c.reparsed, l: '重新解析（我们改了抓取）', cls: 'dim' },
      { n: c.removed, l: '本轮未抓到', cls: 'dim' },
      { n: c.unchanged, l: '未变', cls: 'dim' }
    ].filter((x) => typeof x.n === 'number');

    el('stats').innerHTML = cells
      .map((x) => '<div class="stat ' + x.cls + '"><div class="n">' + x.n + '</div><div class="l">' + esc(x.l) + '</div></div>')
      .join('');
    el('stats-wrap').hidden = false;
  }

  /* ---------- 主键变更检测（演示必需，不然"新增 30 条"是假象） ---------- */

  function churnPairs(data) {
    const addByHash = new Map();
    (data.added || []).forEach((r) => { if (r.content_hash) addByHash.set(r.content_hash, r); });
    return (data.removed || []).filter((r) => r.content_hash && addByHash.has(r.content_hash));
  }

  function renderAlert(data) {
    const pairs = churnPairs(data);
    if (!pairs.length) { el('alert-wrap').innerHTML = ''; return; }
    el('alert-wrap').innerHTML =
      '<div class="alert">' +
        '这一轮的「新增」里有 <strong>' + pairs.length + ' 条</strong>与「本轮未抓到」的记录 ' +
        '<code>content_hash</code> 完全一致——同一篇公告、同一个正文指纹，只是主键变了。' +
        '<strong>这不是真的新发或丢失，是主键算法调整后的一次性现象。</strong>' +
        '下轮抓取后这两类应当同时归零。' +
      '</div>';
  }

  /* ---------- 三类 ---------- */

  function renderSections(data, note) {
    const c = data.counts;
    const html = [];

    html.push(section('added', '新增公告', (data.added || []).length,
      null,
      (data.added || []).length
        ? (data.added || []).map(noticeItemHTML).join('')
        : emptyNote('本次没有新增公告。如果你在等某个学校的通知，说明它还没发。')));

    html.push(section('changed', '有变化的公告', (data.changed || []).length,
      '高校公告被静默修改是常态：改截止日、补附件、延期。这一节是订阅真正卖的东西。',
      (data.changed || []).length
        ? (data.changed || []).map(changedItemHTML).join('')
        : emptyNote('本次没有检测到公告被修改——这一轮没有变化，不代表以后不会有。')));

    // A 的修法：id 对不上时再用「内容身份」配一次，命中归到这一类。
    // 措辞照 diff.py 里的原话，不在前端改写。
    if ((data.reidentified || []).length) {
      html.push(section('reidentified', 'id 变更（内容未变）', data.reidentified.length,
        '同一条公告的 <code>id</code> 变了，但标题、日期、正文哈希都一致 —— ' +
        '<strong>这通常来自抓取算法调整，不是新公告、也不是公告消失。</strong>',
        data.reidentified.map(reidentifiedItemHTML).join('')));
    }

    // A 的规矩（CHANGELOG + api.md 第 10 节）：reparsed **不是**「通知被修改」——
    // 正文一个字没变，是我们改了抽取代码。所以单独一栏、措辞必须区分开，也不计入「有变化」。
    if ((data.reparsed || []).length) {
      html.push(section('reparsed', '重新解析（内容未变）', data.reparsed.length,
        '这些公告的<strong>正文和附件一个字都没变</strong>，是我们<strong>改进了抓取</strong>、把字段抽得更全了 —— ' +
        '<strong>不是学校改了通知</strong>，所以不计入「有变化」。',
        data.reparsed.map(reparsedItemHTML).join('')));
    }

    if (c.removed) {
      html.push(section('removed', '本轮未抓到的公告', c.removed,
        note ? mdInline(note)
             : mdInline('**这不等于公告被删除**，更可能是翻页、栏目改版或本轮抓取失败。不要据此判断通知撤销。'),
        (data.removed || []).map(removedItemHTML).join('')));
    }

    el('results').innerHTML = html.join('');
  }

  function section(cls, title, count, noteHTML, bodyHTML) {
    return '<section class="section ' + cls + '">' +
      '<div class="section-head">' +
        '<span class="section-title">' + esc(title) + '</span>' +
        '<span class="section-count">' + count + ' 条</span>' +
      '</div>' +
      (noteHTML ? '<p class="section-note">' + noteHTML + '</p>' : '') +
      bodyHTML +
    '</section>';
  }

  const emptyNote = (t) => '<p class="empty-note">' + esc(t) + '</p>';

  /* 新增条目：changes.json 里是完整记录，所以附件、疑似、招生方式都能照常标 */
  function noticeItemHTML(r) {
    const url = safeUrl(r.url);
    const title = url
      ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(r.title) + '</a>'
      : esc(r.title);

    const flags = [];
    if (r.apply_mode) flags.push('<span class="flag">' + esc(r.apply_mode) + '</span>');
    if (r.deadline) flags.push('<span class="flag flag-dl">截止 ' + esc(r.deadline) + '</span>');
    const atts = (r.attachments || []).length;
    if (atts) flags.push('<span class="flag flag-att">附件 ' + atts + '</span>');
    const conf = r.extract && r.extract.confidence;
    if (typeof conf === 'number' && conf < 0.5) flags.push('<span class="flag flag-sus">疑似</span>');

    return '<article class="notice-item">' +
      '<div class="notice-head">' +
        '<span class="notice-school">[' + esc(r.school) + ' · ' + esc(r.unit) + ']</span>' +
        '<span class="notice-title">' + title + '</span>' +
      '</div>' +
      '<div class="notice-meta">' +
        '<span>' + esc(r.publish_date || '日期未知') + '</span>' + flags.join('') +
      '</div>' +
    '</article>';
  }

  /* 有变化：字段级 diff，只显示真正变了的字段 */
  function changedItemHTML(ch) {
    const r = ch.record || {};
    const fields = Object.entries(ch.fields || {}).map(([k, pair]) => fieldChangeHTML(k, pair)).join('');

    const bodyRow = (ch.body_changed && !(ch.fields && ch.fields.body_text))
      ? '<div class="diff-row"><span class="diff-key">正文</span>' +
        '<span class="diff-new">正文内容变了，全文改动需人工核对</span></div>'
      : '';

    const url = safeUrl(r.url);
    const link = url
      ? '<a class="diff-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">看原文 ↗</a>'
      : '';

    return '<article class="diff-item">' +
      '<div class="notice-head">' +
        '<span class="notice-school">[' + esc(r.school) + ' · ' + esc(r.unit) + ']</span>' +
        '<span class="notice-title">' + esc(r.title) + '</span>' +
      '</div>' +
      '<div class="diff-fields">' + fields + bodyRow + '</div>' +
      (link ? '<div class="diff-foot">' + link + '</div>' : '') +
    '</article>';
  }

  function fmtVal(v) {
    if (v === null || v === undefined) return '（空）';
    if (typeof v === 'string' && v.length > 90) return v.slice(0, 90) + '…';
    return String(v);
  }

  function removedItemHTML(r) {
    return '<article class="notice-item">' +
      '<div class="notice-head">' +
        '<span class="notice-school">[' + esc(r.school) + ' · ' + esc(r.unit) + ']</span>' +
        '<span class="notice-title">' + esc(r.title) + '</span>' +
      '</div>' +
    '</article>';
  }

  /* 重新解析：文件版把完整记录嵌在 record 里，接口版（契约第 10 节）是扁的 —— 两种都吃 */
  function reparsedMeta(x) {
    const r = x && x.record ? x.record : (x || {});
    return {
      title: r.title || '', school: r.school || '', unit: r.unit || '', url: r.url || null,
      fields: (x && x.fields) || {}
    };
  }

  /* 字段变化有**两种形状**：常见的是 [旧值, 新值] 数组；
   * 但也有些字段给的是一张小结构（例如 attachments: {old_count, new_count, _detail}）——
   * 当成数组渲染会变成 [object Object]，所以这里分开处理。 */
  function fieldChangeHTML(k, pair) {
    const key = '<span class="diff-key">' + esc(k) + '</span>';
    if (Array.isArray(pair)) {
      return '<div class="diff-row">' + key +
        '<span class="diff-old">' + esc(fmtVal(pair[0])) + '</span>' +
        '<span class="diff-arrow">→</span>' +
        '<span class="diff-new">' + esc(fmtVal(pair[1])) + '</span></div>';
    }
    if (pair && typeof pair === 'object') {
      const detail = pair._detail ? String(pair._detail) : '';
      const text = Object.keys(pair)
        .filter((x) => x !== '_detail')
        .map((x) => x.replace(/^old_/, '原 ').replace(/^new_/, '新 ') + ' ' + fmtVal(pair[x]))
        .join('　');
      return '<div class="diff-row">' + key +
        '<span class="diff-new">' + esc(text) + '</span>' +
        (detail ? '<span class="diff-old">' + esc(detail) + '</span>' : '') + '</div>';
    }
    return '';
  }

  function reparsedItemHTML(x) {
    const m = reparsedMeta(x);
    const rows = Object.keys(m.fields).map((k) => fieldChangeHTML(k, m.fields[k])).join('');
    const url = safeUrl(m.url);
    const why = x && x.why ? String(x.why) : '';
    return '<article class="diff-item diff-reparsed">' +
      '<div class="notice-head">' +
        '<span class="notice-school">[' + esc(m.school) + ' · ' + esc(m.unit) + ']</span>' +
        '<span class="notice-title">' + esc(m.title) + '</span>' +
      '</div>' +
      (why ? '<div class="diff-why">归因：' + esc(why) + '</div>' : '') +
      (rows ? '<div class="diff-fields">' + rows + '</div>' : '') +
      (url ? '<div class="diff-foot"><a class="diff-link" href="' + esc(url) +
        '" target="_blank" rel="noopener noreferrer">看原文 ↗</a></div>' : '') +
    '</article>';
  }

  function reidentifiedItemHTML(x) {    const r = x.record || {};
    return '<article class="notice-item">' +
      '<div class="notice-head">' +
        '<span class="notice-school">[' + esc(r.school) + ' · ' + esc(r.unit) + ']</span>' +
        '<span class="notice-title">' + esc(r.title) + '</span>' +
      '</div>' +
      '<div class="notice-meta"><span class="idpair"><code>' + esc(x.old_id) + '</code> → <code>' +
        esc(x.new_id) + '</code></span></div>' +
    '</article>';
  }

  /* ---------- 这一轮谁没抓到 / 谁没解析出来 ----------
   *
   * 这一页的三类（新增 / 有变化 / 本轮未抓到）讲的是「内容有没有变」，用的是**公告级**的 diff；
   * 而「这所学校这一轮压根没抓到」是**站点级**的事实，diff 里看不见 ——
   * 一所学校整轮失败，它在「本轮未抓到」里可能只有 0 条，看起来像「它没动静」。
   * 所以单独一块，措辞用契约第 12 节的表：失败 / 没解析出内容 / 陈旧，三样都不叫「没有更新」。 */
  async function renderFreshness() {
    const node = el('fresh-wrap');
    if (!node) return;
    try {
      const fresh = await getFreshness();
      if (!fresh) { node.hidden = true; return; }
      // 顶部那一行（「数据更新于 N 分钟前 · 覆盖 N 所学校」）和「哪些没抓到」是**两层信息**：
      // 前者是全局的、会自己走动（live.js 负责），后者是逐校的清单。
      // 所以这一页也有一个 `#freshness` 宿主，用的是 common.js 同一个渲染器。
      const bar = el('freshness');
      if (bar) { bar.innerHTML = freshnessBarHTML(fresh); bar.hidden = !bar.innerHTML; }
      const html = freshnessIssuesHTML(fresh);
      if (!html) {
        // 没有异常也要说一句 —— 「全都抓到了」本身是信息，不是空白
        //
        // ⚠️ `fresh.schools.length` 是**站点数**，不是学校数：一所学校常有多个栏目，
        // 实测 37 个条目只对应 29 所学校。原来这里用 37 说「37 所学校全部抓到了」，
        // 和 DATA-NUMBERS §一 那条「别说覆盖 37 所学校」是同一个错。
        const sites = (typeof fresh.site_count === 'number') ? fresh.site_count : fresh.schools.length;
        const schools = (typeof fresh.school_count === 'number') ? fresh.school_count : sites;
        node.innerHTML = '<div class="panel-head"><h2 class="panel-title">抓取情况</h2>' +
          '<span class="hist-range">' + sites + ' 个站点 / ' + schools + ' 所学校</span></div>' +
          '<p class="section-note">这一轮 <strong>' + sites + ' 个站点全部抓到了</strong>（' + schools + ' 所学校），没有失败、也没有解析不出内容的站点' +
          '（数据更新于 ' + esc(fresh.generated_age_text || ageHoursText(fresh.generated_age_hours) || '未知') + '）。</p>';
        node.hidden = false;
        return;
      }
      node.innerHTML = html;
      node.hidden = false;
    } catch (e) {
      // 附加信息坏了不能连累主内容：整块不显示，控制台留一行给排查用。
      // （这是 async 函数，不接住就会变成一条 unhandled rejection —— 量具会把它算成页面异常。）
      node.hidden = true;
      if (window.console) console.warn('[freshness] 读取失败，已隐藏这一块：', e && e.message);
    }
  }

  /* ---------- 时效四档（C1 补，2026-10-18）----------
   *
   * 数字**全部来自接口**：`/api/updates?days=N` 与 `/api/library?days=N`。
   * 契约 §20.1 的恒等式：`updates.total + library.total == 事件总数`
   * （实测 days=1/7/30 三个窗口都成立：0+1131、27+1104、112+1019）。
   *
   * ⚠️ **这里数的是"事件"，不是"公告"**：一条公告可能产生多条事件。
   *    公告列表页的四档是按**公告**算的，两边的数不一样是正常的 —— **别去"修"它**。
   *    这也是我把四档放在这一页、而不是硬塞进公告页的原因：
   *    公告页只有 50 条/页，拿它的窗口计数冒充全量就是假数。
   *
   * ⚠️ 两个接口必须传**同一个** `days`。一边 30 天一边 7 天，8~30 天那批会同时出现在两边
   *    （契约里实测差 85 条）。所以下面每个窗口都是成对取的。
   */
  const WINDOW_DEFS = [
    { days: 3, label: '最近 3 天' },     // C7：原来第一档是「今天」（≤1 天），几乎永远是 0
    { days: 7, label: '本周' },
    { days: 30, label: '本月' }
  ];

  async function renderWindows() {
    const wrap = el('window-wrap');
    const host = el('window-filters');
    const ident = el('window-identity');
    const note = el('window-note');
    if (!wrap || !host) return;
    try {
      const pair = await Promise.all(WINDOW_DEFS.map((w) => Promise.all([
        apiGet('/api/updates', { days: w.days, size: 1 }),
        apiGet('/api/library', { days: w.days, size: 1 })
      ])));
      const rows = pair.map(([u, l], i) => ({
        days: WINDOW_DEFS[i].days,
        label: WINDOW_DEFS[i].label,
        updates: (u && typeof u.total === 'number') ? u.total : null,
        library: (l && typeof l.total === 'number') ? l.total : null
      }));
      if (rows.some((r) => r.updates === null || r.library === null)) { wrap.hidden = true; return; }

      const total = rows[0].updates + rows[0].library;      // = 事件全集
      const sel = (state.winSel && rows.some((r) => r.days === state.winSel)) ? state.winSel : 30;
      state.winSel = sel;
      const cur = rows.find((r) => r.days === sel);

      host.innerHTML = '<div class="tier-row" role="group" aria-label="按发布时间看事件流">' +
        '<span class="tier-label">窗口</span>' +
        rows.map((r) =>
          '<button type="button" class="fchip" data-days="' + r.days + '" aria-pressed="' +
          (r.days === sel) + '">' + esc(r.label) + '<span class="c">' + r.updates + '</span></button>'
        ).join('') +
        '<span class="tier-label" style="margin-left:8px">最近一天 / 本周 / 本月窗口内的「值得看的新公告」条数</span>' +
        '</div>';

      ident.textContent = '事件全集 ' + total + ' 条';
      note.innerHTML =
        '<strong>' + esc(cur.label) + '窗口（' + cur.days + ' 天）</strong>：' +
        '值得看的新公告 <strong>' + cur.updates + '</strong> 条 ＋ ' +
        '资料库 <strong>' + cur.library + '</strong> 条 ＝ 事件全集 <strong>' + total + '</strong> 条。' +
        '　窗口内 = 发布日 ≤ ' + cur.days + ' 天、未过期、非 junk；资料库是它的补集（不是"另外查一遍"）。' +
        '<br><span class="muted">这里数的是<strong>事件</strong>，公告列表页数的是<strong>公告</strong>——' +
        '一条公告可能产生多条事件，两边数字不同是正常的。</span>';

      // 委托一次就够：容器里的 innerHTML 会重画，监听挂在外层不会丢。
      if (!host.dataset.wired) {
        host.dataset.wired = '1';
        host.addEventListener('click', (e) => {
          const b = e.target.closest('.fchip');
          if (!b || !b.dataset.days) return;
          state.winSel = Number(b.dataset.days);
          renderWindows();
        });
      }
      wrap.hidden = false;
    } catch (e) {
      // 附加信息：接口挂了就把整块收起来，不编数字、也不连累上面的「今日更新」主内容。
      wrap.hidden = true;
      if (window.console) console.warn('[windows] 读取失败，已隐藏这一块：', e && e.message);
    }
  }

  /* ---------- 过去 N 天：每天更新的凭据，也是月度订阅的素材 ----------
   * GET /api/changes/history 目前还是 404（B 未实现），所以这里做**渐进增强**：
   * 接口通了就渲染，没通就整块不显示 —— 不编数字、也不让页面报错。
   * 形状按 A 的历史归档（data/out/history/index.json）：{ days: [{date, records, added, changed, removed, reidentified}] }，
   * 这是 A 给的（"返回 index.json 的形状就行"），不是我猜的。 */
  async function renderHistory() {
    const node = el('history-wrap');
    if (!node) return;
    if (!useLive()) { node.hidden = true; return; }

    let data = null;
    try {
      data = await apiGet('/api/changes/history');
    } catch (err) {
      node.hidden = true;     // 接口还没上线：安静地不显示，主内容不受影响
      return;
    }
    const days = (data && (data.days || data.items)) || [];
    if (!days.length) { node.hidden = true; return; }

    const sum = (k) => days.reduce((n, d) => n + (typeof d[k] === 'number' ? d[k] : 0), 0);
    const first = String((days[0] || {}).date || '');
    const last = String((days[days.length - 1] || {}).date || '');

    node.innerHTML =
      '<div class="panel-head">' +
        '<h2 class="panel-title">过去 ' + days.length + ' 天的更新记录</h2>' +
        '<span class="hist-range">' + esc(first) + ' → ' + esc(last) +
          (typeof data.days_with_changes === 'number' ? '　其中 ' + data.days_with_changes + ' 天有变化' : '') +
        '</span>' +
      '</div>' +
      '<div class="stats">' +
        '<div class="stat hi"><div class="n">' + sum('added') + '</div><div class="l">累计新增</div></div>' +
        '<div class="stat"><div class="n">' + sum('changed') + '</div><div class="l">累计有变化</div></div>' +
        '<div class="stat dim"><div class="n">' + sum('removed') + '</div><div class="l">累计未抓到</div></div>' +
        '<div class="stat dim"><div class="n">' + sum('reidentified') + '</div><div class="l">累计 id 变更</div></div>' +
      '</div>' +
      '<ul class="hist-list">' + days.slice().reverse().map((d) =>
        '<li><span class="hist-date">' + esc(d.date || '') + '</span>' +
        '<span class="hist-num">抓到 ' + esc(String(d.records == null ? '—' : d.records)) + ' 条</span>' +
        '<span class="hist-delta">新增 ' + esc(String(d.added || 0)) +
          '　变化 ' + esc(String(d.changed || 0)) +
          '　未抓到 ' + esc(String(d.removed || 0)) + '</span></li>').join('') +
      '</ul>' +
      '<p class="section-note">这是「每天更新」的凭据：<strong>不是"我们的脚本能跑"，而是"这是过去这些天每天的增删改"</strong>。</p>';
    node.hidden = false;
  }

  boot();
})();
