/* 公告列表页（智能体 C）
 * 数据源：contracts/fixtures/notices.list.json（GET /api/notices 的形状）
 * 硬要求同匹配页：deadline 为 null 显示「截止日待确认」不猜日期；confidence<0.5 标「疑似」；带附件标出数量。
 */
(function () {
  'use strict';

  const state = {
    data: null, rows: [],
    school: 'ALL', unit: 'ALL', mode: 'ALL',
    deadline: 'ALL',       // 截止日桶（C17）：within_1/3/7/30 走接口，其余在本页内筛
    q: '', sort: 'publish', attachOnly: false, hasDeadline: false,
    fresh: null,           // GET /api/freshness（读不到就是 null，列表照常显示）
    facets: null,          // GET /api/facets：**全量**筛选项（不是我这一页那 50 条里有的）
    reloading: false       // 后台数据更新后重拉的去重标记（事件可能连发）
  };
  const el = (id) => document.getElementById(id);

  /* 截止日桶（**互斥版**，C17 修订 2026-10-19）。
   *
   * 服务端给两套：`deadline_buckets` 是**累积**的（within_1 ⊂ within_3 ⊂ …，相加 2447 ≠ 2419），
   * `deadline_exclusive_buckets` 是**互斥**的（相加正好 2419）。筛选面板必须用互斥版 ——
   * 累积版一旦被相加或画成饼图，数字就凭空多出 28 条。
   *
   * 但接口的 `deadline_within` 只认 1 / 3 / 7 / 30 这四个**累积上限**，
   * 所以互斥窗口是"先按上限取（服务端），再按下界精确切（本地）"：
   *   within_2_3  -> deadline_within=3，本地只留 2..3 天
   * 7 天内一共 18 条、30 天内 24 条，都在一页里 —— 本地切出来是**精确**的，不是"本页凑合"。
   *
   * 另外四种（已截止 / 30 天以后 / 未读取 / 未公布）接口还没有参数，只能在本页内筛，
   * 页面上会写明「本页内筛」。
   */
  const DL_WINDOW = { within_1: 1, within_2_3: 3, within_4_7: 7, within_8_30: 30, within_7: 7 };
  const DL_RANGE = { within_1: [0, 1], within_2_3: [2, 3], within_4_7: [4, 7], within_8_30: [8, 30] };
  const DL_PAGE_ONLY = { expired: 1, later: 1, unknown: 1, absent: 1 };

  /** 取一次列表（**不碰筛选状态**）。boot 和"数据更新后重拉"共用这一条。
   *
   *  ⚠️ 2026-10-19（C15）：**筛选条件要发给接口，不能只在本地筛已经取回的 50 条**。
   *  之前 `?school=北京航空航天大学` 只显示 3 条 —— 不是北航只有 3 条公告（它有 37 条），
   *  而是"取最新 50 条（全校）→ 本地筛出北航"：09-26 那条「接收推免…通知」根本没被取回来。
   *  这正是这个项目栽过 6 次的"取一页就够了"的同一坑，只是这次藏在本地过滤里。
   *  `/api/notices` 支持 school / unit / q / apply_mode，所以把它们传下去。
   */
  async function fetchRows() {
    const params = { size: 50, meta: 1 };
    // `meta=1`：列表的默认形状被 fixture 钉死（一个多余键都不加），三态信息走 meta 分支。
    // 不给它就拿不到 `field_trust` —— 那是判断 absent / unknown 的唯一依据。
    if (state.school && state.school !== 'ALL') params.school = state.school;
    if (state.unit && state.unit !== 'ALL') params.unit = state.unit;
    if (state.mode && state.mode !== 'ALL' && state.mode !== 'NONE') params.apply_mode = state.mode;
    if (state.q && state.q.trim()) params.q = state.q.trim();

    // 截止日桶（C17）。四个"天数窗口"接口直接支持，服务端筛完再回来 ——
    // 这样计数是真的（`?deadline_within=7` 的 total 与 facets 的 within_7 是同一份定义）。
    // `expired / later / unknown / absent` 没有对应的接口参数，只能在本页 50 条里筛，
    // **页面上会明说"本页内"**，不冒充全量。
    if (DL_WINDOW[state.deadline]) params.deadline_within = DL_WINDOW[state.deadline];

    // 排序也交给接口（`deadline_asc` 是 B25 加的）：只在前端排序的话，
    // 一旦结果超过一页，"最上面那条最快截止"就只对当前这页成立。
    if (state.sort === 'deadline') params.sort = 'deadline_asc';

    const data = await liveOrFixture(
      () => apiGet('/api/notices', params),
      'notices.list.json'
    );

    // 防线：接口层已经在源头修掉重复 id（A 的 9184fbe：http/https 同一篇进两条），
    // 这里按 id 再去一次——真出问题也不会在页面上出现成对一模一样的卡片。
    const seen = new Set();
    const rows = (data.items || []).filter((r) => {
      if (seen.has(r.id)) return false;
      seen.add(r.id);
      return true;
    });
    state.data = data;
    state.rows = rows;
    return data;
  }

  /** 筛选条件一改就**重新问接口**（不是只筛本地那 50 条）。
   *  `reqSeq` 防抖：连打几个字、连点几个下拉，只认最后一次回来的那一份 ——
   *  否则慢的那个请求后到，会把新的结果盖掉，页面出现"筛的是 A、列的是 B"。 */
  let reqSeq = 0;
  async function refetch() {
    const seq = ++reqSeq;
    try {
      await fetchRows();
      if (seq !== reqSeq) return;
    } catch (e) {
      if (seq !== reqSeq) return;
      // 取不到就保留上一屏（不清空），但在计数行说清楚 —— 白屏比旧数据更坏
      el('result-count').textContent = '这次没取到（' + ((e && e.message) || e) + '），下面还是上一次的结果';
      return;
    }
    render();
  }

  /** 筛选项的来源：`GET /api/facets`（**全量**），不是当前这一页的 50 条。
   *
   *  为什么必须换：原来下拉框是 `countBy(state.rows)` 现算的 —— 第一页只有 5~9 所学校，
   *  于是"学校"下拉也只有 5~9 个选项，剩下的 200 多条公告根本筛不到。
   *  这个项目已经栽过 5 次"取一页就够了"，这是第 6 次，而且是同一个坑的另一种长相。
   *
   *  读不到（比如 `?api=fixtures` 离线）就退回现算 —— 有选项总比没有强，
   *  但页面上会因此少一些选项，那是离线演示的已知代价。 */
  async function loadFacets() {
    try {
      const d = await apiGet('/api/facets');
      state.facets = d && typeof d === 'object' ? d : null;
    } catch (e) {
      state.facets = null;
    }
    return state.facets;
  }

  /** 把 facets 的 `[{value,count}]` 转成 `<option>` 要的 `[{key,label}]`；
   *  facets 没有的字段（或无 facets）就用现算的兜底。 */
  function facetOptions(list, fallbackRows, fn) {
    if (Array.isArray(list) && list.length) {
      return list.map((x) => ({ key: String(x.value), label: String(x.value) + '（' + x.count + '）' }));
    }
    return countBy(fallbackRows, fn);
  }

  /* ---------- 深链参数（C3） ----------
   *
   * 为什么需要它：B1 之后 `/api/notices` **默认拒收 junk**，于是"整个单位全是 junk"的学校
   * 会从下拉框里整个消失（天津大学 39 条全 junk，facets 里已经没有了）。
   * 用户既选不到它，也就永远看不到"为什么它不见了"。
   *
   * 有了 `?school=天津大学`，这条路径才够得着，空态才有机会说那句话。
   * 顺带这也是个好用的功能：任何筛选组合都能直接发链接。
   */
  function applyUrlFilters() {
    let q;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    const sch = q.get('school');
    const uni = q.get('unit');
    const kw = q.get('q');
    const mode = q.get('mode');
    const dl = q.get('deadline');     // C17：「快截止」入口深链（?deadline=within_7）
    const sort = q.get('sort');       // C17：?sort=deadline -> 按最快截止排
    if (sch) state.school = sch;
    if (uni) state.unit = uni;
    if (kw) state.q = kw;
    if (mode) state.mode = mode;
    if (dl) state.deadline = dl;
    if (sort === 'deadline' || sort === 'publish') state.sort = sort;
  }

  async function boot() {
    try {
      applyUrlFilters();                 // 深链：?school=&unit=&q=&mode=（C3 需要它才够得着"全 junk 的单位"）
      const data = await fetchRows();

      markSource();
      renderStats(data, state.rows);
      // 先把工具栏画出来（用现算的兜底），再去取全量 facets 补上 —— 不为了筛选项把首屏卡住
      renderToolbar(state.rows);
      // facets 到了要**重画一次**：计数行里"这一档全库多少条"用的是 facets 的互斥桶，
      // 首屏那一次渲染时它还没到（不然 8-30 天档会显示成接口上限 24，而正确是 6）。
      loadFacets().then((f) => { if (f) { refreshToolbarCounts(state.rows); renderDeadlineSummary(); render(); } });
      render();
      el('state').hidden = true;

      // 新鲜度是**附加信息**：它读不到不该把公告列表整页打掉，所以单独一趟、失败就算了。
      getFreshness().then((fresh) => {
        state.fresh = fresh;
        const node = el('freshness');
        if (node && fresh) node.innerHTML = freshnessBarHTML(fresh);
        render();          // 重渲染一次，把「某校这次没抓到」标到卡片上
      });

      // live.js 发现后台数据变了会发这个事件。这里**保住筛选条件重拉**，
      // 而不是让用户点"刷新"然后丢掉他刚选好的学校/关键词。
      document.addEventListener('tuimian:data-updated', reload);
    } catch (err) {
      showError(el('state'), err);
    }
  }

  /** 后台数据更新后重拉：**筛选、排序、搜索词全部保留**。
   *  用户刚把"浙江大学 + 只看带附件"选好，不该因为后台抓了一轮就白选一次。 */
  async function reload() {
    // 同一时刻只跑一次（事件可能连发）
    if (state.reloading) return;
    state.reloading = true;
    try {
      await fetchRows();
      markSource();
      renderStats(state.data, state.rows);
      // 只刷新下拉里的**计数**，不重新绑定事件（重绑会让 change 触发两次）
      refreshToolbarCounts(state.rows);
      render();
    } catch (e) {
      // 重拉失败**不要**把已经显示好的列表换成错误页 —— 那些数据还是有效的，
      // 只是没能更新。在列表上方说明一句就够。
      const n = el('reload-msg');
      if (n) {
        n.textContent = '后台数据更新后重拉失败：' + (e && e.message ? e.message : e) +
          '　（下面显示的还是上一份，点浏览器刷新可重试）';
        n.hidden = false;
      }
    } finally {
      state.reloading = false;
    }
  }

  /* ---------- 统计 ---------- */

  function renderStats(data, rows) {
    const noDeadline = rows.filter((r) => !r.deadline).length;
    const withAtt = rows.filter((r) => r.has_attachments).length;
    const suspect = rows.filter((r) => r.extract && r.extract.confidence < 0.5).length;

    const cells = [
      { n: data.total, l: '抓到公告总数', cls: '' },
      { n: rows.length, l: '本页展示（接口上限 50）', cls: '' },
      { n: withAtt, l: '带附件', cls: 'hi' },
      { n: noDeadline, l: '未抽到截止日', cls: 'dim' },
      { n: suspect, l: '疑似（低置信）', cls: 'dim' }
    ].filter((c) => typeof c.n === 'number');

    el('stats').innerHTML = cells
      .map((c) => '<div class="stat ' + c.cls + '"><div class="n">' + c.n + '</div><div class="l">' + esc(c.l) + '</div></div>')
      .join('');
    el('stats-wrap').hidden = false;
  }

  /* ---------- 工具条 ---------- */

  function options(allLabel, values) {
    return ['<option value="ALL">' + esc(allLabel) + '</option>']
      .concat(values.map((v) => '<option value="' + esc(v.key) + '">' + esc(v.label) + '</option>'))
      .join('');
  }

  function countBy(rows, fn) {
    const m = new Map();
    rows.forEach((r) => {
      const k = fn(r);
      if (k === null || k === undefined || k === '') return;
      m.set(k, (m.get(k) || 0) + 1);
    });
    return Array.from(m.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([key, n]) => ({ key, label: key + '（' + n + '）' }));
  }

  /** 只重建下拉里的**选项和计数**，并把当前选中值放回去。
   *  reload 时用它 —— **不能**再调一次 renderToolbar，那会重复绑定 change 事件，
   *  于是用户选一次学校会触发两次 render（症状是"筛选闪一下"）。 */
  function refreshToolbarCounts(rows) {
    const keep = (id, value) => {
      const n = el(id);
      if (!n) return;
      n.value = value;
      // 之前选中的学校这一轮可能已经不在列表里了。
      // **但"不在列表里"有两种**：①它真的没了 ②它的记录这一轮全被判成 junk、被默认拒收了。
      // 旧代码一律退回「全部」—— 那样第 ② 种就变成了"用户看不见它，也不知道它为什么不见"，
      // 正好是 C3 要防的那件事。所以现在先把值补成一条选项挂住，让空态有机会解释。
      if (n.value !== value) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value + '（本期无收录）';
        n.appendChild(opt);
        n.value = value;
      }
    };
    const fill = (id, allLabel, list) => { const n = el(id); if (n) n.innerHTML = list; };

    // **优先用全量 facets**（26 所学校），读不到才退回当前页现算（5~9 所）
    const f = state.facets;
    fill('school', '全部学校', options('全部学校', facetOptions(f && f.schools, rows, (r) => r.school)));
    fill('unit', '全部单位', options('全部单位', facetOptions(f && f.units, rows, (r) => r.unit)));
    fill('mode', '全部方式', '<option value="ALL">全部方式</option>' +
      '<option value="推免">推免</option><option value="夏令营">夏令营</option>' +
      '<option value="预推免">预推免</option><option value="统考">统考</option>' +
      '<option value="NONE">未标注（' + (f && Array.isArray(f.apply_modes)
        ? f.apply_modes.filter((x) => !x.value).reduce((n, x) => n + (x.count || 0), 0)
        : rows.filter((r) => !r.apply_mode).length) + '）</option>');
    fill('deadline', '全部', deadlineOptions(f));

    keep('school', state.school);
    keep('unit', state.unit);
    keep('mode', state.mode);
    keep('deadline', state.deadline);
    // 选中值真的丢了（那一项没了）→ 同步 state，否则渲染用旧值筛出空列表
    state.school = el('school') ? el('school').value : 'ALL';
    state.unit = el('unit') ? el('unit').value : 'ALL';
    state.mode = el('mode') ? el('mode').value : 'ALL';
    state.deadline = el('deadline') ? el('deadline').value : 'ALL';
  }

  /** 「截止日」下拉：**标签和条数都来自 `/api/facets` 的 `deadline_exclusive_buckets`**。
   *
   *  为什么是 exclusive 不是 `deadline_buckets`：后者是累积的（1 天内 ⊂ 3 天内 ⊂ …），
   *  八个桶相加 2447，比全库 2419 多出 28 条。用户会拿这些数字做判断，
   *  一个"加起来对不上总数"的下拉就是在替我们制造怀疑。互斥版相加正好 2419。
   *
   *  另外：入口那条「7 天内截止」用的是窗口 `within_7`（= 1 + 2-3 + 4-7 三个互斥桶），
   *  它不在互斥列表里，所以选中的时候临时补一条选项，别让下拉显示成"全部"。
   */
  function deadlineOptions(f) {
    const list = (f && f.deadline_exclusive_buckets) || [];
    if (!list.length) {
      return '<option value="ALL">全部（服务端还没给截止日分桶）</option>';
    }
    const known = list.map((b) => b.value);
    let extra = '';
    if (state.deadline && state.deadline !== 'ALL' && known.indexOf(state.deadline) === -1) {
      const sum = list.filter((b) => ['within_1', 'within_2_3', 'within_4_7'].indexOf(b.value) >= 0)
        .reduce((n, b) => n + (b.count || 0), 0);
      const label = state.deadline === 'within_7' ? ('7 天内（共 ' + sum + '）') : state.deadline;
      extra = '<option value="' + esc(state.deadline) + '">' + esc(label) + '</option>';
    }
    return '<option value="ALL">全部截止日</option>' + extra + list.map((b) =>
      '<option value="' + esc(b.value) + '">' + esc(b.label || b.value) + '（' + (b.count || 0) + '）</option>'
    ).join('');
  }

  function renderToolbar(rows) {
    el('school').innerHTML = options('全部学校', countBy(rows, (r) => r.school));
    el('unit').innerHTML = options('全部单位', countBy(rows, (r) => r.unit));
    el('mode').innerHTML = '<option value="ALL">全部方式</option>' +
      '<option value="推免">推免</option><option value="夏令营">夏令营</option>' +
      '<option value="预推免">预推免</option><option value="统考">统考</option>' +
      '<option value="NONE">未标注（' + rows.filter((r) => !r.apply_mode).length + '）</option>';
    // 截止日这一栏要等 facets（全量条数才准）。先给一条占位，facets 到了再换。
    el('deadline').innerHTML = '<option value="ALL">全部截止日</option>';
    // 首次渲染用现算的兜底；facets 一到，refreshToolbarCounts() 会换成全量。

    // 深链进来的筛选条件要落到下拉框上（`?school=天津大学`），否则看起来是"没筛选"
    // 却一条都没有 —— 那种空白最容易被当成页面坏了。
    ['school', 'unit', 'mode'].forEach((id) => {
      const n = el(id);
      if (!n || !state[id]) return;
      n.value = state[id];
      if (n.value !== state[id]) {
        const opt = document.createElement('option');
        opt.value = state[id];
        opt.textContent = state[id] + '（本期无收录）';
        n.appendChild(opt);
        n.value = state[id];
      }
    });
    if (el('q')) el('q').value = state.q || '';
    if (el('sort')) el('sort').value = state.sort === 'deadline' ? 'deadline' : 'publish';

    el('school').addEventListener('change', (e) => { state.school = e.target.value; refetch(); });
    el('unit').addEventListener('change', (e) => { state.unit = e.target.value; refetch(); });
    el('mode').addEventListener('change', (e) => { state.mode = e.target.value; refetch(); });
    el('deadline').addEventListener('change', (e) => { state.deadline = e.target.value; refetch(); });
    // 关键词：**防抖 300ms 再问接口**（每敲一个字就发一次请求，既慢又会抢带宽）
    let qTimer = null;
    el('q').addEventListener('input', (e) => {
      state.q = e.target.value || '';
      if (qTimer) clearTimeout(qTimer);
      qTimer = setTimeout(() => refetch(), 300);
    });
    el('attach-only').addEventListener('change', (e) => { state.attachOnly = !!e.target.checked; render(); });
    // 排序也重新问一次接口：`deadline_asc` 由服务端排，"最上面那条最快截止"才对整页成立
    el('sort').addEventListener('change', (e) => { state.sort = e.target.value; refetch(); });

    el('toolbar').hidden = false;
  }

  /* ---------- 过滤 / 排序 ---------- */

  function filtered() {
    const q = state.q.trim().toLowerCase();
    return state.rows.filter((r) => {
      if (state.school !== 'ALL' && r.school !== state.school) return false;
      if (state.unit !== 'ALL' && r.unit !== state.unit) return false;
      if (state.mode === 'NONE') { if (r.apply_mode) return false; }
      else if (state.mode !== 'ALL' && r.apply_mode !== state.mode) return false;
      if (state.attachOnly && !r.has_attachments) return false;
      if (state.hasDeadline && !hasReadableDeadline(r)) return false;
      if (!inDeadlineBucket(r)) return false;
      if (!q) return true;
      const hay = [r.title, r.school, r.unit, r.board].concat(r.majors || [], r.tags || []).join(' ').toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  /* 截止日桶（C17）。
   *
   * 四个"天数窗口"已经在服务端筛过了（`?deadline_within=`），这里直接放行；
   * 其余四种只能按行的 `deadline` + 三态现算。**三态必须分开**：
   *   · 已截止   = state 是 found 且日期真的过了
   *   · 未读取   = 我们的 `unknown`（卡片上写「未能读取截止日，请点原文核对」）
   *   · 未公布   = `absent`（正文读了，确实没写）
   * 把后两种并进"已截止"就是替用户下了一个我们没有的结论 —— 这也是数据里最大的两堆
   * （525 + 1166 条），藏起来或并掉都会让"截止日未知占大头"这件事消失。
   */
  function inDeadlineBucket(r) {
    const b = state.deadline;
    if (!b || b === 'ALL') return true;
    const st = (r.field_trust && r.field_trust.states && r.field_trust.states.deadline) || null;
    if (b === 'absent') return st === 'absent';
    if (b === 'unknown') return st === 'unknown' || (!st && !r.deadline);

    const v = r.deadline ? String(r.deadline).slice(0, 10) : '';
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    if (!m) return false;
    const today = new Date();
    const t = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
    const d = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    const left = Math.round((d - t) / 86400000);

    // 互斥窗口：服务端只按上限给了（within_2_3 -> within 3 天），下界在这里切。
    // 7 天内 18 条、30 天内 24 条都在一页里，所以这一步是精确的。
    const range = DL_RANGE[b];
    if (range) return left >= range[0] && left <= range[1];
    if (b === 'within_7') return left >= 0 && left <= 7;   // 入口的窗口（三个互斥桶之和）
    if (b === 'expired') return left < 0;
    if (b === 'later') return left > 30;
    return true;
  }

  /** 当前截止日选择对应的**全库条数**（来自 facets 互斥桶）；算不出来返回 null。
   *  `within_7` 是入口那条窗口 = 1 天内 + 2-3 天 + 4-7 天。 */
  function expectedCount() {
    const list = (state.facets && state.facets.deadline_exclusive_buckets) || [];
    if (!list.length || !state.deadline || state.deadline === 'ALL') return null;
    if (state.deadline === 'within_7') {
      return list.filter((b) => ['within_1', 'within_2_3', 'within_4_7'].indexOf(b.value) >= 0)
        .reduce((n, b) => n + (b.count || 0), 0);
    }
    const hit = list.filter((b) => b.value === state.deadline)[0];
    return hit ? (hit.count || 0) : null;
  }

  /** 顶部那行「多少条没有可读的截止日」+「只看有截止日的」开关（C17 ③）。
   *
   *  数字**全部来自 `/api/facets` 的互斥桶**，所以是复算得出来的：
   *    absent 1166 + unknown 525 = 1691，占 2419 的 70%。
   *  这两句必须分开写清楚：
   *    · absent  = 学校确实没写（官网的实际情况）
   *    · unknown = 我们没读到（我们的问题，可以点原文核对）
   *  把两者混成一句"没有截止日"，等于把我们没做好的事说成学校没写。
   */
  function renderDeadlineSummary() {
    const box = el('deadline-summary');
    if (!box) return;
    const list = (state.facets && state.facets.deadline_exclusive_buckets) || [];
    if (!list.length) { box.hidden = true; return; }
    const get = (k) => (list.filter((b) => b.value === k)[0] || {}).count || 0;
    const total = list.reduce((n, b) => n + (b.count || 0), 0);
    const absent = get('absent');
    const unknown = get('unknown');
    const blind = absent + unknown;
    const pct = total ? Math.round((blind / total) * 100) : 0;
    const found = total - blind;           // 有截止日的（含已截止）

    box.hidden = false;
    box.innerHTML =
      '<p class="dl-summary-line">本期 <strong>' + total + '</strong> 条里 <strong>' + blind +
        '</strong> 条公告没有可读的截止日，占 <strong>' + pct + '%</strong> —— 这是官网的实际情况，我们不猜。' +
        '<span class="dl-summary-split">其中学校确实没写 <strong>' + absent +
        '</strong> 条（该公告未公布截止日），我们没读到 <strong>' + unknown +
        '</strong> 条（截止日未能读取，请点原文核对）。</span></p>' +
      '<label class="dl-toggle"><input type="checkbox" id="has-deadline"' + (state.hasDeadline ? ' checked' : '') + '>' +
        ' 只看有截止日的（' + found + ' 条）</label>' +
      (state.hasDeadline
        ? '<p class="dl-summary-note">这一档接口还没有服务端参数，列表里筛的是<strong>本页 ' +
          state.rows.length + ' 条</strong>；全库 ' + found + ' 条这个数字来自 facets。</p>'
        : '');

    const cb = el('has-deadline');
    if (cb) {
      cb.addEventListener('change', (e) => {
        state.hasDeadline = !!e.target.checked;
        renderDeadlineSummary();
        render();
      });
    }
  }

  /** 全库"有可读截止日"的条数 = 总数 - absent - unknown（来自 facets 互斥桶）。
   *  这个数**不靠本页统计**：本页 50 条里有没有截止日，和全库 728 条没关系。 */
  function foundCount() {
    const list = (state.facets && state.facets.deadline_exclusive_buckets) || [];
    if (!list.length) return null;
    return list.reduce((n, b) => n + ((b.value === 'absent' || b.value === 'unknown') ? 0 : (b.count || 0)), 0);
  }

  /** 「只看有截止日的」：三态里**只有 found 才算"有"**。
   *  absent（学校没写）和 unknown（我们没读到）本来就是这个开关要挡掉的那 70%，
   * 把它们算进来等于开关没作用 —— 也会把"我们没读到"洗成"有截止日"。 */
  function hasReadableDeadline(r) {
    const st = (r.field_trust && r.field_trust.states && r.field_trust.states.deadline) || null;
    if (st === 'absent' || st === 'unknown') return false;
    return !!r.deadline;
  }

  function sorted(rows) {
    const arr = rows.slice();
    if (state.sort === 'deadline') {
      // 有截止日的按日期升序在前；没抽到的一律沉底，它们「可能明天就截止」，
      // 不能混在确定有日期的那堆里假装可比。
      arr.sort((a, b) => {
        const da = a.deadline || null, db = b.deadline || null;
        if (da && db) return String(da).localeCompare(String(db));
        if (da) return -1;
        if (db) return 1;
        return String(b.publish_date || '').localeCompare(String(a.publish_date || ''));
      });
    } else {
      arr.sort((a, b) => String(b.publish_date || '').localeCompare(String(a.publish_date || '')));
    }
    return arr;
  }

  /* ---------- 渲染 ---------- */

  function render() {
    const rows = sorted(filtered());
    // 「共 N 条」的 N 是**接口给的 total**，不是这一页取回的条数。
    // 之前写的是本页条数（最多 50）：某个学校有 241 条时，页面会写「共 50 条」——
    // 那是把"我们只取了一页"说成了"它只有 50 条"。
    const apiTotal = (state.data && typeof state.data.total === 'number')
      ? state.data.total
      : state.rows.length;

    // 互斥窗口（2-3 天 / 4-7 天 / 8-30 天）走的是"服务端按上限取、本地按下界切"：
    // 接口 total 是那个**上限**的数量（within_3 = 7），用户选的却是互斥桶（2-3 天 = 4）。
    // 计数行必须报用户选的那个数，否则同一屏两个数字打架（下拉写 4、列表写共 7 条）。
    const expected = expectedCount();
    const shownTotal = (expected != null) ? expected : apiTotal;

    el('result-count').textContent = rows.length === shownTotal
      ? '共 ' + shownTotal + ' 条'
      : rows.length + ' / 共 ' + shownTotal + ' 条';

    // 「只看有截止日的」是本页内筛（接口还没有 has_deadline 参数），必须说出来。
    // 全库多少条在顶部那行里（facets 算的 2419 - 1691 = 728）。
    if (state.hasDeadline) {
      const found = foundCount();
      el('result-count').textContent = rows.length + ' / 共 ' + (found != null ? found : shownTotal) +
        ' 条（本页 ' + state.rows.length + ' 条内筛）';
    }

    // 截止日里"已截止 / 30 天以后 / 未读取 / 未公布"这四档**接口还没有参数**，
    // 只能在本页这 50 条里筛。不写这一句，用户会把"本页 3 条"当成"全库 3 条"。
    if (DL_PAGE_ONLY[state.deadline]) {
      el('result-count').textContent += '（本页 ' + state.rows.length + ' 条内筛；这一档的全量条数见下拉里的数字）';
    }

    if (rows.length) {
      el('results').innerHTML = rows.map(cardHTML).join('');
      return;
    }
    // 空了有两种完全不同的原因，先按常见的写，再去确认是不是"被暂不收录"那种。
    el('results').innerHTML = '<div class="state">没有符合条件的公告，换个关键词或把筛选调回「全部」。</div>';
    probeJunkSuppressed();
  }

  /* ---------- C3：一整个单位被 junk 掉时的空态 ----------
   *
   * B1 之后 `/api/notices` **默认拒收 `record_quality == 'junk'`**，于是会出现一种新的空：
   * 某个单位这期收进来的公告**全部是空壳**（正文过短 / 只是栏目页 / 标题就是站点名），
   * 默认查询一条都不返回。
   *
   * 这时候如果只说「没有符合条件的公告」，就是把责任推给用户 ——
   * 他会以为是自己筛错了，或者这所学校今年没招生。两句话都是错的。
   *
   * 所以：拿同一组筛选条件再查一次 `include_junk=1`，
   * **有剩就说明是被我们暂不收录的**，把条数和官网链接一起给出来。
   * 查不到、或者本身就是空库，就保留原来那句 —— **不编一个原因**。
   */
  async function probeJunkSuppressed() {
    const sch = state.school !== 'ALL' ? state.school : '';
    const uni = state.unit !== 'ALL' ? state.unit : '';
    const kw = (state.q || '').trim();
    if (!sch && !uni && !kw) return;        // 没有任何筛选却空 → 是另一种故事
    try {
      const d = await apiGet('/api/notices', {
        size: 1, meta: 1, include_junk: 1,
        school: sch || undefined, unit: uni || undefined, q: kw || undefined
      });
      const n = (d && typeof d.total === 'number') ? d.total : 0;
      if (!n) return;                        // 带上 junk 也是空的 → 别乱解释
      el('results').innerHTML = junkEmptyHTML(n, sch, uni, kw);
    } catch (e) {
      /* 探不到就保留原来那句。**宁可说得少，也不编一个原因。** */
    }
  }

  /** 该学校的官网 / 栏目页（来自 `/api/freshness` 的 `homepage`）。 */
  function homepageOf(school) {
    const list = (state.fresh && state.fresh.schools) || [];
    const hit = list.find((s) => s.school === school && s.homepage);
    return hit ? hit.homepage : '';
  }

  function junkEmptyHTML(n, sch, uni, kw) {
    const who = [sch, uni].filter(Boolean).join(' · ') || ('「' + kw + '」');
    const home = safeUrl(homepageOf(sch));
    return '<div class="state">' +
      '<h3>' + esc(who) + ' 在库里是空的 —— 但不是因为没发公告</h3>' +
      '<p>这个单位这一期收进来的公告，<strong>正文过短、或者就是栏目页</strong>' +
      '（标题只写了站点名，比如"天津大学研究生招生网"），已<strong>暂不收录</strong>' +
      '（共 <strong>' + n + '</strong> 条）。</p>' +
      '<p class="muted">判断依据是落盘字段 `record_quality = junk`（`body_lt_100` / ' +
      '`body_meta_only` / `title_is_site_name`），不是我们漏抓了 —— ' +
      '它们还在库里，只是不值得占推荐位。</p>' +
      (home
        ? '<p><a class="btn-ghost" href="' + esc(home) + '" target="_blank" rel="noopener noreferrer">点此查看原始列表 ↗</a></p>'
        : '<p class="muted">（这个单位没登记官网地址，从学校研究生院站点自己找一下。）</p>') +
      '</div>';
  }

  function cardHTML(r) {
    const url = safeUrl(r.url);
    const conf = r.extract && typeof r.extract.confidence === 'number' ? r.extract.confidence : null;
    const suspect = conf !== null && conf < 0.5;

    // 距今多久（C1）。这一页还没有四档切换条 ——
    // 四档的**计数**必须在全量 1119 条上算，而这一页一次只取 50 条
    // （`/api/notices` 目前没有日期参数，传 `since` / `days` 都被忽略，实测 total 恒为 1119）。
    // 用"本页 50 条"的计数去冒充全量，比不做更糟，所以这里**只加行级标记**，
    // 等 B 的 `/api/updates`、`/api/library` 落地再把切换条接上来。
    const days = ageDaysOf(r);
    const archived = days !== null && days >= ARCHIVE_DAYS;

    const meta = [
      '<span class="school">' + esc(r.school) + '</span>',
      schoolBadgeHTML(state.fresh, r.school),
      r.unit ? '<span class="sep">·</span><span>' + esc(r.unit) + '</span>' : '',
      r.publish_date ? '<span class="sep">·</span><span class="date">' + esc(r.publish_date) + '</span>' : '',
      ageBadgeHTML(r, days),
      deadlineHTML(r),
      r.apply_mode ? '<span class="pill-mode">' + esc(r.apply_mode) + '</span>' : '',
      attachmentsHTML(r),
      suspect ? '<span class="pill-suspect" title="抽取置信度 ' + conf + '，低于 0.5，字段可能有错">疑似</span>' : ''
    ].filter(Boolean).join('');

    const tags = [].concat(r.majors || [], r.tags || []).filter(Boolean).slice(0, 6);
    const tagHTML = tags.length
      ? '<div class="chips">' + tags.map((t) => '<span class="chip chip-plain">' + esc(t) + '</span>').join('') + '</div>'
      : '';

    const detailHref = 'detail.html?id=' + encodeURIComponent(r.id);
    const ext = url
      ? '<a class="ext" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">原文 ↗</a>'
      : '';

    return '<article class="ncard' + (archived ? ' is-archived' : '') + '">' +
      '<div class="meta">' + meta + '</div>' +
      '<h2 class="title"><a href="' + esc(detailHref) + '">' + esc(r.title) + '</a>' + ext + '</h2>' +
      // 半年以上的卡默认把标签收起来（不是删掉）：倒查的时候还要用。
      archiveBlockHTML(tagHTML, days, '展开历史详情（' + ageText(days) + '，只作参考）') +
    '</article>';
  }

  /* 截止日：**走 deadline.js 的三态渲染**，不再自己判。
   *
   * 原来这里是 `if (!r.deadline) return ''` —— 没截止日就整行不显示。
   * 那正是契约 §14.5 用 ⛔ 明令禁止的：`unknown`（我们没读到）和 `absent`（确实没有）
   * 被合并成"什么都不说"，用户会以为这条没有截止日，于是不去看原文。
   * 三态数据来自列表接口的 `field_trust.states.deadline`（要带 `?meta=1`）。 */
  function deadlineHTML(r) {
    return deadlineLineHTML(r);
  }

  function attachmentsHTML(r) {
    if (!r.has_attachments) return '';
    const n = typeof r.attachments_count === 'number' ? r.attachments_count : 0;
    const label = n > 0 ? '附件 ' + n : '有附件';
    return '<span class="pill-attach" title="正文可能就在附件里">' + esc(label) + '</span>';
  }

  boot();
})();
