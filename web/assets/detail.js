/* 公告详情页（智能体 C）
 * 数据源：contracts/fixtures/notices.detail.json（GET /api/notices/{id}）+ notices.list.json
 * fixture 里只有一条正文样例，所以：
 *   ?id= 缺省或等于样例 id → 显示完整详情；
 *   ?id= 是别的公告 → 显示该条元信息 + 明确说明"正文样例只有一条"，不假装有正文；
 *   查不到 → 给出明确空态，不白屏。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const KIND = { file: '文件', pdf_embed: '内嵌 PDF', iframe: '内嵌页面', image: '图片' };

  async function boot() {
    const id = new URLSearchParams(location.search).get('id');
    try {
      if (useLive() && id) {
        // 真接口：GET /api/notices/{id}?user=xxx
        // **402 必须在这里分流**：付费墙的响应体里没有 body_text，
        // 把它当成"这条公告没有正文"就是拿数据缺失掩盖付费墙（api.md 第 5 节明令禁止）。
        try {
          const got = await fetchDetail(id);
          dataSource = 'live';
          markSource();
          renderFull(got.rec, got.quota);
          el('state').hidden = true;
          return;
        } catch (err) {
          if (err.status === 402) {
            dataSource = 'live';
            markSource();
            renderQuota(err);
            el('state').hidden = true;
            return;
          }
          if (err.status === 404) {
            dataSource = 'live';
            markSource();
            renderNotFound(id);
            el('state').hidden = true;
            return;
          }
          lastLiveError = err;
          // 4xx 是接口的答复（400/403/410…），不是"接口没通"，不能拿样例顶上
          if (isClientError(err) || !ALLOW_FALLBACK) throw err;
        }
      }

      // 样例回退（或 ?api=fixtures 指定）
      const [detail, list] = await Promise.all([
        loadFixture('notices.detail.json'),
        loadFixture('notices.list.json').catch(() => null)
      ]);
      dataSource = 'fixtures';
      markSource();

      const listItem = (id && list) ? (list.items || []).find((r) => r.id === id) : null;
      if (!id || id === detail.id) {
        renderFull(detail, null);
      } else if (listItem) {
        renderStub(listItem, detail);
      } else {
        renderNotFound(id);
      }
      el('state').hidden = true;
    } catch (err) {
      showError(el('state'), err);
    }
  }

  /* 自己 fetch 而不是用 apiGet：这一页要用响应头里的额度信息（X-Quota-*）。
   * 额度走 header 不走 body，是 B 为了保住"响应键集合 == fixture 键集合"这条硬约束。 */
  async function fetchDetail(id) {
    const path = '/api/notices/' + encodeURIComponent(id);
    // `meta=1`：多要一份 `field_state` + `deadline_display`（契约 §14.5）。
    // 不加的话，`deadline` 为 null 时页面**只有两种说法**（有日期 / 没有），
    // 而契约要求三种（读到了 / 确实没有 / 我们没读到）。第三种的差别是"要不要去看原文"。
    const r = await fetchWithTimeout(path + qs(userQuery({ meta: 1 })), {}, FETCH_TIMEOUT_MS, '/api/notices/{id}');
    const body = await r.json().catch(() => null);
    const h = r.headers;
    const get = (k) => (h && h.get ? h.get(k) : null);
    const quota = { limit: get('X-Quota-Limit'), used: get('X-Quota-Used'), remaining: get('X-Quota-Remaining') };
    if (!r.ok) {
      const err = new Error((body && body.error && body.error.message) || ('HTTP ' + r.status));
      err.status = r.status;
      err.payload = body;
      err.quota = quota;
      err.path = '/api/notices/{id}';
      throw err;
    }
    return { rec: body, quota: quota };
  }

  function quotaHTML(q) {
    if (!q || !q.remaining) return '';
    return '<p class="quota-line"><strong>详情页</strong>免费额度：每天 ' + esc(q.limit || '3') +
      ' 条，今天还剩 <strong>' + esc(q.remaining) + '</strong> 条。' +
      '它和推送的免费条数不是同一个额度；公告列表和匹配都不受限。</p>';
  }

  /* 走查 #2：后端给的 `upgrade_hint` 是**对象**，之前当字符串拼，页面上渲染出一句
   * `[object Object]`。第一版我改成"把所有值拼起来"，结果更糟 —— 把 plan_id、价格、
   * features、endpoint 全糊成一段。现在只取一句像人话的；拿不准就不显示：
   * 页面上本来就有付费墙主文案和「去订阅」按钮，不需要一段机器拼出来的话。 */
  function hintText(v) {
    if (typeof v === 'string') return v.trim();
    if (v && typeof v === 'object') {
      const prefer = ['message', 'note', 'text', 'hint', 'title', 'desc', 'description'];
      for (let i = 0; i < prefer.length; i++) {
        const x = v[prefer[i]];
        if (typeof x === 'string' && x.trim()) return x.trim();
      }
    }
    return '';
  }

  /* 402：这是墙，不是数据缺失。把话说清楚，别让用户以为公告没正文。 */
  function renderQuota(err) {
    const hint = hintText(err.payload && err.payload.upgrade_hint);
    const q = err.quota || {};
    el('results').innerHTML =
      '<div class="paywall">' +
        '<div class="pw-badge">付费墙</div>' +
        '<h2 class="pw-title">今天的免费额度用完了</h2>' +
        '<p class="pw-line">这不是「这条公告没有正文」，而是<strong>免费身份每天只能看 ' +
          esc(q.limit || '3') + ' 条公告详情</strong>，今天的已经用完了。' +
          '公告列表、匹配结果、导师页都不受这个限制。</p>' +
        (q.used ? '<p class="pw-line">今天已用 ' + esc(q.used) + ' 条，剩余 0 条。</p>' : '') +
        (hint ? '<p class="pw-hint">' + esc(hint) + '</p>' : '') +
        '<div class="dactions">' +
          '<a class="btn-primary" href="pricing.html">去订阅（演示用模拟支付）</a>' +
          '<a class="btn-ghost" href="notices.html">← 回公告列表</a>' +
        '</div>' +
        '<p class="pw-foot">演示提示：把身份切到订阅用户就不会看到这一页。当前身份 <code>' +
          esc(currentUser()) + '</code>。</p>' +
      '</div>';
  }

  /* ---------- 「下一步」（C14） ----------
   *
   * 详情页最容易犯的错：正文读完了，人却不知道下一步做什么，于是关掉页面。
   * 所以这里把"下一步"做成**三件带按钮的事**，而且第一句会随截止日状态改口 ——
   * 「还剩 3 天」和「已于 10-10 截止」要导出的动作本来就不一样。
   *
   * ⚠️ 句子里的天数走 `deadlineDaysLeft()`（deadline.js 唯一实现），
   * 不在这里再算一遍 —— 两处各算一次，迟早一处对一处错。
   */
  function dnextHTML(rec) {
    const dd = deadlineDisplay(rec);
    const left = deadlineDaysLeft(rec);
    const day = dd.state === 'found' ? String(dd.text || '').slice(0, 10) : '';

    let lead;
    if (dd.state === 'found' && left !== null && left < 0) {
      lead = '这条已经截止（' + day + '）。仍值得点开原文看一眼：学校延期或补发附件时，正文不会改，只会在原页面更新。';
    } else if (dd.state === 'found' && left === 0) {
      lead = '这条<strong>今天截止</strong>。材料通常要盖章或签字，先确认还来不来得及。';
    } else if (dd.state === 'found' && left !== null && left > 0) {
      lead = '这条<strong>还剩 ' + left + ' 天</strong>（' + day + ' 截止）。先对着原文把材料清单核一遍——附件常常比正文更关键。';
    } else if (dd.state === 'absent') {
      lead = '这条正文里没写截止日。有的学院只在宣讲会或群里说，原文里搜「截止 / 报名 / 时间」三个词。';
    } else {
      lead = '我们没读到这条的截止日（可能在图片或附件里）。请点原文核对，别按「它没写截止日」来安排时间。';
    }

    const url = safeUrl(rec.url);
    const school = rec.school || '';
    const unit = rec.unit || '';
    const sameHref = 'notices.html' + qs({
      school: school || undefined,
      unit: unit || undefined
    });
    const watchLabel = (school || '这个学院') + (unit ? ' · ' + unit : '');

    const items = [
      '<li class="dnext-item"><span class="dnext-no">1</span>' +
        '<span class="dnext-text"><strong>核对原文</strong>：材料要求、邮寄/网申口径、是否要导师推荐。' +
          (url ? '' : '<span class="muted">（这条没有原文链接，属于数据缺陷）</span>') + '</span>' +
        (url ? '<a class="btn-primary btn-mini" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">看原文 ↗</a>' : '') +
      '</li>',
      '<li class="dnext-item"><span class="dnext-no">2</span>' +
        '<span class="dnext-text"><strong>关注 ' + esc(watchLabel) + '</strong>：以后这个学院的新公告直接出现在通知里，不用每天翻列表。</span>' +
        '<button type="button" class="btn-ghost btn-mini" id="dnext-watch" data-school="' + esc(school) +
          '" data-unit="' + esc(unit) + '">读关注状态…</button>' +
      '</li>',
      '<li class="dnext-item"><span class="dnext-no">3</span>' +
        '<span class="dnext-text"><strong>看同一学院的其它公告</strong>：判断这条是常规操作还是特例（名额、批次常常在别的通知里）。</span>' +
        '<a class="btn-ghost btn-mini" href="' + esc(sameHref) + '">同类公告</a>' +
      '</li>'
    ].join('');

    return '<section class="dnext">' +
      '<div class="dnext-h"><h2>下一步</h2></div>' +
      '<p class="dnext-lead">' + lead + '</p>' +
      '<ul class="dnext-list">' + items + '</ul>' +
    '</section>';
  }

  /* 关注按钮：**先问服务端有没有这条规则，再决定按钮是"关注"还是"已关注"**。
   *
   * 三种结果分开说，不许糊成一句：
   *   · 有同校同院的规则        → 已关注
   *   · 只有同校（没限学院）    → 已关注（整校）—— 这种规则其实也覆盖这条公告
   *   · 读不到（/api/watch 没通）→ 明说读不到
   * 第三种的诱惑是默认成"未关注"，那会让用户重复点、以为关注失灵。
   */
  function sameText(a, b) {
    const n = (v) => String(v == null ? '' : v).trim().toLowerCase();
    return n(a) === n(b);
  }

  async function hydrateWatch(rec) {
    const btn = el('dnext-watch');
    if (!btn) return;
    const school = rec.school || '';
    const unit = rec.unit || '';
    if (!school) { btn.textContent = '这条没有学校字段，不能关注'; btn.className = 'btn-ghost btn-mini is-dead'; return; }

    let watches = null;
    try {
      const d = await apiGet('/api/watch', userQuery());
      watches = (d && d.watches) || [];
    } catch (e) {
      btn.className = 'btn-ghost btn-mini is-dead';
      btn.textContent = '关注状态读不到（/api/watch 没通）';
      btn.setAttribute('title', String((e && e.message) || e));
      return;
    }

    const exact = watches.find((w) => sameText(w.school, school) && sameText(w.unit, unit));
    const schoolOnly = watches.find((w) => sameText(w.school, school) && !w.unit);
    const hit = exact || schoolOnly;
    if (hit) {
      btn.className = 'btn-ghost btn-mini is-on';
      btn.innerHTML = (exact ? '✓ 已关注这个学院' : '✓ 已关注 ' + esc(school) + '（整校）') +
        ' <a href="subscriptions.html" class="dnext-manage">管理</a>';
      btn.setAttribute('title', '关注规则存在服务端（' + hit.id + '）');
      return;
    }

    wireWatchButton(btn, school, unit);
  }

  function wireWatchButton(btn, school, unit) {
    btn.className = 'btn-primary btn-mini';
    btn.textContent = '关注' + (unit ? '这个学院' : '这个学校');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = '关注中…';
      try {
        // **body 里叫 user_id，查询参数里叫 user** —— 这两个名字不一样，写错会静默落到 u_demo
        const r = await apiPost('/api/watch', {}, { user_id: currentUser(), school: school, unit: unit || undefined });
        const created = r && r.created;
        btn.className = 'btn-ghost btn-mini is-on';
        btn.innerHTML = (created ? '✓ 已关注（新建）' : '✓ 已经关注过了') +
          ' <a href="subscriptions.html" class="dnext-manage">管理</a>';
      } catch (e) {
        btn.disabled = false;
        btn.className = 'btn-ghost btn-mini is-warn';
        btn.textContent = '关注失败：' + ((e && e.message) || e);
        btn.setAttribute('title', '失败后按钮保持可点，重试一次不会产生重复规则（幂等）');
      }
    });
  }

  /* ---------- 完整详情 ---------- */

  function renderFull(rec, quota) {
    const conf = rec.extract && typeof rec.extract.confidence === 'number' ? rec.extract.confidence : null;

    const meta = [
      '<span class="school">' + esc(rec.school) + '</span>',
      rec.unit ? '<span class="sep">·</span><span>' + esc(rec.unit) + '</span>' : '',
      rec.board ? '<span class="sep">·</span><span>' + esc(rec.board) + '</span>' : '',
      rec.source ? '<span class="sep">·</span><span>' + esc(rec.source) + '</span>' : '',
      rec.publish_date ? '<span class="sep">·</span><span class="date">' + esc(rec.publish_date) + ' 发布</span>' : '',
      // 三态：直接用服务端拼好的 `deadline_display`（契约 §14.5 明说「不要自己拼」）。
      // 原来这里是二分支，`deadline` 为 null 一律写「公告未明确截止日期」——
      // 那是把「我们没读到」说成了「确实没有」，正好是契约 ⛔ 的那一条。
      deadlineLineHTML(rec),
      rec.apply_mode ? '<span class="pill-mode">' + esc(rec.apply_mode) + '</span>' : '',
      conf !== null && conf < 0.5 ? '<span class="pill-suspect">疑似</span>' : ''
    ].filter(Boolean).join('');

    el('results').innerHTML =
      '<article class="dhead">' +
        '<h1 class="dtitle">' + esc(rec.title) + '</h1>' +
        '<div class="dmeta">' + meta + '</div>' +
        quotaHTML(quota) +
        dnextHTML(rec) +
        '<div class="dactions">' +
          // 「看原文」不再放这儿：它已经是「下一步」的第 1 件事，
          // 同一屏两个一模一样的主按钮，用户会以为哪个是"更正式的"。
          '<a class="btn-ghost" href="letter.html?notice_id=' + encodeURIComponent(rec.id) + '">生成套磁信（模板样例）</a>' +
          '<a class="btn-ghost" href="notices.html">← 返回列表</a>' +
        '</div>' +
      '</article>' +

      '<section class="dblock">' +
        '<h2 class="panel-title">正文' + (rec.body_len ? '（' + rec.body_len + ' 字）' : '') + '</h2>' +
        '<div class="dbody">' + bodyHTML(rec.body_text) + '</div>' +
      '</section>' +

      '<section class="dblock">' +
        '<h2 class="panel-title">附件' + ((rec.attachments || []).length ? '（' + rec.attachments.length + ' 个）' : '') + '</h2>' +
        attachListHTML(rec.attachments) +
      '</section>' +

      '<section class="dblock">' +
        '<h2 class="panel-title">字段抽取信息</h2>' +
        '<div class="kvlist">' + extractRows(rec) + '</div>' +
      '</section>';

    // 关注按钮的状态要问服务端（**不阻塞正文渲染**：正文已经在屏幕上了）
    hydrateWatch(rec);
  }

  /* 正文可能带【附件正文：文件名】标记（CHANGELOG 里记过），按标记分段，别混成一坨。
   * 「首行重复标题」A 已经改到抓取侧（detail.py 的 _strip_leading_title），前端不再自己处理 ——
   * 放在抓取侧是因为匹配打分、变更检测、套磁信引文都读 body_text，清一次下游全受益。 */
  function bodyHTML(text) {
    const raw = String(text == null ? '' : text).trim();
    if (!raw) {
      return '<p class="muted">这条没抽到正文。可能是扫描版 PDF 或需要登录/异步加载的页面——点上面的「看原文」确认。</p>';
    }
    const parts = raw.split(/【附件正文：([^】]*)】/);
    let out = segHTML(parts[0]);
    for (let i = 1; i < parts.length; i += 2) {
      out += '<div class="attach-seg">' +
        '<div class="attach-seg-title">以下正文来自附件：' + esc(parts[i] || '未命名') + '</div>' +
        segHTML(parts[i + 1] || '') +
      '</div>';
    }
    return out || '<p class="muted">正文为空。</p>';
  }

  /* 走查 #1：以前是"每个换行都分段"，于是一段被拍平的 PDF 表格就渲染成一列竖排的词
   * （序号 / 学院 / 姓名 / 学号 / …），根本读不了。现在改成：
   *   1) 只按**空行**分段；
   *   2) 段内换行用 <br> 保留；
   *   3) 如果一段里全是短行（典型就是被拍平的表格），整段走 <pre> 原样呈现 ——
   *      表格的可读性本来就靠原始换行，重新排版只会更乱。 */
  function segHTML(t) {
    const s = String(t == null ? '' : t).trim();
    if (!s) return '';
    return s.split(/\n\s*\n+/).map((block) => {
      const lines = block.split('\n').map((l) => l.trim()).filter(Boolean);
      if (!lines.length) return '';
      const short = lines.filter((l) => l.length <= 4).length;
      if (lines.length >= 6 && short / lines.length >= 0.5) {
        return '<pre class="body-pre">' + esc(lines.join('\n')) + '</pre>';
      }
      return '<p>' + lines.map(esc).join('<br>') + '</p>';
    }).join('');
  }

  function attachListHTML(list) {
    if (!list || !list.length) return '<p class="muted">这条没有附件。</p>';
    return '<ul class="atts">' + list.map((a) => {
      const url = safeUrl(a.url);
      const kind = KIND[a.kind] || a.kind || '附件';
      const bits = [];
      if (typeof a.text_len === 'number' && a.text_len > 0) bits.push('已抽出 ' + a.text_len + ' 字');
      else if (a.parsed === false) bits.push('未抽出正文');
      if (a.note) bits.push(a.note);
      const name = url
        ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(a.name || '未命名附件') + '</a>'
        : esc(a.name || '未命名附件');
      return '<li><span class="kind">' + esc(kind) + '</span>' + name +
        (bits.length ? '<span class="alen">' + esc(bits.join(' · ')) + '</span>' : '') + '</li>';
    }).join('') + '</ul>';
  }

  function extractRows(rec) {
    const ex = rec.extract || {};
    const rows = [
      ['抽取方法', [ex.method, ex.version].filter(Boolean).join(' / ')],
      ['抽取置信度', typeof ex.confidence === 'number' ? String(ex.confidence) + (ex.confidence < 0.5 ? '（低，已标疑似）' : '') : null],
      ['正文长度', typeof rec.body_len === 'number' ? rec.body_len + ' 字' : null],
      ['招生年份', rec.target_year],
      ['培养层次', (rec.degree_types || []).join('、')],
      ['专业方向', (rec.majors || []).join('、')],
      ['申请材料', (rec.materials || []).join('、')],
      ['联系邮箱', rec.contact && rec.contact.email],
      ['联系电话', rec.contact && rec.contact.phone],
      ['抓取时间', rec.crawled_at],
      ['正文指纹', rec.content_hash]
    ];
    return rows
      .filter((r) => r[1] !== null && r[1] !== undefined && r[1] !== '')
      .map((r) => '<div class="row"><span class="k">' + esc(r[0]) + '</span><span class="v">' + esc(r[1]) + '</span></div>')
      .join('');
  }

  /* ---------- fixture 里没有正文的那条 ---------- */

  function renderStub(item, sample) {
    const url = safeUrl(item.url);
    el('results').innerHTML =
      '<article class="dhead">' +
        '<h1 class="dtitle">' + esc(item.title) + '</h1>' +
        '<div class="dmeta">' +
          '<span class="school">' + esc(item.school) + '</span>' +
          (item.unit ? '<span class="sep">·</span><span>' + esc(item.unit) + '</span>' : '') +
          (item.publish_date ? '<span class="sep">·</span><span class="date">' + esc(item.publish_date) + ' 发布</span>' : '') +
        '</div>' +
        '<div class="dactions">' +
          (url ? '<a class="btn-primary" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">看原文 ↗</a>' : '') +
          '<a class="btn-ghost" href="notices.html">← 返回列表</a>' +
        '</div>' +
      '</article>' +
      '<section class="dblock">' +
        '<h2 class="panel-title">正文</h2>' +
        '<p class="muted">演示数据里只提供了一条正文样例（<code>' + esc(sample.id) + '</code>，' +
        esc(sample.title) + '）。真实接口 <code>GET /api/notices/{id}</code> 会返回这一条的完整正文与附件，' +
        '字段形状和刚才那条样例完全一致——所以这一页不是做不了，是这一轮没有第二条样例数据。</p>' +
      '</section>';
  }

  /* 两种来源都会走到这里：接口 404（真数据里没有），和样例里找不到。
   * 措辞按数据源分开 —— 走查发现真接口下说「在演示数据里找不到」是在说假话，
   * 而且真正的原因（id 过期 / 抓取调整后 id 变了）当时没写出来。 */
  function renderNotFound(id) {
    const live = dataSource === 'live';
    el('results').innerHTML =
      '<div class="state err">' +
        '<h3>' + (live ? '接口里没有这条公告' : '样例里没有这条公告') + '</h3>' +
        '<p>请求的 id：<code>' + esc(id || '(空)') + '</code>' +
          (live ? '，接口明确返回 404。' : '，本地样例里找不到。') + '</p>' +
        (live
          ? '<p>常见的两个原因：链接里的 id 已经过期（公告被下架或换了一批）；' +
            '或者抓取调整之后同一条公告换了 id —— 日报里把这种情况叫「id 变更」，' +
            '它既不是新增也不是消失。</p>'
          : '') +
        '<p class="err-exits">' +
          '<a class="btn-ghost" href="notices.html">← 回公告列表</a>　' +
          '<a class="btn-ghost" href="/">匹配结果</a></p>' +
      '</div>';
  }

  boot();
})();
