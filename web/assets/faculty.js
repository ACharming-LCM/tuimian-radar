/* 导师页（智能体 C）
 * 数据源：contracts/fixtures/faculty.list.json（契约：api.md 第 8 节 / faculty.schema.json）
 * 两条硬要求：
 *   · research_areas 为空 → 显示「该导师页面没有研究方向」，不显示空标签，且不排在前面
 *   · email 为空 → 「未抽到邮箱，联系前请到主页核对」，不许留白
 */
(function () {
  'use strict';

  const NO_AREA_GROUP = '研究方向未标注';
  const IDENTITY_GROUP = '院士与特聘教授';

  const state = { rows: [], q: '', group: 'ALL', phdOnly: false };
  const el = (id) => document.getElementById(id);

  /* ---------- 数据 ---------- */

  /** 取**全部**导师，不是第一页。
   *
   *  早先这里固定 `size: 50`，于是页面头部写着「557 位导师」、下面只列 50 位 ——
   *  而且「可匹配 / 名录级」两格只能按这 50 条算，算出来是 36 / 14，差了一个数量级。
   *  这个项目在"取一页就够了"上已经栽过多次，这里是同一坑的又一次。
   *
   *  557 条不大（一次 50 条 → 12 个请求），拼起来渲染一次就好；
   *  样例模式（`?api=fixtures`）只有一份，不拼。 */
  async function fetchAllFaculty() {
    const first = await liveOrFixture(
      () => apiGet('/api/faculty', { size: 50, page: 1 }),
      'faculty.list.json'
    );
    if (!useLive()) return first;
    const total = typeof first.total === 'number' ? first.total : (first.items || []).length;
    const pages = Math.ceil(total / 50);
    if (pages <= 1 || !(first.items || []).length) return first;

    const rest = await Promise.all(
      Array.from({ length: pages - 1 }, (_, i) =>
        apiGet('/api/faculty', { size: 50, page: i + 2 }).catch(() => null))
    );
    const items = (first.items || []).concat(
      ...rest.filter(Boolean).map((r) => r.items || [])
    );
    // 按 id 去重：翻页期间数据若被日更重写，可能出现重复
    const seen = new Set();
    const uniq = items.filter((r) => (seen.has(r.id) ? false : (seen.add(r.id), true)));
    return Object.assign({}, first, { items: uniq, total: total });
  }

  async function boot() {
    try {
      const data = await liveOrFixture(
        () => fetchAllFaculty(),
        'faculty.list.json'
      );
      state.rows = (data.items || []).slice();

      markSource();
      renderStats(data, state.rows);
      renderToolbar(state.rows);
      render();
      el('state').hidden = true;
    } catch (err) {
      showError(el('state'), err);
    }
  }

  const areasOf = (r) => (r.research_areas || []).filter(Boolean);

  /* group_kind 是 A 新加的字段（faculty.schema.json）：
   *   机构 → 用于分组（凝聚态物理研究所 / 浙江近代物理中心 / 聚变理论与模拟中心）
   *   身份 → 用于徽标（中国科学院院士 / 特聘教授）；这些人没有机构信息，统一收进一组
   * 判据顺序的坑（「中国科学院院士」里含「学院」）A 已经在抓取侧处理了，前端只按字段走。 */
  function groupOf(r) {
    if (!areasOf(r).length) return NO_AREA_GROUP;      // 硬要求：没方向的一定沉底
    if (r.group_kind === '机构') return r.group || '未分机构';
    if (r.group_kind === '身份') return IDENTITY_GROUP;
    return r.group || '未分栏目';
  }

  /* 机构组排最前（找导师就是找方向对得上的机构），身份组其次，没方向的最后 */
  function groupRank(g) {
    if (g === NO_AREA_GROUP) return 2;
    if (g === IDENTITY_GROUP) return 1;
    return 0;
  }

  /* ---------- 头部统计 ---------- */

  function renderStats(data, rows) {
    const groups = new Set(rows.map((r) => r.group || '未分栏目'));
    const withAreas = rows.filter((r) => areasOf(r).length).length;
    const avgAreas = rows.length
      ? (rows.reduce((n, r) => n + areasOf(r).length, 0) / rows.length)
      : 0;
    const phd = rows.filter((r) => r.is_phd_supervisor === true).length;
    const noMail = rows.filter((r) => !r.email).length;
    // **可匹配 / 名录级**（契约新增 `matchable`）：
    // 这一格必须放出来。**557 位导师听起来很多，但能参与匹配的只有其中一部分** ——
    // 不把这件事写在页面上，用户会以为"搜到名字就能匹配"，而我们只会按方向筛。
    const matchable = rows.filter((r) => r.matchable === true).length;
    const roster = rows.filter((r) => r.matchable === false).length;

    const cells = [
      { n: typeof data.total === 'number' ? data.total : rows.length, l: '位导师', cls: '' },
      { n: matchable, l: '可匹配', cls: 'hi' },
      { n: roster, l: '名录级（不参与匹配）', cls: 'dim' },
      { n: groups.size, l: '个栏目', cls: '' },
      { n: avgAreas.toFixed(1), l: '人均研究方向', cls: 'hi' },
      { n: phd, l: '位博导', cls: '' },
      { n: rows.length - withAreas, l: '没写研究方向', cls: 'dim' },
      { n: noMail, l: '没抽到邮箱', cls: 'dim' }
    ].filter((c) => c.n !== undefined);

    el('stats').innerHTML = cells
      .map((c) => '<div class="stat ' + c.cls + '"><div class="n">' + esc(String(c.n)) + '</div><div class="l">' + esc(c.l) + '</div></div>')
      .join('');
    el('stats-wrap').hidden = false;
  }

  /* ---------- 工具条 ---------- */

  function renderToolbar(rows) {
    const counts = {};
    rows.forEach((r) => {
      const g = groupOf(r);
      counts[g] = (counts[g] || 0) + 1;
    });
    const order = Object.keys(counts).sort((a, b) => {
      if (groupRank(a) !== groupRank(b)) return groupRank(a) - groupRank(b);
      return counts[b] - counts[a];
    });
    const chips = [{ key: 'ALL', label: '全部栏目', n: rows.length }]
      .concat(order.map((g) => ({ key: g, label: g, n: counts[g] })));

    el('group-filters').innerHTML = chips.map((c) =>
      '<button type="button" class="fchip" data-group="' + esc(c.key) + '" aria-pressed="' +
      (c.key === state.group) + '">' + esc(c.label) + '<span class="c">' + c.n + '</span></button>'
    ).join('');

    el('group-filters').addEventListener('click', (e) => {
      const btn = e.target.closest('.fchip');
      if (!btn) return;
      state.group = btn.dataset.group;
      el('group-filters').querySelectorAll('.fchip').forEach((b) => {
        b.setAttribute('aria-pressed', String(b.dataset.group === state.group));
      });
      render();
    });

    el('q').addEventListener('input', (e) => {
      state.q = e.target.value || '';
      render();
    });

    el('phd-only').addEventListener('change', (e) => {
      state.phdOnly = !!e.target.checked;
      render();
    });

    el('toolbar').hidden = false;
  }

  /* ---------- 过滤与排序 ---------- */

  function filtered() {
    const q = state.q.trim().toLowerCase();
    return state.rows.filter((r) => {
      if (state.group !== 'ALL' && groupOf(r) !== state.group) return false;
      if (state.phdOnly && r.is_phd_supervisor !== true) return false;
      if (!q) return true;
      const hay = [r.name, r.title, r.group, r.unit, r.school].concat(areasOf(r)).join(' ').toLowerCase();
      return hay.indexOf(q) >= 0;
    });
  }

  /* 硬要求：没有研究方向的沉到后面 —— 没有方向就无法判断契合度 */
  function sortRows(rows) {
    return rows.slice().sort((a, b) => {
      const ea = areasOf(a).length ? 0 : 1;
      const eb = areasOf(b).length ? 0 : 1;
      if (ea !== eb) return ea - eb;
      return String(a.name || '').localeCompare(String(b.name || ''), 'zh');
    });
  }

  /* ---------- 渲染 ---------- */

  function render() {
    const rows = sortRows(filtered());
    const total = state.rows.length;

    el('result-count').textContent = rows.length === total
      ? '共 ' + total + ' 位导师'
      : rows.length + ' / ' + total + ' 位导师';

    if (!rows.length) {
      el('results').innerHTML = '<div class="state">没有匹配的导师，换个关键词或点「全部栏目」。</div>';
      return;
    }

    const groups = new Map();
    rows.forEach((r) => {
      const g = groupOf(r);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(r);
    });

    // 机构组在前、身份组其次、没方向的最后；同类按人数降序
    const order = Array.from(groups.keys()).sort((a, b) => {
      if (groupRank(a) !== groupRank(b)) return groupRank(a) - groupRank(b);
      return groups.get(b).length - groups.get(a).length;
    });

    el('results').innerHTML = order.map((g) => {
      const list = groups.get(g);
      return '<h2 class="group-title">' + esc(g) +
        '<span class="group-count">' + list.length + ' 人</span></h2>' +
        list.map(cardHTML).join('');
    }).join('');
  }

  function cardHTML(r) {
    const areas = areasOf(r);

    const areaHTML = areas.length
      ? '<div class="chips">' + areas.map((a) => '<span class="chip chip-plain">' + esc(a) + '</span>').join('') + '</div>'
      : '<p class="no-area">该导师页面没有研究方向' +
        '<span class="no-area-hint">—— 没法判断是否契合，所以这一条排在后面</span></p>';

    // 硬要求：不许留白。
    // 手机上还要多想一步：mailto: 在手机上会切到邮件 App，
    // 但很多人的习惯是**先把邮箱复制走**（贴到自己的备忘 / 微信 / 电脑上再写）。
    // 所以邮箱地址直接显示出来，旁边给一个真的能用的复制按钮。
    const mailHTML = r.email
      ? '<a class="fmail" href="mailto:' + esc(r.email) + '">' + esc(r.email) + '</a>' +
        '<button type="button" class="copy-btn" data-copy="' + esc(r.email) + '" ' +
          'aria-label="复制邮箱 ' + esc(r.email) + '">复制</button>'
      : '<span class="fmail-missing">未抽到邮箱，联系前请到主页核对</span>';

    const home = safeUrl(r.homepage);
    const homeHTML = home
      ? '<a class="fhome" href="' + esc(home) + '" target="_blank" rel="noopener noreferrer">个人主页 ↗</a>'
      : '';

    // schema 写明：null 表示名单没写，不等于 false，所以不能默认当"不是博导"
    let phdHTML = '';
    if (r.is_phd_supervisor === true) phdHTML = '<span class="pill-phd">博导</span>';
    else if (r.is_phd_supervisor === false) phdHTML = '<span class="pill-unknown">非博导</span>';
    else if (r.is_phd_supervisor === null || r.is_phd_supervisor === undefined) {
      phdHTML = '<span class="pill-unknown" title="名单页没写，不代表不是">博导身份未标注</span>';
    }

    // 身份（院士 / 特聘教授）只当徽标用，不当分组依据
    const identityHTML = (r.group_kind === '身份' && r.group)
      ? '<span class="pill-identity" title="名单页标注的收录身份，不是研究所">' + esc(r.group) + '</span>'
      : '';

    // **名录级徽标**（契约 `matchable=false`）。
    // 为什么不干脆把这些条目藏起来：它们是**真数据**，能查到"这个学院有哪些老师"，
    // 藏掉等于把信息删了。但也不能让人误以为能按方向匹配 —— 所以标出来。
    // 三态：true 不加徽标（大多数，别刷屏）、false 加、缺字段**不加**（老数据不撒谎说它不可匹配）。
    const rosterHTML = r.matchable === false
      ? '<span class="pill-roster" title="这份名单只有姓名/院系，没有研究方向 —— ' +
        '能查到人，但不能按方向匹配排序">名录级：只展示、不参与匹配排序</span>'
      : '';

    const bits = [r.school, r.unit, r.title].filter(Boolean).map(esc);
    if (!areas.length && r.group) bits.push('原栏目 ' + esc(r.group));
    const metaBits = bits.join(' · ');

    return '' +
      '<article class="fcard">' +
        '<div class="fcard-head">' +
          '<span class="fname">' + esc(r.name) + '</span>' +
          identityHTML +
          phdHTML +
          rosterHTML +
        '</div>' +
        '<div class="fmeta">' + metaBits + '</div>' +
        areaHTML +
        '<div class="fcontact">' + mailHTML + homeHTML + '</div>' +
      '</article>';
  }

  boot();
})();
