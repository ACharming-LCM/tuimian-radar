/* 推送预览页（智能体 C）
 *
 * 数据源：`GET /api/push/preview`（契约 §16），回退 `contracts/fixtures/push.preview.json`。
 *
 * **这一版是重写的。** 第一版我读的是 A 的 `events.sample.json`，自己在页面上拼
 * 「截止 X / 该公告未公布截止日 / 未能读取截止日…」三句话。B 的交付单第一条就写着：
 *   「`headline` / `body` 直接用，别自己拼文案。前端拼一次就可能漏掉 unknown 那一支，
 *     而漏掉等于说了"没有截止日"。」
 * 他是对的 —— 同一句话有两个实现，就一定会有一天两边不一样。
 * 所以现在：**文案全部来自服务端**，前端只负责排版和分层。
 *
 * 前端自己保留的只有三件事，都有明确理由：
 *   ① `source.kind === "sample"` 时**必须在页面顶部标记**（B 的硬要求：
 *      没有这个标记就是用假输入冒充真结果）
 *   ② `empty_reason` 四态 → **直接用服务端的 `empty_text`**，不按 reason 再写一遍
 *   ③ 原文链接永远可点（免费身份也一样）
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  const state = {
    payload: null,
    demoEmpty: false,
    // 时效档（C1）。**注意别和下面那个 `#tier-bar` 混了**：那个是"免费/订阅"身份条，
    // 这个是"今天/本周/本月/资料库"。同一个词两个意思，所以命名上分开成 ageTier。
    ageTier: (typeof DEFAULT_TIER === 'string' ? DEFAULT_TIER : 'month')
  };

  /* 事件类型的短标签。类型名和三种取值来自 contracts/events.schema.json，
   * 这里只做**显示用的短词**（长句由服务端的 headline 承担，不重复）。 */
  const TYPE_BADGE = {
    added: { cls: 'ev-added', text: '新公告' },
    deadline_changed: { cls: 'ev-changed', text: '截止日变了' },
    attachment_added: { cls: 'ev-attach', text: '补了附件' }
  };
  const badgeOf = (t) => TYPE_BADGE[t] || { cls: 'ev-other', text: '变更' };

  function boot() {
    load()
      .then(() => {
        markSource();
        render();
        el('state').hidden = true;
      })
      .catch((err) => {
        renderSampleBanner();          // 失败也要先把"这是样例"说清楚
        showError(el('state'), err);
      });
  }

  /* ---------- 数据 ---------- */

  /* 是否优先使用**真实事件流**（不传 `source`，让服务端读 `data/out/events.jsonl`）。
   *
   * ✅ **2026-10-17 已解除（改为 true）。** 两条解除条件当时都写在这里，逐条核过：
   *   ① B 的 `c9e94c6` 修了 `is_backfill`：`server/push.py:381` 有 `if e.get("is_backfill"):`
   *   ② 门禁在 `server/tests/run.py:4759`：「真实事件流上的推送，`items` 里不许出现
   *      `is_backfill=true`」（而且它还钉住了反面：`deadline_changed` **不许**被一刀切掉 ——
   *      数据层对它不标 `is_backfill`，再老的公告改了截止日也该报）
   *
   * 打开前实测过一次（不是只看注释）：`GET /api/push/preview?user=u_c_demo`（**不传 source**）
   * → `source.kind=events`、286 条事件里 146 条命中、推送出现的是 2027 届真公告，
   * 不是「8 年前的公告被称为新公告」。
   *
   * 保留成具名常量的理由没变：下一个人要能一眼看出"现在用真实事件流"这件事是**可切换的**，
   * 以及切回去的条件是什么（如果哪天事件流里又混进补抓，改成 false 就能退回样例）。
   * `?source=sample` 仍然随时可用（截图、对比、演示兜底）。 */
  const PREFER_REAL_EVENTS = true;

  async function load() {
    const q = new URLSearchParams(location.search);

    // `?source=sample` 是**显式**要求看样例（截图、对比、演示兜底时用）。
    const explicitSample = q.get('source') === 'sample';
    // 真实事件流还没准备好时，也只能走样例 —— 但这件事必须由上面那个常量表达，
    // 不是把 'sample' 写死在请求里。
    const wantSample = explicitSample || !PREFER_REAL_EVENTS;

    const params = { user: currentUser() };
    if (wantSample) params.source = 'sample';

    const data = await liveOrFixture(
      () => apiGet('/api/push/preview', params),
      'push.preview.json',
      { missingOk: true }            // 接口还没上线 → 允许落样例，页脚会标出来
    );
    state.payload = data || {};
    state.askedSample = wantSample;
    state.explicitSample = explicitSample;

    if (/[?&]demo=empty/.test(location.search)) {
      // 只用于截「空态」那张图（四态自查 + 截图清单）。真接口的 no_events_file
      // 只在事件文件不存在时出现，而样例永远有 5 条，不给开关就截不到。
      state.demoEmpty = true;
    }
  }

  /* ---------- 顶部：样例标记（B 的硬要求） ---------- */

  function renderSampleBanner() {
    const host = el('banner');
    if (!host) return;
    const p = state.payload || {};
    const src = p.source || {};

    // **数据陈旧**（契约 §16.4 新增）：`source.stale` 为真时顶部必须警告。
    // 文案**直接用 `source.note`**，不自己拼 —— 同一条口径两边各写一遍，早晚不一样。
    // 这一条比"样例"更靠前：样例是"输入不是真的"，陈旧是"输入是真的但过期了"，
    // 后者更容易被误当成最新数据。
    if (src.stale) {
      host.innerHTML =
        '<div class="sample-banner stale-banner" role="status">' +
          '<strong>数据已超过 30 小时没更新</strong>　下面这些推送可能已经过时。' +
          '<span class="sb-sub">' + mdInline(src.note || '（接口没给说明）') + '</span>' +
        '</div>';
      host.hidden = false;
      return;
    }

    if (src.kind !== 'sample') { host.innerHTML = ''; host.hidden = true; return; }

    host.innerHTML =
      '<div class="sample-banner" role="status">' +
        '<strong>演示样例事件</strong>　这不是今天真实抓到的公告。' +
        // 服务端那句 note 里带 markdown 星号（`**演示样例事件**`），
        // 直接 esc 会在页面上看到两个星号 —— 用 common.js 的 mdInline 转成粗体。
        '<span class="sb-sub">' + mdInline(src.note || '') +
          (src.total ? '（共 ' + src.total + ' 条样例）' : '') + '</span>' +
      '</div>';
    host.hidden = false;
  }

  /* ---------- 渲染 ---------- */

  function render() {
    renderSampleBanner();
    renderTier();
    renderPhone();
    renderList();
  }

  function items() {
    if (state.demoEmpty) return [];
    return (state.payload && state.payload.items) || [];
  }

  /* 分层条：订阅状态**只用**服务端给的 plan，不自己算 */
  function renderTier() {
    const node = el('tier-bar');
    if (!node) return;
    const p = state.payload || {};
    const plan = p.plan || {};
    const prof = p.profile || {};
    const sub = !!plan.active;

    const switchHTML =
      '<span class="tier-switch">' +
        '<a class="tier-btn' + (sub ? '' : ' is-on') + '" href="?user=u_demo">免费身份</a>' +
        '<a class="tier-btn' + (sub ? ' is-on' : '') + '" href="?user=u_demo_sub">订阅身份</a>' +
      '</span>';

    let status;
    if (plan.text) {
      status = '<span class="tier-note">' + esc(plan.text) + '</span>';
    } else if (!p.plan) {
      status = '<span class="tier-note tier-note-warn">读不到订阅状态。</span>';
    } else {
      status = '<span class="tier-note">' + (sub ? '已订阅' : '免费身份') + '</span>';
    }

    // 画像没填 → 直接给入口，而不是让人看着空列表猜
    const profHTML = (prof.filled === false)
      ? '<div class="tier-note tier-note-warn">' +
          esc(prof.text || '还没填画像，我们不知道推什么给你。') +
          '　<a href="onboard.html">去填画像 →</a></div>'
      : '';

    node.innerHTML =
      '<div class="tier-bar">' +
        '<div class="tier-main">' + switchHTML + status + '</div>' +
        '<div class="tier-user">身份：<code>' + esc(currentUser()) + '</code>' +
          (p.user_id && p.user_id !== currentUser()
            ? '　<span class="tier-mismatch">（这份样例属于 ' + esc(p.user_id) + '）</span>' : '') +
        '</div>' +
      '</div>' + profHTML;
    node.hidden = false;
  }

  /* ---------- 手机推送样式（截图/视频的主画面） ---------- */

  function pickShowcase(list) {
    // 优先「新增 + 截止日读到了」——那是用户最想看到的样子
    return list.find((x) => x.type === 'added' && x.deadline && x.deadline.state === 'found')
      || list.find((x) => x.type === 'added')
      || list[0] || null;
  }

  function renderPhone() {
    const wrap = el('phone-wrap');
    if (!wrap) return;
    const it = pickShowcase(items());
    if (!it) { wrap.hidden = true; return; }

    const bm = badgeOf(it.type);
    const when = relativeTime(it.at);
    const u = safeUrl(it.url);

    wrap.innerHTML =
      '<div class="phone-head">' +
        '<h2 class="panel-title">你会收到的推送</h2>' +
        '<span class="phone-hint">锁屏上的样子 · 点开就到原文</span>' +
      '</div>' +
      '<div class="phone">' +
        '<div class="phone-status"><span>推免雷达</span><span>' + esc(when) + '</span></div>' +
        '<div class="notif">' +
          '<div class="notif-top">' +
            '<span class="notif-app"><span class="notif-dot"></span>推免雷达</span>' +
            '<span class="notif-when">' + esc(when) + '</span>' +
          '</div>' +
          '<div class="notif-badge ' + bm.cls + '">' + esc(bm.text) + '</div>' +
          '<div class="notif-school">' + esc(it.school) + (it.unit ? ' · ' + esc(it.unit) : '') + '</div>' +
          '<div class="notif-title">' + esc(it.title) + '</div>' +
          /* 这一行**原样用服务端的 body**：三态措辞就在里面 */
          '<div class="notif-body">' + esc(it.body || '') + '</div>' +
          reminderHTML(it, true) +
          (u ? '<a class="notif-open" href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">看原文 ↗</a>' : '') +
        '</div>' +
        '<div class="phone-foot">推免雷达 · 只推跟你有关的</div>' +
      '</div>' +
      '<p class="phone-note">这就是全部内容 —— 没有"点开 App 才知道是什么"的悬念。' +
        '推送里已经把<strong>学校、标题、截止日</strong>说完了，你决定要不要看原文。</p>';
    wrap.hidden = false;
  }

  /* 提醒窗口（T-7 / T-3 / T-1）：文案由服务端给，前端只决定放哪 */
  function reminderHTML(it, inPhone) {
    const r = it.reminder;
    if (!r || !r.text) return '';
    return '<div class="notif-remind' + (inPhone ? '' : ' push-remind') + '">' +
      '<span class="remind-kind">' + esc(r.kind || '提醒') + '</span>' +
      esc(r.text) + '</div>';
  }

  /* ---------- 列表 ---------- */

  /** 有推送、但当前时效档里没有 —— 和"今天完全没有推送"是两件事。
   *  前者说明**我们筛的是时间**，后者说明**今天真没有**。文案不能共用。 */
  function tierEmptyHTML(all, counts) {
    const tierLabel = (TIERS.find((t) => t.key === state.ageTier) || {}).label || '';
    // C7：最近 3 天空的时候说**实情**（"官网近 3 天没有新的 2027 招生公告，下面是本周的 N 条"），
    // 不写"暂无数据"——那会让人以为整个库是空的。
    if (state.ageTier === 'd3') return d3EmptyHTML(counts);
    const ages = all.map((it) => ageDaysOf(it)).filter((d) => d !== null);
    const oldest = ages.length ? Math.max.apply(null, ages) : null;
    return '<div class="state">' +
      '<h3>「' + esc(tierLabel) + '」这一档没有推送</h3>' +
      '<p class="muted">这批推送一共 <strong>' + all.length + '</strong> 条，最早的一条是' +
        esc(oldest === null ? '发布日未知的' : ageText(oldest) + '发的') +
        ' —— <strong>不是没有推送，是它们都不在这个时间范围里</strong>。' +
        '切到「本月」或「资料库」就能看到。</p>' +
      '</div>';
  }

  function renderList() {
    const head = el('list-head');
    const raw = items();
    // C2：**没有 why 的一条都不显示**（宁缺毋滥）。
    // 推送的全部价值是"我凭什么推给你"，一条说不出理由的推送，
    // 用户只能当成骚扰 —— 少一条比多一条坏。
    const all = raw.filter((it) => Array.isArray(it.why) && it.why.length > 0);
    // 参考资料（目录 / 简章 / EMBA）**不进推送流**（C17 ②）：它们不是"今天该看的机会"，
    // 混在推送里会让人以为学校发了招生通知。收进列表末尾一个可折叠的「参考资料」区，
    // 既不给它们推送位，也不把它们藏起来（用户可能就是想找简章）。
    const stream = all.filter((it) => !isReference(it));
    const refs = all.filter(isReference);
    const p = state.payload || {};
    const t = p.tally || {};

    // 时效档（C1）：推送页的列表是**完整**的（服务端只发 20 条，`tally.sent`），
    // 所以四档计数就是在这 20 条上算 —— 和点下去能出来的条数必然一致。
    const counts = tierCounts(stream);
    const list = stream.filter((it) => inTier(ageDaysOf(it), state.ageTier));

    if (head) {
      if (!stream.length) head.innerHTML = '';
      else {
        head.innerHTML = '<div class="list-head"><h2 class="panel-title">今天全部的推送</h2>' +
          '<span class="hist-range">' + list.length + ' / ' + stream.length + ' 条' +
            (typeof t.matched === 'number' ? '（命中 ' + t.matched + ' 条事件）' : '') + '</span></div>' +
          tierChipsHTML(counts, state.ageTier);
        // 事件委托挂在容器上：innerHTML 换掉时监听不丢，也不会重复挂。
        if (!head.dataset.wired) {
          head.dataset.wired = '1';
          head.addEventListener('click', (e) => {
            const btn = e.target.closest('.fchip');
            if (!btn || !btn.dataset.tier) return;
            state.ageTier = btn.dataset.tier;
            renderList();
          });
        }
      }
      head.hidden = false;
    }

    const node = el('results');

    if (!list.length) {
      // 三种"空"必须分开说：
      //   ① 今天真没有推送（服务端 empty_text）
      //   ② 有推送，但这一档没有（tierEmptyHTML）
      //   ③ **有推送，但一条都说不出理由**（也宁可不显示 —— C2 的"宁缺毋滥"）
      if (!all.length && raw.length) {
        node.innerHTML = noWhyHTML(raw.length);
      } else {
        node.innerHTML = stream.length ? tierEmptyHTML(stream, counts) : emptyHTML();
      }
      return;
    }

    const html = list.map(cardHTML);

    // **末尾有几条已截止**（契约新增 `tally.expired`）：必须说出来。
    // 不说的话，用户会拿一条"已经截止"的公告去准备材料 —— 那是这个产品最不该造成的事。
    // 放在列表最后，因为服务端就是按"已截止的沉底"排的。
    if (typeof t.expired === 'number' && t.expired > 0) {
      html.push('<p class="expired-note">列表最后 <strong>' + t.expired +
        '</strong> 条<strong>已经截止</strong>（发布日是过去的，截止日已过）。' +
        '留着是为了你能倒查"它什么时候截止的"，<strong>别照着它们安排时间</strong>。</p>');
    }

    // 服务端说被截断了（免费层），就把墙挂在**它自己的数**上，不自己算
    if (t.truncated && t.truncated_reason === 'free') {
      const rest = Math.max(0, (t.matched || 0) - (t.sent || 0));
      html.push(wallHTML(rest, t.free_limit));
    } else if (/[?&]demo=freewall/.test(location.search)) {
      // 只为截图：样例是订阅身份的快照，不给开关就拍不到付费墙那张图
      html.push(wallHTML(0, t.free_limit));
    }

    // 参考资料区（C17 ②）：目录 / 简章 / EMBA 不进推送流，但也不能凭空消失 ——
    // 折在最后，默认收起，标题写明"它们不是今天的机会"。
    if (refs.length) {
      html.push('<details class="ref-more"><summary>' +
        '参考资料（目录 / 简章 / EMBA）' + refs.length + ' 条 —— 不是今天的机会，收在这里' +
        '</summary>' + refs.map(cardHTML).join('') + '</details>');
    }

    node.innerHTML = html.join('');
    wireWhy(node);
  }

  /* ---------- 「为什么推给你」（C2）：读 B2 的 `why`，**不重写理由** ----------
   *
   * 服务端每条 `why` 都是一句人话（"意向学校命中：北京大学"）。这一层只把**前半句**
   * 抽成短标签，整句原样留作"点开看依据"。
   *
   * ⛔ 前端不换一种说法。理由的措辞是服务端的唯一事实来源：
   *    前端自己改写一遍，两边就会各说一套，之后没人知道哪句是准的。
   *    这和 deadline.js 那条「三态措辞全站只有一处实现」是同一个道理。
   *
   * 契约 §18.5 还说明了一件事：`score_dims` 的**加权和不给**（那是内部公式），
   * 所以这里只显示"命中了哪几维"，不显示分数。
   */
  const WHY_KINDS = [
    { test: /^意向学校/, label: '意向学校', hint: '你的画像里填了这个意向学校' },
    { test: /^专业命中/, label: '专业命中', hint: '公告的专业列表里有你填的本科专业' },
    { test: /^方向命中/, label: '方向命中', hint: '公告的方向词和你填的方向重合' },
    { test: /^方向未命中/, label: '方向未命中', hint: '你填了方向词，但这条公告里没出现（照实标出来，不装作命中）' },
    { test: /发布（/, label: '发布时间', hint: '按发布时间算的新旧程度' },
    { test: /截止/, label: '截止紧迫', hint: '按截止日算的紧迫程度' }
  ];

  function whyKindOf(text) {
    return WHY_KINDS.find((k) => k.test.test(text)) || { label: '理由', hint: '服务端给的理由' };
  }

  function whyDetailOf(text) {
    return whyKindOf(text).hint + '：' + text;
  }

  function whyRowHTML(it) {
    const why = (it.why || []).filter(Boolean);
    if (!why.length) return '';
    const tags = why.map((w, i) =>
      '<button type="button" class="why-tag" data-idx="' + i + '" aria-expanded="false" ' +
      'title="' + esc(whyDetailOf(w)) + '">' + esc(whyKindOf(w).label) + '</button>'
    ).join('');
    return '<div class="why-row" data-why="' + esc(encodeURIComponent(JSON.stringify(why))) + '">' +
      '<span class="why-cap">为什么推给你</span>' + tags + '</div>' +
      '<p class="why-detail" hidden></p>';
  }

  /** 点标签展开依据。**同一张卡只展开一条**，再点一下收起。 */
  function wireWhy(node) {
    if (!node || node.dataset.whyWired) return;
    node.dataset.whyWired = '1';
    node.addEventListener('click', (e) => {
      const btn = e.target.closest('.why-tag');
      if (!btn) return;
      const row = btn.closest('.why-row');
      const det = row && row.nextElementSibling;
      if (!row || !det) return;
      let list = [];
      try { list = JSON.parse(decodeURIComponent(row.dataset.why || '')); } catch (err) { list = []; }
      const idx = String(btn.dataset.idx);
      const same = (det.dataset.idx === idx) && !det.hidden;
      row.querySelectorAll('.why-tag').forEach((b) => {
        const on = (b === btn) && !same;
        b.setAttribute('aria-expanded', String(on));
        b.classList.toggle('is-on', on);
      });
      if (same) { det.hidden = true; det.dataset.idx = ''; return; }
      det.hidden = false;
      det.dataset.idx = idx;
      det.textContent = whyDetailOf(list[Number(idx)] || '');
    });
  }

  /** 有推送、但一条都说不出理由 —— 这不是"没有推送"，是"我们不推说不出理由的"。 */
  function noWhyHTML(n) {
    return '<div class="state">' +
      '<h3>这次一条都没有推</h3>' +
      '<p class="muted">服务端返回了 <strong>' + n + '</strong> 条候选，但<strong>没有一条给出推送理由</strong>。' +
      '说不清"为什么推给你"的推送，和骚扰没区别，所以这里宁可不显示 —— ' +
      '不是今天没有新公告，也不是你画像填得不对。</p>' +
      '</div>';
  }

  /* ---------- 空态：四态四句，**直接用服务端的 empty_text** ---------- */

  /* 空的时候**光说"没有"是不够的** —— 三种"没有"的原因对用户完全不同：
   *   · 事件文件还不存在：**管道这一轮没跑**（我们的问题，不是学校没发通知）
   *   · 事件流读到了但是空：**今天确实没有新公告**（这是结论，得有依据）
   *   · 样例：**不是今天真实抓到的**（演示输入）
   * 所以除了服务端那句 `empty_text`，再按 `source.kind` 补一句 **"为什么空"**。 */
  function sourceKindNote() {
    const src = (state.payload && state.payload.source) || {};
    const kind = src.kind;
    if (kind === 'none') {
      return '<p class="muted"><strong>事件文件还不存在</strong>：数据管道这一轮还没跑。' +
        '这不是「今天没有新公告」—— 我们还没开始看。</p>';
    }
    if (kind === 'events') {
      return '<p class="muted"><strong>事件流读到了，里面是空的</strong>：今天确实没有产生新事件。' +
        '我们只说"没观测到新事件"，不说"学校都不会有变化"。</p>';
    }
    if (kind === 'sample') {
      return '<p class="muted">这一份是<strong>演示样例事件</strong>' +
        (state.explicitSample ? '（你显式加了 <code>?source=sample</code>）' : '') +
        '，不是今天真实抓到的。</p>';
    }
    return '';
  }

  function emptyHTML() {
    const p = state.payload || {};
    if (state.demoEmpty) {
      return '<div class="state empty-state">' +
        '<h3>今天没有值得打扰你的新变化</h3>' +
        '<p>推送只讲三件事：<strong>学校新发了公告</strong>、<strong>截止日变了</strong>、<strong>补发了附件</strong>。' +
        '这一轮这三类都是 0。</p>' +
        '<p class="muted">这不是"坏了"——学校不会每天发通知。但这<strong>不等于你关注的项目没有截止日</strong>，' +
        '自己的节奏还要去<a href="notices.html">公告列表</a>翻一遍。</p></div>';
    }

    // 契约 §16.4：empty_reason 四态四句，`empty_text` 已经是给人看的那句
    const reason = p.empty_reason || 'no_events_file';
    const text = p.empty_text || '今天没有新的推送。';
    const extra = {
      no_profile: '<a class="btn-primary btn-mini" href="onboard.html">去填画像 →</a>',
      no_events_file: '',        // "为什么空"由 sourceKindNote() 按 source.kind 说，避免两套说法
      no_match: '<p class="muted">有事件，但一条都没命中你的画像。' +
        '可能是画像填得太窄 —— 去<a href="onboard.html">画像页</a>放宽一两项试试。</p>',
      // **第六态**（契约新增）：数据旧 + 没匹配到。它和 no_match 的区别是——
      // 光说"没匹配到"会让用户以为画像填窄了，实际是我们的数据已经放了两天。
      stale_events: '<p class="muted">事件流本身已经超过 30 小时没更新，' +
        '所以"没有匹配"这件事<strong>不能说明你现在没有机会</strong> —— ' +
        '先等数据更新，或者直接去<a href="notices.html">公告列表</a>看当前库里的内容。</p>',
      '': ''
    }[reason] || '';

    // **`mdInline` 而不是 `esc`**：服务端给的 `empty_text` 里带 markdown（`**不知道**`）。
    // 用 `esc` 会在页面上露出两个星号 —— 走查 #7 踩过同一个坑（后端文案里的 `**` 漏出来）。
    return '<div class="state empty-state">' +
      '<h3>' + mdInline(text) + '</h3>' + extra + sourceKindNote() +
      (p.generated_at ? '<p class="muted">这一份生成于 ' + esc(String(p.generated_at).replace('T', ' ')) + '</p>' : '') +
      '</div>';
  }

  /* ---------- 一条推送 ---------- */

  /* 这一条是"招生通知"还是别的东西（C17 ②，2026-10-19）
   *
   * 服务端给了 `notice_kind`：announcement / other / catalog / emba。
   * 首屏十条里 7 条 announcement、3 条 other，**原来界面上完全看不出区别** ——
   * 而那 3 条 other 往往是"报考点公告：请上传材料"这类，对推免学生的价值和招生通知不是一回事。
   *
   * 三条硬规矩：
   *   ① **必须有文字**，不许只靠颜色（色盲用户）；
   *   ② `catalog`（目录 / 简章 / 参考书，265 条）**不进推送流**，收进下面的「参考资料」区；
   *   ③ 标签是"这条是什么"，不是"这条值不值得看"——不写"推荐/不重要"这种评价词。
   */
  const KIND_LABEL = {
    announcement: { text: '招生通知', cls: 'kind-ann', hint: '带招生/报名/推免信息的公告' },
    other: { text: '其他', cls: 'kind-other', hint: '不是招生通知（常见：报考点、咨询活动、复试安排）' },
    catalog: { text: '参考资料', cls: 'kind-ref', hint: '目录 / 简章 / 参考书之类的资料，不占推送位' },
    emba: { text: '参考资料', cls: 'kind-ref', hint: 'EMBA 简章之类的资料，不占推送位' }
  };

  function kindOf(it) {
    return KIND_LABEL[it && it.notice_kind] || { text: '其他', cls: 'kind-other', hint: '服务端没有给类型' };
  }

  /** 参考资料（目录/简章/EMBA）：收进单独一区，不进推送流。 */
  function isReference(it) {
    return it && (it.notice_kind === 'catalog' || it.notice_kind === 'emba');
  }

  function kindChipHTML(it) {
    const k = kindOf(it);
    return '<span class="kind-chip ' + k.cls + '" title="' + esc(k.hint) + '">' + esc(k.text) + '</span>';
  }

  function cardHTML(it) {
    const bm = badgeOf(it.type);
    const u = safeUrl(it.url);
    const when = relativeTime(it.at);

    return '<article class="push-card">' +
      '<div class="push-side">' +
        '<span class="push-badge ' + bm.cls + '">' + esc(bm.text) + '</span>' +
        '<span class="push-when">' + esc(when) + '</span>' +
        // 「发布于多久前」：`push-when` 说的是**检测到**的时间，这个说的是**公告发布**的时间。
        // 两个时间不一样很常见（补抓/重解析会让事件"刚刚"发生，而公告是 10 天前发的），
        // 只给一个的话，用户会拿检测时间去判断新旧 —— 那是错的。
        (it.publish_date ? '<span class="age-badge">发布于' + esc(ageText(ageDaysOf(it))) + '</span>' : '') +
        kindChipHTML(it) +
      '</div>' +
      '<div class="push-body">' +
        /* headline / body 都是服务端拼好的，前端**不再加工** */
        '<h3 class="push-title">' + esc(displayTitle(it)) + '</h3>' +
        '<div class="push-serverline">' + esc(it.body || '') + expiredBadgeHTML_(it) + '</div>' +
        deadlineBlockHTML(it) +
        // 这里**不再**渲染 matched_because 芯片：下面 whyRowHTML 已经是同一件事，
        // 而且能点开看依据。两行「为什么推给你」上下叠着，是页面上最扎眼的一处重复。
        changeHTML(it) +
        reminderHTML(it, false) +
        whyRowHTML(it) +
        '<div class="push-actions">' +
          (u ? '<a class="btn-ghost btn-mini" href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">看原文 ↗</a>' : '') +
          (it.notice_id
            ? '<a class="btn-ghost btn-mini" href="detail.html?id=' + encodeURIComponent(it.notice_id) + '">详情页</a>'
            : '') +
        '</div>' +
      '</div>' +
    '</article>';
  }

  /* 截止日那一条。
   *
   * `body` 里**已经**有截止日的三态说法（服务端拼的），所以这里再列一遍会重复。
   * 但 `absent` / `unknown` 这两种必须**在视觉上跳出来**：
   * 一行灰字里夹着「未能读取截止日」，读者很可能一眼扫过去当成"没事"。
   * 所以只有这两种多挂一个带颜色的行（`unknown` 还带原文链接，由 deadline.js 保证）。
   * `found` 不重复 —— 日期已经在那行 body 里了。 */
  function deadlineBlockHTML(it) {
    const d = it.deadline;
    if (!d || !d.state || d.state === 'found') return '';
    const html = deadlineLineHTML({ deadline: d, url: it.url });
    return '<div class="push-deadline">' + html + '</div>';
  }

  /* 已过期徽标：挂在**服务端那行文字后面**，不重新拼日期。
   *
   * 为什么不在 `deadlineBlockHTML` 里加：那一块对 `found` 是**故意留空**的 ——
   * 服务端拼的 `body` 里已经写着「已于 2026-10-15 截止（已过 3 天）」，
   * 再渲染一次日期就是同一句话说两遍。所以这里只补一个徽标：
   * 它要说的不是"过期了"（那行已经说了），而是"**过期了也可能仍有效，请点原文核对**"。 */
  function expiredBadgeHTML_(it) {
    const d = it && it.deadline;
    if (!d || d.state !== 'found') return '';          // absent / unknown 一律不算过期
    const left = (typeof d.days_left === 'number') ? d.days_left : null;
    if (left === null || left >= 0) return '';
    // **防御**：徽标是附加信息，它拿不到绝不能让整张卡片渲染失败。
    // （第一版就是直接调用，而 deadline.js 没导出它 → 整页一张卡片都不出。
    //   附加信息的可用性不能和主内容绑死，这是这个项目一贯的原则。）
    return (typeof expiredBadgeHTML === 'function') ? expiredBadgeHTML() : '';
  }

  /** 标题去重：服务端 headline 常带「【新公告】学校：」前缀，
   *  而卡片左边已经有「新公告」徽标、正文上方已经有学校名 —— 同一句话说两遍。 */
  function displayTitle(it) {
    let t = String(it.headline || it.title || '');
    t = t.replace(/^【(新公告|有变化|补抓的旧公告)】\s*/, '');
    // 「学校：标题」里的学校名在卡片头已经出现（含「XX大学：」）
    if (it.school && t.indexOf(it.school + '：') === 0) t = t.slice(it.school.length + 1);
    else if (it.school && t.indexOf(it.school + ':') === 0) t = t.slice(it.school.length + 1);
    return t.trim() || String(it.title || '');
  }

  function matchedHTML(it) {
    const m = (it.matched_because || []).filter(Boolean);
    if (!m.length) return '';
    // 同一张卡只留一处「为什么推给你」：有详细 why（可点开依据）时，
    // 这一行就是重复的 —— 第一版两张标签并排出现，读起来像两套理由。
    if ((it.why || []).filter(Boolean).length) return '';
    return '<div class="push-why">' +
      '<span class="why-label">为什么推给你</span>' +
      '<span class="why-chips">' + m.slice(0, 6).map((t) => '<span class="why-chip">' + esc(t) + '</span>').join('') + '</span>' +
    '</div>';
  }

  /* 变更细节：只说"变成了什么"。两种事件说法不同，分开写。 */
  function changeHTML(it) {
    const ch = it.change;
    if (!ch) return '';

    if (ch.field === 'deadline') {
      const to = ch.to != null ? String(ch.to) : null;
      const from = ch.from != null ? String(ch.from) : null;
      let line;
      if (from && to) line = '截止日从 <strong>' + esc(from) + '</strong> 改成 <strong>' + esc(to) + '</strong>';
      else if (!from && to) {
        // from=null 的语义是"之前没读到"。**不能说成"原来没有"** —— 那是 §14 整节在防的事。
        line = '之前<strong>没能读到</strong>截止日，这次读到了 <strong>' + esc(to) + '</strong>';
      } else if (from && !to) line = '之前的截止日 <strong>' + esc(from) + '</strong> 这次<strong>没能读到</strong>了';
      else line = '截止日有变化，但这次没给出前后值';
      return '<div class="push-change">' + line + '</div>';
    }

    if (ch.field === 'attachments') {
      const atts = ch.attachments || [];
      if (!atts.length) return '<div class="push-change">补发了附件，但这次没给出附件清单</div>';
      const itemsHTML = atts.map((a) => {
        const au = safeUrl(a.url);
        const name = a.name || '（未命名附件）';
        return '<li>' + (au
          ? '<a href="' + esc(au) + '" target="_blank" rel="noopener noreferrer">' + esc(name) + '</a>'
          : esc(name)) + '</li>';
      }).join('');
      return '<div class="push-change"><div>补发了 <strong>' + atts.length + '</strong> 个附件：</div>' +
        '<ul class="push-atts">' + itemsHTML + '</ul></div>';
    }

    return '';
  }

  function wallHTML(rest, freeLimit) {
    const tail = rest > 0
      ? '还有 <strong>' + rest + '</strong> 条推送'
      : '免费身份只看得到前 <strong>' + (freeLimit || 3) + '</strong> 条';
    return '<div class="paywall paywall-wall">' +
      '<div class="paywall-icon">🔒</div>' +
      '<h3 class="paywall-title">' + tail + '</h3>' +
      '<p class="paywall-text">订阅后看全部。当前是<strong>免费身份</strong>。' +
        '月度订阅 ¥29 / 月，含推送 + 匹配排序 + 套磁信。</p>' +
      '<div class="paywall-actions">' +
        '<a class="btn-primary" href="pricing.html">去订阅</a>' +
        '<span class="paywall-free">原文链接<strong>永远免费</strong>，不订阅也能点。</span>' +
      '</div>' +
    '</div>';
  }

  /* ---------- 相对时间 ---------- */

  /** `at` 是「我们**检测到**变更的时刻」，不是公告发布时刻（契约明说）。 */
  function relativeTime(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return '时间未知';
    const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
    if (mins < 1) return '刚刚';
    if (mins < 60) return mins + ' 分钟前';
    const h = Math.floor(mins / 60);
    if (h < 24) return h + ' 小时前';
    return Math.floor(h / 24) + ' 天前';
  }

  /* 给排查/截图用：演示开关不经过任何真实数据 */
  window.TuimianPush = { state: state, items: items };

  boot();
})();
