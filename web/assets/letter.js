/* 套磁信页（智能体 C）
 * 数据源：POST /api/letter（B 的模板版）；拿不到就回退 contracts/fixtures/letter.response.json
 * 三条硬要求：
 *   1. 页面上必须说清「这是模板生成、没接模型」——不许让它看起来像已经能生成（这是诚信问题，不是减分）；
 *   2. citations 是对象数组（label / quote / source / url），逐条可点回公告原文；
 *   3. 演示重点是 citations 能回链，不是信漂不漂亮。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  async function boot() {
    const noticeId = new URLSearchParams(location.search).get('notice_id');
    try {
      const data = await liveOrFixture(() => requestLetter(noticeId), 'letter.response.json');
      markSource();

      // fixture 只有一条样例；真接口是按 notice_id 现生成的，不需要这个判断
      const mismatch = dataSource === 'fixtures' && noticeId && data.notice_id && noticeId !== data.notice_id;
      if (mismatch) renderMismatch(noticeId, data);
      else render(data, noticeId);
      el('state').hidden = true;
    } catch (err) {
      showError(el('state'), err);
    }
  }

  /** 真接口：POST /api/letter，body 是 {notice_id, profile}。
   *  画像取的是匹配页那一份（同一个 localStorage）—— 所以演示时
   *  「现场填的背景」会一路带到这封信里，不是各页各一套。 */
  async function requestLetter(noticeId) {
    if (!noticeId) {
      throw new Error('这一页要带 notice_id 才能生成 —— 请从公告详情页的「生成套磁信」按钮进来。');
    }
    return apiPost('/api/letter', {}, { notice_id: noticeId, profile: loadProfile() });
  }

  const SAMPLE_TITLE = (cites) => {
    const c = (cites || []).find((x) => x.source === 'title');
    return c ? c.quote : null;
  };

  const highlightBlanks = (s) => s.replace(/_{3,}/g, '<mark class="blank">$&</mark>');

  function bodyHTML(body) {
    const raw = String(body == null ? '' : body).trim();
    if (!raw) return '<p class="muted">（这条没有正文）</p>';
    return raw.split(/\n{2,}/).map((seg) =>
      '<p>' + seg.split('\n').map((l) => highlightBlanks(esc(l))).join('<br>') + '</p>'
    ).join('');
  }

  function citeHTML(c, i) {
    const url = safeUrl(c.url);
    return '<li class="cite">' +
      '<div class="cite-head">' +
        '<span class="cite-no">' + (i + 1) + '</span>' +
        '<span class="cite-label">' + esc(c.label || '引文') + '</span>' +
        (c.source ? '<span class="cite-src">' + esc(c.source) + '</span>' : '') +
      '</div>' +
      '<blockquote class="cite-quote">' + esc(c.quote || '') + '</blockquote>' +
      (url
        ? '<a class="cite-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">回到公告原文 ↗</a>'
        : '<span class="cite-nolink">这条引文没有可回链的地址</span>') +
    '</li>';
  }

  /* 硬要求 1：不许看起来像已经能生成。method 由后端给，模板版是 "template"。 */
  function bannerHTML(data) {
    const isTemplate = data.method === 'template' || dataSource === 'fixtures';
    const title = isTemplate
      ? '本页为<strong>模板生成</strong>：正文是按公告原文拼出来的模板，<strong>没有接入模型</strong>。'
      : '本页由模型生成（<code>method: ' + esc(String(data.method || '未知')) + '</code>）。';
    const notes = [data._note, data._profile_note].filter(Boolean);
    const meta = [];
    if (data.generated_at) meta.push('生成时间 ' + data.generated_at);
    if (data.tone) meta.push('语气 ' + data.tone);

    return '<div class="alert alert-strong">' +
      '<div class="alert-title">' + title + '</div>' +
      (notes.length ? '<span class="alert-sub">' + notes.map(mdInline).join('　') + '</span>' : '') +
      (meta.length ? '<span class="alert-sub">' + esc(meta.join('　')) + '</span>' : '') +
    '</div>';
  }

  function warningsHTML(data) {
    const ws = (data.warnings || []).filter(Boolean);
    if (!ws.length) return '';
    return '<section class="dblock"><h2 class="panel-title">生成时的提醒</h2>' +
      '<ul class="warns">' + ws.map((w) => '<li>' + mdInline(w) + '</li>').join('') + '</ul></section>';
  }

  function render(data, noticeId) {
    const cites = data.citations || [];
    const title = SAMPLE_TITLE(cites);

    el('results').innerHTML =
      bannerHTML(data) +

      '<section class="dblock">' +
        '<h2 class="panel-title">这封信是针对哪条公告写的</h2>' +
        '<p class="letter-target">' + (title ? esc(title) : esc(data.notice_id || noticeId || '未知公告')) + '</p>' +
        '<p class="muted">模板正文里的 <mark class="blank">____</mark> 是画像占位符：真实调用时由 ' +
          '<code>POST /api/letter</code> 的 <code>profile</code> 填进去。' +
          (dataSource === 'fixtures' ? '当前是样例数据，所以还留着空位。' : '') + '</p>' +
      '</section>' +

      /* 收件人：**公告里没有导师邮箱，所以这一块必须由画像现选**。
       *
       * 早先这里只有一句"去导师页挑人"的指路 —— 那在主路径上是一道**看得见的缝**：
       * 信写好了，观众会问"发给谁"，然后画面必须跳走。
       *
       * 现在的做法：按当前画像调 `POST /api/faculty/match` 取前 5 位，
       * 当场选一位 → 显示姓名 / 单位 / 邮箱 + 复制。**不改后端形状**（这个接口本来就有）。
       * 选完也要说清"这仍是模板信、没接模型"。
       * 内容由 renderRecipient() 异步填进来（它要发请求，不能卡在同步渲染里）。 */
      '<section class="dblock letter-to" id="letter-to">' +
        '<h2 class="panel-title">收件人</h2>' +
        '<p class="muted letter-to-text">正在按你的画像找对口方向的老师…</p>' +
      '</section>' +

      '<section class="dblock">' +
        '<h2 class="panel-title">主题</h2>' +
        '<p class="letter-subject">' + esc(data.subject || '（无主题）') + '</p>' +
      '</section>' +

      '<section class="dblock">' +
        '<h2 class="panel-title">正文（模板）</h2>' +
        '<div class="dbody letter-body">' + bodyHTML(data.body) + '</div>' +
      '</section>' +

      warningsHTML(data) +

      '<section class="dblock">' +
        '<h2 class="panel-title">引文出处（' + cites.length + ' 条，逐条可回链）</h2>' +
        '<p class="section-note">这几条是从公告原文里摘出来的，点「回到公告原文」跳学校官网。' +
          '接入模型之后，正文由模型生成，但<strong>引文必须仍然逐条能指回原文</strong>——' +
          '这是唯一能防住「模型编造导师信息」的机制。</p>' +
        (cites.length
          ? '<ol class="cites">' + cites.map(citeHTML).join('') + '</ol>'
          : '<p class="muted">这条没有引文。</p>') +
      '</section>' +

      '<div class="dactions">' +
        '<a class="btn-ghost" href="detail.html?id=' + encodeURIComponent(data.notice_id || noticeId || '') + '">← 回公告详情</a>' +
        '<a class="btn-ghost" href="notices.html">公告列表</a>' +
      '</div>';

    renderRecipient();
  }

  /* ---------- 收件人：按画像现选 ---------- */

  /** 画像里"参与匹配"的字段（和契约 §16.2 一致：school 只展示、不参与）。
   *  这里只用它决定"能不能选人"，具体匹配交给服务端。 */
  function matchFields(p) {
    if (!p) return null;
    const out = {};
    if (p.school) out.school = p.school;
    if (Array.isArray(p.majors) && p.majors.length) out.majors = p.majors;
    if (Array.isArray(p.preferred_schools) && p.preferred_schools.length) out.preferred_schools = p.preferred_schools;
    if (Array.isArray(p.interest_keywords) && p.interest_keywords.length) out.interest_keywords = p.interest_keywords;
    return (out.majors || out.preferred_schools || out.interest_keywords) ? out : null;
  }

  async function renderRecipient() {
    const host = document.getElementById('letter-to');
    if (!host) return;

    const p = matchFields(loadProfile());
    if (!p) {
      // 没画像就没法按方向选人 —— 这个"空"是有出路的，不是空白
      host.innerHTML =
        '<h2 class="panel-title">收件人</h2>' +
        '<p class="muted">还没有画像，所以没法按研究方向给你挑老师。' +
          '填四行字之后这一块会自动出现对口方向的候选人。</p>' +
        '<div class="dactions">' +
          '<a class="btn-primary" href="onboard.html">去填画像 →</a>' +
          '<a class="btn-ghost" href="faculty.html">直接去导师页翻 →</a>' +
        '</div>';
      return;
    }

    let data = null;
    try {
      data = await apiPost('/api/faculty/match', { limit: 5 }, p);
    } catch (e) {
      data = null;
    }

    const items = (data && data.items) || [];
    if (!items.length) {
      // 读不到 / 一个都没命中：**回落到指路**，而不是显示一个空框
      host.innerHTML =
        '<h2 class="panel-title">收件人</h2>' +
        '<p class="muted">没能按你的画像匹配到老师' +
          (data ? '（导师库里暂时没有方向对得上的）' : '（导师匹配接口这次没通）') +
          '。可以直接去导师页按单位/方向翻。</p>' +
        '<div class="dactions">' +
          '<a class="btn-primary" href="faculty.html">去导师页挑人、复制邮箱 →</a>' +
        '</div>';
      return;
    }

    host.innerHTML =
      '<h2 class="panel-title">收件人</h2>' +
      '<p class="muted letter-to-text">这封信是按<strong>公告</strong>生成的，公告里没有老师邮箱。' +
        '下面这几位是按你画像里的<strong>意向学校 / 专业 / 方向</strong>匹配出来的 —— ' +
        '选一位，邮箱就能复制走。</p>' +
      '<ul class="to-picks">' +
        items.map((it, i) => {
          const areas = (it.research_areas || []).slice(0, 2).join('、');
          return '<li class="to-pick">' +
            '<button type="button" class="to-pick-btn" data-pick="' + i + '">' +
              '<span class="to-pick-name">' + esc(it.name || '（未署名）') + '</span>' +
              '<span class="to-pick-meta">' + esc(it.unit || '') +
                (it.title ? ' · ' + esc(it.title) : '') + '</span>' +
              (areas ? '<span class="to-pick-areas">' + esc(areas) + '</span>' : '') +
              '<span class="to-pick-mail">' + (it.email ? esc(it.email) : '没留邮箱') + '</span>' +
            '</button>' +
          '</li>';
        }).join('') +
      '</ul>' +
      '<div id="to-chosen" class="to-chosen" hidden></div>' +
      '<p class="section-note">这封信仍然是<strong>模板生成、没有接入模型</strong>；' +
        '选谁只影响"发给谁"，不影响正文怎么写。</p>';

    const chosen = host.querySelector('#to-chosen');
    host.querySelectorAll('[data-pick]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const it = items[Number(btn.getAttribute('data-pick'))];
        if (!it) return;
        host.querySelectorAll('.to-pick-btn').forEach((b) => b.classList.remove('is-on'));
        btn.classList.add('is-on');
        chosen.hidden = false;
        chosen.innerHTML =
          '<p class="to-chosen-name">收件人：<strong>' + esc(it.name || '（未署名）') + '</strong>' +
            '　<span class="muted">' + esc(it.unit || '') + '</span></p>' +
          (it.email
            ? '<div class="dactions">' +
                '<code class="to-mail">' + esc(it.email) + '</code>' +
                '<button type="button" class="copy-btn" data-copy="' + esc(it.email) + '">复制邮箱</button>' +
                (it.homepage ? '<a class="btn-ghost" href="' + esc(it.homepage) +
                  '" target="_blank" rel="noopener noreferrer">老师主页 ↗</a>' : '') +
              '</div>' +
              '<p class="muted">邮箱是从导师主页抓的，可能过期 —— 发之前自己在主页上对一眼。</p>'
            : '<p class="muted">这位老师在名录里<strong>没有留邮箱</strong>，所以复制不了。' +
                '这不是页面坏了：名录里有些只给姓名和方向。</p>');
      });
    });
  }

  /* 样例数据只有一条，从别的公告点进来时要说清楚，不能拿样例冒充 */
  function renderMismatch(requested, data) {
    const cites = data.citations || [];
    const title = SAMPLE_TITLE(cites);
    el('results').innerHTML =
      '<div class="alert alert-strong">' +
        '<div class="alert-title">演示样例只有一条：它对应的是另一篇公告。</div>' +
        '<span class="alert-sub">你点开的是 <code>' + esc(requested) + '</code>，' +
        '而样例属于 <code>' + esc(data.notice_id || '') + '</code>（' + esc(title || '') + '）。' +
        '现在是样例模式（<code>?api=fixtures</code>）；真接口会按 <code>notice_id</code> 现生成一封。</span>' +
        '<span class="alert-sub">另外：正文是<strong>模板</strong>拼的、<strong>没有接入模型</strong>；' +
        '真接口返回的也是模板版（<code>method: template</code>）。</span>' +
      '</div>' +
      '<div class="dactions">' +
        '<a class="btn-primary" href="letter.html?notice_id=' + encodeURIComponent(data.notice_id || '') + '">看那条公告的样例</a>' +
        '<a class="btn-ghost" href="detail.html?id=' + encodeURIComponent(requested) + '">← 回公告详情</a>' +
      '</div>';
  }

  boot();
})();
