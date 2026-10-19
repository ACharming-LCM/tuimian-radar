/* 站内通知中心（智能体 C，C13，2026-10-19）
 *
 * 铃铛挂在 `.hero` 右上角（不是 `.nav` —— 375 下 `.nav` 是横向滑动条，挂进去会被挤出屏幕，
 * 而且 `body{overflow-x:hidden}` 会让量具看不见这件事，见 mount() 里那段）。
 * 每个页面都有 `.hero`，所以十一个页面一次生效，不用十份 HTML 各写一套定位。
 *
 * ## 数据从哪来（两个来源，别只用一个）
 *
 *   ① `/api/digest?user=`  —— **服务端**按我的关注规则算好的「变了的东西」。
 *      匹配语义就是 `/api/notices` 那套 `substring_hit`（`server/watch.py`），权威。
 *      但它只覆盖「本轮抓取发现的新增/变更」，没有变更记录的日子它是空的。
 *   ② `/api/updates?days=7` ∩ 我的 watches —— 「最近新发布、还新鲜、没过期」的公告。
 *      这一半要在前端匹配，因为接口没有 `user` 参数（`/api/updates` 是公共的）。
 *      **代价要说清**：`/api/updates` 的 item 不带 `body_text`，所以关键词只搜
 *      标题/学校/院系/专业/标签 —— 比服务端 `watch._hay()` 少一块正文。宁可少报，
 *      也不能报一条点进去跟关注无关的东西。
 *
 *
 * ## 红点只在真有未读时亮
 *
 * 「已读」不是服务端状态（没有这个接口，也不该为一个演示造一个）。
 * 存在本机 localStorage，键带 user：换身份不继承另一个身份的已读。
 * 规则是**按条记录**而不是"最后访问时间" —— 时间戳会把"两天前没看的那条"
 * 在下次打开时判成已读，等于红点替用户做了决定。
 *
 * 没关注 → 不可能有通知 → 不亮（这是 `no_watches` 那一支）；
 * 关注了但没新东西 → 也不亮（这是"如实"：说"没更新"比亮个点更值钱）。
 */
