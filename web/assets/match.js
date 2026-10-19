/* 匹配结果页（智能体 C）
 * 数据源：contracts/fixtures/match.response.json
 * 三条硬要求：deadline 为 null 不猜日期 / confidence<0.5 标疑似 / warnings 全量显示
 */
(function () {
  'use strict';

  const state = {
    match: null,
    faculty: null,
    profile: null,      // 当前使用的画像（用户自己填的，或示例）
    bucket: 'ALL',
    tier: (typeof DEFAULT_TIER === 'string' ? DEFAULT_TIER : 'month'),   // 时效四档，默认「本月」
    sort: 'score',
    fresh: null         // GET /api/freshness：「数据是什么时候的」
  };

  const el = (id) => document.getElementById(id);

  /* ---------- 数据 ---------- */

  async function boot() {
    try {
      state.profile = loadProfile();
      // 两个接口：POST /api/match（公告）+ POST /api/faculty/match（导师）。
      // 契约第 9 节：score / days_left 依赖当天日期，验收只比形状和不变量，不比数字。
      const [match, faculty] = await Promise.all([
        loadMatch(state.profile),
        loadFacultyMatch(state.profile).catch(() => null)
      ]);
      state.match = match;
      state.faculty = faculty;

      markSource();
      renderProfile(state.profile);
      renderProfileForm(state.profile, false);

      const edit = el('profile-edit');
      if (edit) {
        edit.addEventListener('click', () => {
          const node = el('profile-form');
          if (!node) return;
          if (!node.innerHTML) { renderProfileForm(state.profile, true); return; }
          node.hidden = !node.hidden;
          edit.textContent = node.hidden ? '修改背景' : '收起';
        });
      }

      renderStats(match);
      renderToolbar(match.items || []);
      render();
      renderFacultySide(faculty);
      el('state').hidden = true;

      // 「每天更新」在首页就要看得见，而且是**可验证的**（数据时间 + 覆盖学校数）。
      // 读不到就整块不显示——附加信息不该影响主内容。
      getFreshness().then((fresh) => {
        state.fresh = fresh;
        const node = el('freshness');
        if (node && fresh) node.innerHTML = freshnessBarHTML(fresh);
        render();
      });
    } catch (err) {
      showError(el('state'), err);
    }
  }

  /* ---------- 画像：用户自己填，或者用示例 ---------- */

  const splitList = (s) => String(s == null ? '' : s)
    .split(/[、,，;；\s]+/).map((x) => x.trim()).filter(Boolean);
  const val = (id) => { const n = el(id); return n && n.value != null ? String(n.value).trim() : ''; };
  const numOr = (v) => (v === null || v === undefined ? '' : String(v));

  /* loadProfile / saveProfile 在 common.js 里（套磁信页也要用同一份画像） */

  const loadMatch = (p) => liveOrFixture(() => apiPost('/api/match', { limit: 30 }, p), 'match.response.json');
  const loadFacultyMatch = (p) => liveOrFixture(() => apiPost('/api/faculty/match', { limit: 30 }, p), 'faculty.match.json');

  /* 表单：A 点名的五个"够用"字段（专业 / 成绩排名 / 英语 / 科研经历 / 目标学位）放主区；
   * 其余放「更多设置」并给示例默认值 —— 尤其是 wanted_modes，
   * 它是后端做硬过滤的依据，缺了会把结果直接筛空。 */
  function renderProfileForm(p, open) {
    const node = el('profile-form');
    if (!node) return;

    const boxes = (name, values, selected) => values.map((v) =>
      '<label class="chip-check"><input type="checkbox" name="' + name + '" value="' + esc(v) + '"' +
      (selected.indexOf(v) >= 0 ? ' checked' : '') + '>' + esc(v) + '</label>').join('');

    node.innerHTML =
      '<div class="pf-grid">' +
        '<label>学校<input id="pf-school" type="text" value="' + esc(p.school || '') + '"></label>' +
        '<label>目标入学年<input id="pf-year" type="number" value="' + esc(numOr(p.target_entry_year)) + '"></label>' +
        '<label>成绩排名<input id="pf-rank" type="number" value="' + esc(numOr(p.gpa_rank && p.gpa_rank.rank)) + '"></label>' +
        '<label>专业总人数<input id="pf-total" type="number" value="' + esc(numOr(p.gpa_rank && p.gpa_rank.total)) + '"></label>' +
        '<label>英语 CET-6<input id="pf-cet6" type="number" value="' + esc(numOr(p.english && p.english.cet6)) + '"></label>' +
      '</div>' +
      '<label class="pf-wide">专业方向（逗号分隔，同义词自己写全，如 物理学、力学、应用物理）' +
        '<input id="pf-majors" type="text" value="' + esc((p.majors || []).join('、')) + '"></label>' +
      '<label class="pf-wide">科研经历（一行一条）' +
        '<textarea id="pf-research" rows="2">' + esc((p.research || []).join('\n')) + '</textarea></label>' +
      '<div class="pf-chips"><span class="pf-label">目标学位</span>' +
        boxes('pf-levels', ['直博', '博士', '硕士'], p.target_levels || []) + '</div>' +
      '<details class="pf-more"><summary>更多设置（不填就用示例值）</summary>' +
        '<label class="pf-wide">意向招生方式（推免 / 预推免 / 夏令营）' +
          '<input id="pf-modes" type="text" value="' + esc((p.wanted_modes || []).join('、')) + '"></label>' +
        '<label class="pf-wide">兴趣关键词（导师匹配用，如 超导、量子）' +
          '<input id="pf-keywords" type="text" value="' + esc((p.interest_keywords || []).join('、')) + '"></label>' +
        '<label class="pf-wide">意向学校（逗号分隔）' +
          '<input id="pf-schools" type="text" value="' + esc((p.preferred_schools || []).join('、')) + '"></label>' +
        '<label class="pf-wide">意向单位（逗号分隔）' +
          '<input id="pf-units" type="text" value="' + esc((p.preferred_units || []).join('、')) + '"></label>' +
      '</details>' +
      '<div class="pf-actions">' +
        '<button type="button" class="btn-primary" id="pf-submit">用这个背景重新匹配</button>' +
        '<button type="button" class="btn-ghost" id="pf-reset">恢复示例背景</button>' +
        '<span class="pf-hint">只存在你自己的浏览器里，不会上传到别处。</span>' +
      '</div>';

    node.hidden = !open;
    const edit = el('profile-edit');
    if (edit) edit.textContent = open ? '收起' : '修改背景';

    el('pf-submit').addEventListener('click', () => applyProfile(profileFromForm(p)));
    el('pf-reset').addEventListener('click', () => applyProfile(Object.assign({}, DEMO_PROFILE)));
  }

  function checkedBoxes(name) {
    const node = el('profile-form');
    const out = [];
    if (node && node.querySelectorAll) {
      node.querySelectorAll('input[name="' + name + '"]:checked').forEach((i) => out.push(i.value));
    }
    return out;
  }

  function profileFromForm(base) {
    const p = Object.assign({}, base);
    p.name = '现场填写的背景';

    if (val('pf-school')) p.school = val('pf-school');
    const year = parseInt(val('pf-year'), 10);
    if (!isNaN(year)) p.target_entry_year = year;

    const majors = splitList(val('pf-majors'));
    if (majors.length) p.majors = majors;

    const rank = parseInt(val('pf-rank'), 10);
    const total = parseInt(val('pf-total'), 10);
    if (!isNaN(rank) && !isNaN(total) && total > 0) p.gpa_rank = { rank: rank, total: total };

    const cet6 = parseInt(val('pf-cet6'), 10);
    p.english = Object.assign({}, p.english, isNaN(cet6) ? {} : { cet6: cet6 });

    const research = String(val('pf-research')).split(/\n+/).map((x) => x.trim()).filter(Boolean);
    if (research.length) p.research = research;

    const levels = checkedBoxes('pf-levels');
    if (levels.length) p.target_levels = levels;

    const modes = splitList(val('pf-modes'));
    if (modes.length) p.wanted_modes = modes;
    const kw = splitList(val('pf-keywords'));
    if (kw.length) p.interest_keywords = kw;
    const schools = splitList(val('pf-schools'));
    if (schools.length) p.preferred_schools = schools;
    const units = splitList(val('pf-units'));
    if (units.length) p.preferred_units = units;
    return p;
  }

  /** 换了背景就重新问一次后端 —— 演示第 2 步「现场填一个真实背景」的核心动作。 */
  async function applyProfile(next) {
    state.profile = next;
    saveProfile(next);
    renderProfile(next);
    renderProfileForm(next, false);
    el('results').innerHTML = '<div class="state">正在用这份背景重新匹配…</div>';
    try {
      const [match, faculty] = await Promise.all([
        loadMatch(next),
        loadFacultyMatch(next).catch(() => null)
      ]);
      state.match = match;
      state.faculty = faculty;
      markSource();
      renderStats(match);
      renderToolbar(match.items || []);
      render();
      renderFacultySide(faculty);
    } catch (err) {
      showError(el('results'), err);
    }
  }

  /* 同一屏给出「该报的项目 + 该联系的导师」。侧栏只放前 6 位，
   * 全部 30 位在导师页——这里要的是"一眼看到方向对得上谁"，不是替代导师页。 */
  function renderFacultySide(faculty) {
    const node = el('faculty-side');
    if (!node) return;

    if (!faculty || !(faculty.items || []).length) {
      node.innerHTML = '<div class="side-head"><span class="side-title">对口导师</span></div>' +
        '<p class="side-empty">这一轮没读到导师匹配数据。导师页本身不受影响。</p>';
      node.hidden = false;
      return;
    }

    const top = faculty.items.slice().sort((a, b) => num(b.score) - num(a.score)).slice(0, 6);
    const total = typeof faculty.total === 'number' ? faculty.total : faculty.items.length;

    node.innerHTML =
      '<div class="side-head">' +
        '<span class="side-title">对口导师</span>' +
        '<span class="side-count">前 ' + top.length + ' / ' + total + '</span>' +
      '</div>' +
      '<p class="side-note">按画像里的 <code>interest_keywords</code> 命中的研究方向排序</p>' +
      '<ul class="side-list">' + top.map(sideFacultyHTML).join('') + '</ul>' +
      '<a class="side-more" href="faculty.html">看全部导师 →</a>';
    node.hidden = false;
  }

  /* 走查 #8：`group` 有时是身份（特聘教授 / 中国科学院院士），跟职称拼在一起会变成
   * 「特聘教授 · 教授」这种读着很怪的东西。身份类就只显示身份。 */
  const IDENTITY_WORDS = ['院士', '特聘', '学者', '人才', '杰青', '优青', '讲师'];
  function sideMeta(f) {
    const g = f.group || '';
    const t = f.title || '';
    if (g && IDENTITY_WORDS.some((w) => g.indexOf(w) >= 0)) return g;
    if (g && t && g.indexOf(t) < 0) return g + ' · ' + t;
    return g || t;
  }

  function sideFacultyHTML(f) {
    const hits = (f.hits || []).filter(Boolean);
    const hitHTML = hits.length
      ? '<div class="side-hits">命中：' + hits.map(esc).join('、') + '</div>'
      : '<div class="side-hits side-hits-empty">方向未直接命中，按单位和层次匹配</div>';
    const phd = f.is_phd_supervisor === true ? '<span class="pill-phd">博导</span>' : '';
    // 「发邮件」只是入口的一半：手机上更常用的是**先把邮箱复制走**。
    // 原来这里连地址都不显示，只写「发邮件」，那就没法复制。
    const mail = f.email
      ? '<a class="side-mail" href="mailto:' + esc(f.email) + '">发邮件</a>' +
        '<button type="button" class="copy-btn" data-copy="' + esc(f.email) + '" ' +
          'title="' + esc(f.email) + '" aria-label="复制邮箱 ' + esc(f.email) + '">复制邮箱</button>' +
        '<span class="side-addr">' + esc(f.email) + '</span>'
      : '<span class="side-mail-missing">未抽到邮箱</span>';
    return '<li>' +
      '<div class="side-name">' + esc(f.name) + phd +
        '<span class="side-score">' + num(f.score).toFixed(2) + '</span></div>' +
      '<div class="side-meta">' + esc(sideMeta(f)) + '</div>' +
      hitHTML +
      '<div class="side-actions">' + mail + '</div>' +
    '</li>';
  }

  /* ---------- 头部信息 ---------- */

  function renderProfile(p) {
    if (!p) return;
    const eng = [];
    if (p.english) {
      if (p.english.cet6) eng.push('CET-6 ' + p.english.cet6);
      if (p.english.toefl) eng.push('托福 ' + p.english.toefl);
      if (p.english.ielts) eng.push('雅思 ' + p.english.ielts);
    }
    const rows = [
      ['学校', p.school],
      ['专业方向', (p.majors || []).join('、')],
      ['年级', p.grade],
      ['目标入学年', p.target_entry_year],
      ['成绩排名', p.gpa_rank ? p.gpa_rank.rank + ' / ' + p.gpa_rank.total : null],
      ['英语', eng.length ? eng.join('，') : '未填（涉及语言要求的公告会进提醒）'],
      ['科研经历', (p.research || []).join('；')],
      ['目标层次', (p.target_levels || []).join('、')],
      ['意向招生方式', (p.wanted_modes || []).join('、')],
      ['意向单位', (p.preferred_units || []).join('、')],
      ['意向学校', (p.preferred_schools || []).join('、')]
    ];
    el('profile').innerHTML = rows
      .filter((r) => r[1] !== null && r[1] !== undefined && r[1] !== '')
      .map((r) => '<div class="row"><span class="k">' + esc(r[0]) + '</span><span class="v">' + esc(r[1]) + '</span></div>')
      .join('');
    el('profile-wrap').hidden = false;
  }

  function renderStats(match) {
    const s = match.stats || {};
    const shown = (match.items || []).length;
    const cells = [
      { n: s.total, l: '抓到的公告', cls: '' },
      { n: s.dropped_expired, l: '已过期剔除', cls: 'dim' },
      { n: s.dropped_mode, l: '类型不符剔除', cls: 'dim' },
      { n: s.scored, l: '参与打分', cls: '' },
      { n: shown, l: '本页展示', cls: 'hi' }
    ].filter((c) => typeof c.n === 'number');

    el('stats').innerHTML = cells
      .map((c) => '<div class="stat ' + c.cls + '"><div class="n">' + c.n + '</div><div class="l">' + esc(c.l) + '</div></div>')
      .join('');
    el('stats-wrap').hidden = false;
  }

  /* ---------- 工具条 ---------- */

  function renderToolbar(items) {
    // ---- 时效四档（C1）----
    // 计数在**完整结果集**上算（`/api/match` 不分页，items 就是全量），
    // 所以这个数字和点下去能出来的条数一定对得上 —— 这条对账关系不能破。
    // 默认档是「本月」：首屏不许出现 30 天以上的记录。
    const counts = tierCounts(items);
    const tierHost = el('tier-filters');
    if (tierHost) {
      tierHost.innerHTML = tierChipsHTML(counts, state.tier);
      // 用**事件委托**挂在容器上：内部 innerHTML 换掉时监听不会丢，也不会重复挂。
      if (!tierHost.dataset.wired) {
        tierHost.dataset.wired = '1';
        tierHost.addEventListener('click', (e) => {
          const btn = e.target.closest('.fchip');
          if (!btn || !btn.dataset.tier) return;
          state.tier = btn.dataset.tier;
          tierHost.innerHTML = tierChipsHTML(counts, state.tier);
          render();
        });
      }
    }

    const bucketCounts = {};
    items.forEach((it) => { bucketCounts[it.bucket] = (bucketCounts[it.bucket] || 0) + 1; });

    const order = ['立即行动', '重点关注', '可规划', '需人工确认', '已过期'];
    const known = order.filter((b) => bucketCounts[b]);
    const unknown = Object.keys(bucketCounts).filter((b) => order.indexOf(b) < 0);
    const chips = [{ key: 'ALL', label: '全部', n: items.length }]
      .concat(known.concat(unknown).map((b) => ({ key: b, label: b, n: bucketCounts[b] })));

    el('bucket-filters').innerHTML = chips.map((c) =>
      '<button type="button" class="fchip" data-bucket="' + esc(c.key) + '" aria-pressed="' +
      (c.key === state.bucket) + '">' + esc(c.label) + '<span class="c">' + c.n + '</span></button>'
    ).join('');

    el('bucket-filters').addEventListener('click', (e) => {
      const btn = e.target.closest('.fchip');
      if (!btn) return;
      state.bucket = btn.dataset.bucket;
      el('bucket-filters').querySelectorAll('.fchip').forEach((b) => {
        b.setAttribute('aria-pressed', String(b.dataset.bucket === state.bucket));
      });
      render();
    });

    el('sort').value = state.sort;
    el('sort').addEventListener('change', (e) => {
      state.sort = e.target.value;
      render();
    });

    el('toolbar').hidden = false;
  }

  /* ---------- 列表 ---------- */

  function visibleItems() {
    let items = (state.match.items || []).slice();
    if (state.bucket !== 'ALL') items = items.filter((it) => it.bucket === state.bucket);

    // 时效档（C1）：默认「本月」= ≤30 天。**首屏不许出现 30 天以上的记录** ——
    // 这不是排序问题，是"你打开第一屏看到的东西得是今天能用的"。
    if (state.tier) items = items.filter((it) => inTier(ageDaysOf(it), state.tier));

    if (state.sort === 'score') {
      items.sort((a, b) => num(b.score) - num(a.score));
    } else if (state.sort === 'publish') {
      items.sort((a, b) => String(b.publish_date || '').localeCompare(String(a.publish_date || '')));
    } else if (state.sort === 'deadline') {
      // 有截止日的按日期升序在前，没抽到的一律沉底（按契合度排），
      // 因为它们「可能明天就截止」，不能混在确定有日期的那堆里假装可比。
      items.sort((a, b) => {
        const da = a.deadline || null, db = b.deadline || null;
        if (da && db) return String(da).localeCompare(String(db));
        if (da) return -1;
        if (db) return 1;
        return num(b.score) - num(a.score);
      });
    }
    return items;
  }

  function num(v) { return typeof v === 'number' ? v : 0; }

  function render() {
    const items = visibleItems();
    const total = (state.match.items || []).length;
    const tierLabel = (TIERS.find((t) => t.key === state.tier) || {}).label || '全部';
    const scope = (state.bucket === 'ALL' ? '' : state.bucket + ' · ') + '时效：' + tierLabel;

    el('result-count').textContent = items.length + ' / ' + total + ' 条匹配结果（' + scope + '）';

    // 空态要说人话：**为什么空**、**下一步做什么**。
    // 时效档把结果筛空了和"一条都没匹配到"是两件事，不能共用一句话 ——
    // 用户切到「最近 3 天」大概率是空的（同一天发公告不是常态），
    // 这时候说"没有匹配到公告"会让他以为产品坏了。
    el('results').innerHTML = items.length
      ? items.map(cardHTML).join('')
      : (total === 0
        ? '<div class="state">' +
            '<p><strong>这一轮没有匹配到公告。</strong></p>' +
            '<p class="muted">常见原因两个：一是「意向单位 / 招生方式 / 目标学位」收得太窄，' +
            '后端按这几项做硬过滤；二是这个方向库里确实还没抓到。' +
            '点右上角「修改背景」放宽一两项再匹配一次，或者先去 ' +
            '<a href="notices.html">公告列表</a> 看看这一轮抓到了什么。</p>' +
          '</div>'
        // C7：最近 3 天为空时说**实情**（"官网近 3 天没有新的 2027 招生公告，下面是本周的 N 条"），
        // 不许写"暂无数据" —— 那会让人以为整个库是空的。
        : (state.tier === 'd3'
          ? d3EmptyHTML(tierCounts(state.match.items || []))
          : '<div class="state">' +
              '<p><strong>「' + esc(tierLabel) + '」这个时效档里没有结果。</strong></p>' +
              '<p class="muted">一共 ' + total + ' 条匹配结果，只是都不在这个时间范围内 —— ' +
              '切到「本月」或「资料库」就能看到。<strong>我们不把旧公告混进「今天该看的」里</strong>，' +
              '所以这里宁可空着。</p>' +
            '</div>'));

    wireToggles(el('results'));
  }

  function cardHTML(it) {
    const bm = bucketMeta(it.bucket);
    const conf = (it.extract && typeof it.extract.confidence === 'number') ? it.extract.confidence : null;
    const suspect = conf !== null && conf < 0.5;
    const url = safeUrl(it.url);

    const title = url
      ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(it.title) + '</a>'
      : '<span>' + esc(it.title) + '</span>';

    // 距今多久（C1）：**每张卡都要能一眼看出新旧**。
    // 放在发布日后面，日期给出"哪一天"，它给出"多少天前" —— 两个都要，
    // 因为 2026-09-30 这种写法要用户在脑子里做减法，而这是第一屏最该省掉的动作。
    const days = ageDaysOf(it);
    const archived = days !== null && days >= ARCHIVE_DAYS;

    const meta = [
      '<span class="school">' + esc(it.school) + '</span>',
      schoolBadgeHTML(state.fresh, it.school),
      it.unit ? '<span class="sep">·</span><span>' + esc(it.unit) + '</span>' : '',
      it.publish_date ? '<span class="sep">·</span><span class="date">' + esc(it.publish_date) + '</span>' : '',
      ageBadgeHTML(it, days),
      deadlineHTML(it),
      attachmentsHTML(it),
      suspect
        ? '<span class="pill-suspect" title="抽取置信度 ' + conf + '，低于 0.5，字段可能有错">疑似</span>'
        : ''
    ].filter(Boolean).join('');

    const reasons = (it.reasons || []).filter(Boolean);
    const reasonHTML = reasons.length
      ? '<div class="chips">' + reasons.map((r) => '<span class="chip">' + esc(r) + '</span>').join('') + '</div>'
      : '';

    const warns = (it.warnings || []).filter(Boolean);
    const warnHTML = warns.length
      ? '<ul class="warns">' + warns.map((w) => '<li>' + esc(w) + '</li>').join('') + '</ul>'
      : '<p class="no-warn">这条没有触发自动提醒——不等于没有门槛，条件仍以原文为准。</p>';

    const raw = num(it.raw_score);
    const score = num(it.score);
    const rawLine = (raw && Math.abs(raw - score) > 0.005)
      ? '<div class="score-raw" title="原始分 ' + raw.toFixed(2) + '，按抽取置信度降权后得 ' + score.toFixed(2) + '">原始 ' + raw.toFixed(2) + '</div>'
      : '';

    return '' +
      '<article class="card' + (archived ? ' is-archived' : '') + '">' +
        '<div class="score-col">' +
          '<div class="score-num">' + score.toFixed(2) + '</div>' +
          '<div class="score-cap">契合度</div>' +
          '<div class="bar"><i style="width:' + Math.round(Math.min(1, Math.max(0, score)) * 100) + '%"></i></div>' +
          rawLine +
          '<div class="badge ' + bm.cls + '" title="' + esc(bm.hint) + '">' + esc(it.bucket || '未分类') + '</div>' +
        '</div>' +
        '<div class="body-col">' +
          '<div class="meta">' + meta + '</div>' +
          '<h2 class="title">' + title + '</h2>' +
          '<button type="button" class="toggle">展开完整标题</button>' +
          // 超过 180 天：理由和提醒默认收起来。**不是删掉** ——
          // 这条公告"为什么被匹配到"仍然可查，只是不该在首屏占位置。
          archiveBlockHTML(reasonHTML + warnHTML, days,
            '展开历史详情（这条是 ' + ageText(days) + ' 的公告，只作参考）') +
        '</div>' +
      '</article>';
  }

  /* 抽到截止日才显示；抽不到就什么都不显示 ——
   * 不在卡片上编一个日期，也不在每张卡片上重复同一句"待确认"（满屏都是等于没说）。
   * 详情页里会写明「公告未明确截止日期，请查看原文」。 */
  /* 截止日：走 deadline.js 的三态渲染（不再 `if (!it.deadline) return ''`）。
   *
   * 注意匹配页有个真实的缺口：`/api/match` 的 item **不带** `field_state` / `field_trust`，
   * 所以这里只能按「未能读取」说 —— 那是最安全的一句（它让人去点原文）。
   * 要区分「确实没有」得让后端在 match 的 item 里补 `field_trust`，已写进 journal 转给 B。
   * `days_left` 仍然用接口给的，不在前端拿"今天"重算（重算会和后端的数据对不上）。 */
  function deadlineHTML(it) {
    return deadlineLineHTML(it, { daysLeft: it.days_left });
  }

  /* 附件里常常才是正文（A 实测从附件里捞回 15 万字），所以必须一眼可见 —— 契约已给 has_attachments */
  function attachmentsHTML(it) {
    if (!it.has_attachments) return '';
    const n = typeof it.attachments_count === 'number' ? it.attachments_count : 0;
    const label = n > 0 ? '附件 ' + n : '有附件';
    const title = n > 0
      ? '这条带 ' + n + ' 个附件，正文可能就在附件里'
      : '这条带附件，正文可能就在附件里';
    return '<span class="pill-attach" title="' + esc(title) + '">' + esc(label) + '</span>';
  }

  /* 标题被 clamp 成两行，只在真的溢出时才给「展开」按钮 */
  function wireToggles(root) {
    root.querySelectorAll('.card').forEach((card) => {
      const a = card.querySelector('.title a');
      const btn = card.querySelector('.toggle');
      if (!btn) return;
      if (!a || a.scrollHeight <= a.clientHeight + 2) { btn.remove(); return; }
      btn.addEventListener('click', () => {
        const open = card.classList.toggle('open');
        btn.textContent = open ? '收起标题' : '展开完整标题';
      });
    });
  }

  boot();
})();