(function () {
  'use strict';

  if (!useLive()) return;          // 样例模式下没有真实通知，不装这个铃铛

  const BELL_SVG = '<svg class="bell-ico" viewBox="0 0 16 16" aria-hidden="true">' +
    '<path d="M8 1.6a3.9 3.9 0 0 0-3.9 3.9v2.3L3 10.2h10L11.9 7.8V5.5A3.9 3.9 0 0 0 8 1.6Z" ' +
    'fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>' +
    '<path d="M6.4 12.2a1.7 1.7 0 0 0 3.2 0" fill="none" stroke="currentColor" ' +
    'stroke-width="1.3" stroke-linecap="round"/></svg>';

  const state = {
    watches: [],
    items: [],
    read: localList('read'),
    muted: localList('muted'),
    open: false,
    error: null,
    partial: null,
    showRead: false,      // 面板要不要把已读的也列出来（默认只列未读）
    loaded: false,
    loadedAt: ''
  };

  /* ---------- 匹配（对齐 server/watch.py 的语义） ---------- */

  const norm = (v) => String(v == null ? '' : v).trim().toLowerCase();

  function sub(needle, hay) {
    const n = norm(needle);
    return !n || norm(hay).indexOf(n) !== -1;
  }

  /** 关键词的搜索面。**比服务端少"正文"**：/api/updates 的 item 不带 body_text。 */
  function hayOf(it) {
    return [
      it.title, it.school, it.unit, it.board,
      (it.majors || []).join(' '), (it.tags || []).join(' ')
    ].filter(Boolean).join(' ');
  }

  function watchHit(w, it) {
    if (w.school && !sub(w.school, it.school)) return false;
    if (w.unit && !sub(w.unit, it.unit)) return false;
    const kws = (w.keywords || []).filter(Boolean);
    if (kws.length) {
      const hay = hayOf(it);
      if (!kws.some((k) => sub(k, hay))) return false;
    }
    return true;
  }

  function whyOf(w) {
    const bits = [];
    if (w.school) bits.push(w.school);
    if (w.unit) bits.push(w.unit);
    const kws = (w.keywords || []).filter(Boolean);
    if (kws.length) bits.push('关键词 ' + kws.join('、'));
    return '你关注了 ' + bits.join(' · ');
  }

  function relDay(iso) {
    const s = String(iso == null ? '' : iso).slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    const n = new Date();
    const p = (x) => (x < 10 ? '0' + x : String(x));
    const today = n.getFullYear() + '-' + p(n.getMonth() + 1) + '-' + p(n.getDate());
    if (s === today) return '今天';
    const days = Math.round((Date.UTC(+today.slice(0, 4), +today.slice(5, 7) - 1, +today.slice(8, 10)) -
      Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10))) / 86400000);
    if (days === 1) return '昨天';
    if (days > 1 && days < 30) return days + ' 天前';
    return s;
  }

  /* 两个 ISO 日期差几天（只按日历日算，不做时区换算）。解析不了返回 null。 */
  function daysBetween(a, b) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(a || '').slice(0, 10));
    const n = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(b || '').slice(0, 10));
    if (!m || !n) return null;
    return Math.round((Date.UTC(+n[1], +n[2] - 1, +n[3]) - Date.UTC(+m[1], +m[2] - 1, +m[3])) / 86400000);
  }

  /* ---------- 组装通知 ---------- */

  function build(watchRes, digestRes, updatesRes) {
    const watches = (watchRes && watchRes.watches) || [];
    const muted = new Set(state.muted);
    const byId = Object.create(null);

    // ① 服务端算好的"变了的东西"（权威匹配，带 matched_by）
    const dg = (digestRes && digestRes.items) || [];
    const byWatchId = Object.create(null);
    watches.forEach((w) => { byWatchId[w.id] = w; });
    dg.forEach((it) => {
      if (!it || !it.notice_id) return;
      const ids = (it.matched_by || []).map((h) => h.watch_id).filter(Boolean);
      // 服务端的 why 是 `学校=南开大学` 这种**给日志看的**写法；给用户看的那一句
      // 统一由 whyOf() 生成，两种来源（digest / updates）才不会有两种口吻。
      const w0 = ids.length ? byWatchId[ids[0]] : null;
      // 「新收录」不一定是新公告：补抓会把半年前的旧公告也收进来。
      // 发布日比发现日早 30 天以上，就照实说「补抓的旧公告」——
      // 否则用户会以为学校昨天发了通知，然后拿旧公告的截止日去安排时间。
      const lag = daysBetween(it.publish_date, it.changed_on);
      let kindLabel = it.change === 'changed' ? '有变化' : '新收录';
      if (it.change !== 'changed' && lag !== null && lag > 30) kindLabel = '补抓的旧公告';
      byId[it.notice_id] = {
        key: 'd:' + it.notice_id + '@' + (it.changed_on || ''),
        notice_id: it.notice_id,
        title: it.title,
        school: it.school,
        unit: it.unit,
        url: it.url,
        when: it.changed_on || '',
        pub: it.publish_date || '',
        kind: 'change',
        kindLabel: kindLabel,
        why: w0 ? whyOf(w0) : (it.why || ''),
        watch_ids: ids,
        unmuteAll: ids.length > 0 && ids.every((id) => muted.has(id))
      };
    });

    // ② 新发布 ∩ 我的关注（前端匹配，见文件头那段代价说明）
    const ups = (updatesRes && updatesRes.items) || [];
    ups.forEach((it) => {
      if (!it || !it.notice_id || byId[it.notice_id]) return;
      const hits = watches.filter((w) => watchHit(w, it));
      if (!hits.length) return;
      const ids = hits.map((w) => w.id);
      byId[it.notice_id] = {
        key: 'f:' + it.notice_id + '@' + (it.publish_date || ''),
        notice_id: it.notice_id,
        title: it.title,
        school: it.school,
        unit: it.unit,
        url: it.url,
        when: it.publish_date || '',
        pub: it.publish_date || '',
        kind: 'fresh',
        kindLabel: '新公告',
        why: whyOf(hits[0]),
        watch_ids: ids,
        unmuteAll: ids.every((id) => muted.has(id))
      };
    });

    // 静音的规则命中 -> 不进通知（"开关提醒"就是在这里生效的）
    const items = Object.keys(byId).map((k) => byId[k])
      .filter((it) => !it.unmuteAll)
      .sort((a, b) => String(b.when).localeCompare(String(a.when)));

    return { watches, items };
  }

  function unreadOf() {
    const read = new Set(state.read);
    return state.items.filter((it) => !read.has(it.key));
  }

  /* 每条的第三行：**分清"什么时候发现的"和"什么时候发布的"**。
   *
   * 这两个日期不是一回事，混在一起就是误导：
   *   · 变更记录（digest）的 `changed_on` 是**我们抓到变化的那天**，不是发布日 ——
   *     一条 10-08 发的公告，可能今天才被收进来。原来写「新收录 · 2 天前」，
   *     读者会当成"2 天前发布的"，然后去算截止日，差出十天。
   *   · 新公告（updates）的 `publish_date` 才是发布日。
   * 所以两种来源用两种说法，而且**能给出两个日期时就都给出来**。
   */
  function whenTextOf(it) {
    const bits = [];
    const when = it.when ? relDay(it.when) : null;
    const at = (rel, verb) => (rel === '今天' ? '今天' + verb : (rel === '昨天' ? '昨天' + verb : verb + '于 ' + rel));
    if (it.kind === 'change') {
      bits.push(it.kindLabel + (when ? ' · ' + at(when, '发现') : ''));
      if (it.pub && it.pub !== it.when) bits.push(at(relDay(it.pub), '发布'));
    } else {
      bits.push(when ? at(when, '发布') : '发布日期未读到');
    }
    if (it.school) bits.push(it.school + (it.unit ? ' · ' + it.unit : ''));
    return bits.join(' · ');
  }

  /* ---------- 画 ---------- */

  function mount() {
    // **挂 .hero 右上角，不挂 .nav**。挂 .nav 在 375 上是错的：
    // 手机上 `.nav` 是"横向滑动条"（`flex-wrap: nowrap` + `overflow-x: auto`，
    // 见 style.css 那条媒体查询），铃铛会被排在最后一个、滑出屏幕外 ——
    // 而 `body { overflow-x: hidden }` 会把这件事藏起来，量具量 `documentElement.scrollWidth`
    // 也量不到（实测：nav scrollWidth 497 > 可见 375，铃铛 x=392 在屏幕外）。
    // 挂 hero 右上角：滚动的还是导航条，铃铛永远在第一屏。
    const hero = document.querySelector('.hero');
    if (!hero) return null;
    const wrap = document.createElement('span');
    wrap.className = 'np-wrap';
    wrap.innerHTML = '<button type="button" class="nav-bell" id="np-bell" aria-expanded="false" ' +
      'aria-haspopup="dialog" aria-controls="np-panel" title="站内通知">' + BELL_SVG +
      '<span class="bell-label">通知</span><span class="bell-badge" id="np-badge" hidden></span></button>' +
      '<div class="np" id="np-panel" role="dialog" aria-label="站内通知" hidden></div>';
    hero.appendChild(wrap);
    return wrap;
  }

  function renderBell() {
    const badge = document.getElementById('np-badge');
    if (!badge) return;
    const n = unreadOf().length;
    if (n > 0) {
      badge.hidden = false;
      badge.textContent = n > 99 ? '99+' : String(n);
      badge.setAttribute('aria-label', n + ' 条未读');
    } else {
      badge.hidden = true;
      badge.textContent = '';
      badge.removeAttribute('aria-label');
    }
  }

  function panelHTML() {
    const un = unreadOf();
    const head = '<div class="np-head"><span class="np-title">通知</span>' +
      (un.length ? '<button type="button" class="link-btn" id="np-readall">全部已读</button>' : '') +
      '</div>';

    if (state.error) {
      return head + '<p class="np-empty">通知读不到：' + esc(state.error) +
        '<br>读不到就<strong>不知道有没有新东西</strong> —— 这里不会假装"没有"。' +
        '<br><button type="button" class="link-btn" id="np-retry">重试</button></p>';
    }

    if (!state.watches.length) {
      return head + '<p class="np-empty">你还没有关注任何学校 / 学院。<br>' +
        '关注之后，这里只放跟你有关的东西，别的都不进。' +
        '<br><a href="subscriptions.html">去加一条关注 →</a></p>';
    }

    if (!state.items.length) {
      if (state.partial) {
        return head + '<p class="np-empty">' + esc(state.partial) +
          '<br>所以<strong>不能说"没有新东西"</strong> —— 有一路数据我们没拿到。' +
          '<br><button type="button" class="link-btn" id="np-retry">重试</button></p>';
      }
      return head + '<p class="np-empty">关注了 ' + state.watches.length + ' 条规则，最近没有新公告。<br>' +
        '<span class="subs-meta">（不是"坏了"：盯着 ' + esc(state.loadedAt || '刚刚') + ' 的数据看过了。）</span></p>' +
        '<div class="np-foot"><a href="subscriptions.html">管理关注</a><a href="daily.html">今日更新</a></div>';
    }

    // 一口气 28 条里 24 条是补抓的旧公告 —— 不说一句，用户会以为学校同时发了 28 个通知。
    const backfill = state.items.filter((it) => it.kindLabel === '补抓的旧公告').length;
    const backfillNote = (backfill && backfill >= 3)
      ? '<p class="np-empty">这 ' + state.items.length + ' 条里有 <strong>' + backfill +
        ' 条是补抓的旧公告</strong>（发布日早于我们发现它的日子），按发布日看它们不新。</p>'
      : '';

    /* 默认只列未读。
     *
     * 已读的还留在列表里，等于把"有没有新动静"这个唯一的信息淹掉：
     * 28 条全列出来、红点又是灭的，人第一眼不知道到底有没有新的。
     * 所以：有未读就列未读，已读折成一行「还有 N 条已读」；一条未读都没有时
     * 直说「都看过了」，把已读藏在一个开关后面（想看还能看，不占地方）。
     */
    const unreadSet = new Set(state.read);
    const readItems = state.items.filter((it) => unreadSet.has(it.key));
    const base = state.showRead ? state.items : un;
    // 一次最多画 30 条：面板是"有没有动静"的地方，不是列表页。
    // 画 120 条既慢又没人看，还不说"下面还有"就是骗人 —— 所以下面补一句总数。
    const MAX_SHOWN = 30;
    const shown = base.slice(0, MAX_SHOWN);
    const rows = shown.map((it) => {
      const read = unreadSet.has(it.key);
      return '<li class="np-item"><a class="np-link" href="detail.html?id=' + encodeURIComponent(it.notice_id) +
        '" data-key="' + esc(it.key) + '"' + (read ? '' : ' data-unread="1"') + '>' +
        '<span class="np-why">' + esc(it.why || it.school || '') + '</span>' +
        '<span class="np-what">' + (read ? '' : '<strong>· </strong>') + esc(it.title || '（无标题）') + '</span>' +
        '<span class="np-when">' + esc(whenTextOf(it)) + '</span>' +
        '</a></li>';
    }).join('');

    const noneUnread = !un.length
      ? '<p class="np-empty">没有未读 —— 最近 ' + state.items.length + ' 条都看过了。</p>'
      : '';
    const readToggle = readItems.length
      ? '<button type="button" class="link-btn" id="np-toggle-read">' +
        (state.showRead ? '只看未读' : '看已读的 ' + readItems.length + ' 条') + '</button>'
      : '';

    return head + backfillNote + noneUnread + (rows ? '<ul class="np-list">' + rows + '</ul>' : '') +
      (base.length > MAX_SHOWN
        ? '<p class="np-empty">只列最近 ' + MAX_SHOWN + ' 条（一共 ' + base.length +
          ' 条）。更早的在「今日更新」里。</p>'
        : '') +
      '<div class="np-foot"><a href="subscriptions.html">管理关注</a>' + readToggle +
      '<span class="subs-meta">已读只记在这台浏览器</span></div>';
  }

  function renderPanel() {
    const p = document.getElementById('np-panel');
    if (!p) return;
    p.innerHTML = panelHTML();
    p.hidden = !state.open;
    const bell = document.getElementById('np-bell');
    if (bell) bell.setAttribute('aria-expanded', state.open ? 'true' : 'false');
  }

  function markRead(keys) {
    const set = new Set(state.read.concat(keys));
    state.read = Array.from(set);
    localSet('read', state.read);
    renderBell();
    renderPanel();
  }

  /* ---------- 取数 ---------- */

  async function load() {
    const [w, dg, up] = await Promise.all([
      apiGet('/api/watch', userQuery()).catch((e) => ({ __err: e })),
      apiGet('/api/digest', userQuery()).catch((e) => ({ __err: e })),
      apiGet('/api/updates', { days: 7, limit: 50 }).catch((e) => ({ __err: e }))
    ]);

    if (w && w.__err) {
      // 关注规则读不到 -> **不是"没关注"**。这两句对用户是相反的意思。
      state.error = (w.__err && w.__err.message) || String(w.__err);
      state.watches = [];
      state.items = [];
    } else {
      state.error = null;
      const built = build(w, dg && !dg.__err ? dg : null, up && !up.__err ? up : null);
      state.watches = built.watches;
      state.items = built.items;
      // 两路数据源**任何一路**没读到，就不能说"没有新公告" ——
      // "没读到"和"没有"是相反的两句话（这条从数据层一路管到界面）。
      const missing = [];
      if (dg && dg.__err) missing.push('变更记录');
      if (up && up.__err) missing.push('今日更新');
      state.partial = missing.length ? ('有一部分没读到：' + missing.join('、') +
        '（' + ((dg.__err || up.__err).message || '接口没通') + '）') : null;
      const h = (up && up.window_days) || 7;
      state.loadedAt = '最近 ' + h + ' 天';
    }
    state.loaded = true;
    renderBell();
    if (state.open) renderPanel();
  }

  /* ---------- 交互 ---------- */

  function wire(wrap) {
    const bell = document.getElementById('np-bell');
    if (!bell) return;

    bell.addEventListener('click', (e) => {
      e.stopPropagation();
      state.open = !state.open;
      renderPanel();
    });

    document.addEventListener('click', (e) => {
      if (!state.open) return;
      if (wrap.contains(e.target)) return;
      state.open = false;
      renderPanel();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && state.open) { state.open = false; renderPanel(); }
    });

    // 面板里的动作都走委托：面板 innerHTML 会整块重画
    const panel = document.getElementById('np-panel');
    panel.addEventListener('click', (e) => {
      const t = e.target;
      if (t.closest && t.closest('#np-readall')) {
        e.preventDefault();
        markRead(state.items.map((it) => it.key));
        return;
      }
      if (t.closest && t.closest('#np-toggle-read')) {
        e.preventDefault();
        state.showRead = !state.showRead;
        renderPanel();
        return;
      }
      if (t.closest && t.closest('#np-retry')) {
        e.preventDefault();
        state.open = true;
        renderPanel();
        load();
        return;
      }
      const link = t.closest ? t.closest('a.np-link') : null;
      if (link) {
        // 点了就算看过（点完还会跳走，跳走前必须把本机状态写好）
        const k = link.getAttribute('data-key');
        if (k) markRead([k]);
      }
    });
  }

  function start() {
    const wrap = mount();
    if (!wrap) return;
    wire(wrap);
    renderBell();       // 先画"没有未读"，别让页头先空着
    // 通知**不进首屏**：这三个请求要等页面自己的取数落屏之后才发。
    // 实测（_c-perf.js）：只在 requestIdleCallback 里发是不够的 —— load 之后 idle 立刻就到，
    // 三个请求和 /api/push/preview 撞在同一帧，首屏心跳从 ~200ms 拖到 285ms。
    // 所以先**硬等 1.2 秒**（首屏判据是 480ms），再走 idle。
    const go = () => load();
    setTimeout(() => {
      if (typeof window.requestIdleCallback === 'function') {
        window.requestIdleCallback(go, { timeout: 1500 });
      } else {
        go();
      }
    }, 1200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  /* 给别的页面用的只读出口（现在只有「我的订阅」页的「预览会收到什么」用它）。
   *
   * 为什么放在这里、而不是让订阅页自己再拉一遍三个接口：
   * **匹配逻辑只能有一份**。订阅页再实现一遍「digest ∩ watches + updates ∩ watches」，
   * 两边迟早对不上 —— 而这套东西的公信力就建立在「预览里有的，通知里一定有」。
   */
  window.tuimianNotify = {
    ready: () => state.loaded,
    reload: () => load(),
    /** 前 n 条通知（和铃铛里那份完全同一批），字段已经整理成人能看的形状。 */
    top: function (n) {
      return state.items.slice(0, n || 3).map(function (it) {
        return {
          notice_id: it.notice_id,
          title: it.title || '（无标题）',
          why: it.why || it.school || '',
          when: whenTextOf(it),
          kindLabel: it.kindLabel,
          unread: state.read.indexOf(it.key) === -1
        };
      });
    },
    error: function () { return state.error || state.partial || null; },
    count: function () { return state.items.length; },
    watchCount: function () { return state.watches.length; }
  };
})();
