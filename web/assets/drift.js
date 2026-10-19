/* 契约漂移面板（第 4 环）
 *
 * 为什么它算鲁棒性：契约漂移是**静默发生**的。
 *   · 后端加了个字段 → 前端不会崩，只是永远不显示（用户少了一信息，没人发现）；
 *   · 前端渲染了个不该露的字段 → 也不会报错（论坛的实名/IP 泄漏就是这么来的）。
 * 这两种都只能靠**对表**发现，而人不会天天对表。所以让页面自己每天对一次。
 *
 * 面板里三件事，来源各不相同：
 *   ① 渲染了但契约里没有（**泄漏**）      —— 载荷键 vs 白名单，硬结论
 *   ② 契约里有但这次载荷里没有（**漂移**）—— 载荷键 vs 白名单，硬结论
 *   ③ 契约有、载荷有、但页面上找不到这个值 —— **启发式**（拿值去 innerText 里找），
 *      所以单独标出来，不跟 ①② 混在一起。
 *
 * 白名单的出处：`contracts/fixtures/*.json` 的键集合 + `contracts/api.md`
 * 第 1/2/8/10/12/14 节里 `?meta=1` 追加的那些键。**没写死数据值**，只写死键名。
 *
 * ⚠️ 它自己的盲点（比面板本身重要）：**本页一条数据都没读到时，它什么都查不出**。
 * 那时面板会明确写「没有样本，这不是通过」，而不是显示一个绿勾 ——
 * 论坛页那块自检在列表 0 条时正是这个毛病。
 */
(function () {
  'use strict';

  var SHAPES = {
    'notice.list': {
      file: 'contracts/fixtures/notices.list.json + api.md §14.2 / §15.3（meta=1 追加）',
      // `data_state` 是 `?meta=1` 的顶层追加（契约 §15.3：分清"没数据"和"筛得太窄"）。
      // 原白名单漏了它，于是它每次都被当成"泄漏"报警 ——
      // **一个总在报警的面板等于没有面板**，所以这条必须补上。
      top: ['total', 'page', 'size', 'items', 'data_state'],
      item: ['id', 'school', 'school_slug', 'unit', 'board', 'title', 'publish_date', 'url',
        'deadline', 'apply_mode', 'target_year', 'degree_types', 'majors', 'tags', 'extract',
        'has_attachments', 'attachments_count',
        // 只有 ?meta=1 才有
        'warnings', 'extract_meta', 'field_trust'],
      structural: ['school_slug', 'page', 'size', 'total']
    },
    'notice.detail': {
      file: 'contracts/fixtures/notices.detail.json + api.md §14.2/§14.4',
      top: [],
      item: ['id', 'school', 'school_slug', 'unit', 'board', 'title', 'publish_date', 'url', 'source',
        'body_text', 'body_len', 'attachments', 'deadline', 'target_year', 'degree_types', 'apply_mode',
        'majors', 'materials', 'contact', 'tags', 'extract', 'crawled_at', 'content_hash', 'relevance',
        'body_how', 'field_state',
        'warnings', 'extract_meta', 'field_trust', 'deadline_display', '_duplicate_variants'],
      structural: ['school_slug', 'relevance', 'body_how']
    },
    'match': {
      file: 'contracts/fixtures/match.response.json（契约 §2 明说形状被它钉死）',
      top: ['items', 'profile', 'stats'],
      item: ['id', 'score', 'raw_score', 'bucket', 'days_left', 'title', 'school', 'unit',
        'publish_date', 'deadline', 'url', 'reasons', 'warnings', 'has_attachments',
        'attachments_count', 'extract'],
      structural: []
    },
    'faculty.match': {
      file: 'contracts/fixtures/faculty.match.json',
      top: ['items', 'profile', 'total'],
      item: ['id', 'name', 'school', 'unit', 'title', 'group', 'research_areas', 'email',
        'homepage', 'hits', 'reasons', 'warnings', 'score'],
      structural: []
    },
    'faculty.list': {
      file: 'contracts/fixtures/faculty.list.json（契约 §8）',
      top: ['total', 'page', 'size', 'items'],
      item: ['id', 'name', 'school', 'unit', 'title', 'group', 'group_kind', 'research_areas',
        'email', 'homepage', 'is_phd_supervisor'],
      structural: ['page', 'size', 'total']
    },
    'posts.list': {
      // 契约 §4：前台**只许 9 个公开字段**。
      // 原白名单多写了一个 `body_truncated`，而它在 api.md 和 fixtures 里**都不存在** ——
      // 于是每页都报一条假的"接口漂移"。**白名单里不许有臆造的键**：
      // 报假警和漏报一样坏，因为人会因此不再看这个面板。
      file: 'contracts/api.md §4（前台只许 9 个公开字段）',
      top: ['total', 'page', 'size', 'items'],
      item: ['id', 'title', 'body', 'unit', 'tag', 'author_display', 'created_at', 'reply_count',
        'report_count'],
      structural: ['page', 'size', 'total']
    },
    'me': {
      file: 'contracts/fixtures/me.response.json（契约 §5）',
      top: ['user', 'plans', '_note'],
      user: ['id', 'nickname', 'subscribed', 'daily_free_quota', 'daily_used', 'expires_at'],
      plan: ['id', 'name', 'price', 'period', 'features'],
      structural: ['_note']
    },
    'letter': {
      file: 'contracts/fixtures/letter.response.json（契约 §3）',
      top: ['notice_id', 'subject', 'body', 'citations', 'method', 'warnings', 'generated_at',
        'tone', 'cited_from', '_note', '_profile_note'],
      citation: ['label', 'quote', 'source', 'url'],
      structural: ['_note', '_profile_note']
    },
    'changes': {
      file: 'contracts/api.md §10',
      top: ['generated_at', 'counts', 'added', 'changed', 'reparsed', 'removed', 'reidentified'],
      counts: ['previous', 'current', 'added', 'changed', 'reparsed', 'removed', 'reidentified', 'unchanged'],
      structural: []
    },
    'freshness': {
      file: 'contracts/api.md §12',
      top: ['generated_at', 'now', 'generated_age_hours', 'generated_age_text', 'oldest_hours',
        'stale_after_hours', 'worst', 'totals', 'schools', 'source', '_note'],
      school: ['school', 'unit', 'homepage', 'last_ok', 'age_hours', 'freshness', 'records',
        'boards', 'boards_empty', 'failed', 'has_failure', 'empty', 'status'],
      structural: ['_note', 'now', 'source']
    }
  };

  var PATH_SHAPE = {
    '/api/notices': 'notice.list',
    '/api/match': 'match',
    '/api/faculty/match': 'faculty.match',
    '/api/faculty': 'faculty.list',
    '/api/posts': 'posts.list',
    '/api/me': 'me',
    '/api/letter': 'letter',
    '/api/changes': 'changes',
    '/api/freshness': 'freshness'
  };

  var seen = {};       // shape -> 最近一次载荷
  var order = [];

  function shapeFor(path) {
    if (!path) return null;
    if (path.indexOf('/api/notices/') === 0) return 'notice.detail';
    return PATH_SHAPE[path] || null;
  }

  function record(shape, data) {
    if (!shape || !data || typeof data !== 'object') return;
    if (!seen[shape]) order.push(shape);
    seen[shape] = data;
  }

  /* ---------- 拿载荷：挂在 apiGet / apiPost 上（common.js 是唯一入口） ---------- */
  /* 详情页自己用 fetchWithTimeout 拿响应头（额度在 header 里），所以它单独调 record()。
   * 这一层不动任何一个页面的取数逻辑 —— 只是"顺手记一笔"。 */
  if (typeof window.apiGet === 'function' && !window.apiGet.__drift) {
    var origGet = window.apiGet;
    window.apiGet = function (path) {
      return origGet.apply(null, arguments).then(function (d) {
        record(shapeFor(path), d);
        return d;
      });
    };
    window.apiGet.__drift = true;
  }
  if (typeof window.apiPost === 'function' && !window.apiPost.__drift) {
    var origPost = window.apiPost;
    window.apiPost = function (path) {
      return origPost.apply(null, arguments).then(function (d) {
        record(shapeFor(path), d);
        return d;
      });
    };
    window.apiPost.__drift = true;
  }

  /* ---------- 对表 ---------- */
  function keysOf(o) {
    return (o && typeof o === 'object' && !Array.isArray(o)) ? Object.keys(o) : [];
  }

  function diffKeys(actual, allowed) {
    var leaks = actual.filter(function (k) { return allowed.indexOf(k) < 0 && k.charAt(0) !== '_'; });
    var missing = allowed.filter(function (k) { return actual.indexOf(k) < 0 && k.charAt(0) !== '_'; });
    return { leaks: leaks, missing: missing };
  }

  function shapeAllowed(shape, extra) {
    var s = SHAPES[shape] || {};
    return (extra || s.item || []).slice();
  }

  function checkOne(shape) {
    var data = seen[shape];
    var s = SHAPES[shape] || {};
    var out = { shape: shape, file: s.file || '', samples: 0, leaks: [], missing: [], unused: [] };
    if (!data) return out;

    var text = (document.body ? document.body.innerText : '') || '';
    function probeKeys(obj, allowed, label) {
      var actual = keysOf(obj);
      var d = diffKeys(actual, allowed);
      d.leaks.forEach(function (k) { out.leaks.push(label + k); });
      d.missing.forEach(function (k) { out.missing.push(label + k); });
      // 「有没有显示出来」：只对字符串叶子做，拿值去屏幕上找（启发式，见文件头）
      var structural = (s.structural || []);
      actual.forEach(function (k) {
        if (structural.indexOf(k) >= 0 || k.charAt(0) === '_') return;
        var v = obj[k];
        var leaves = [];
        (function walk(x, depth) {
          if (depth > 2) return;
          if (typeof x === 'string') { if (x.length >= 2 && x.length <= 80) leaves.push(x); return; }
          if (Array.isArray(x)) { x.forEach(function (y) { walk(y, depth + 1); }); return; }
          if (x && typeof x === 'object') { Object.keys(x).forEach(function (y) { walk(x[y], depth + 1); }); }
        })(v, 0);
        if (!leaves.length) return;
        var hit = leaves.some(function (t) { return text.indexOf(t) >= 0; });
        if (!hit) out.unused.push(label + k);
      });
    }

    if (shape === 'notice.detail') {
      out.samples = 1;
      probeKeys(data, s.item, '');
    } else if (shape === 'me') {
      out.samples = 1;
      probeKeys(data, s.top, '');
      probeKeys(data.user || {}, s.user || [], 'user.');
      (Array.isArray(data.plans) ? data.plans : []).forEach(function (p) { probeKeys(p, s.plan || [], 'plans[].'); });
    } else if (shape === 'letter') {
      out.samples = 1;
      probeKeys(data, s.top, '');
      (Array.isArray(data.citations) ? data.citations : []).forEach(function (c) {
        probeKeys(c, s.citation || [], 'citations[].');
      });
    } else if (shape === 'changes') {
      out.samples = 1;
      probeKeys(data, s.top, '');
      probeKeys(data.counts || {}, s.counts || [], 'counts.');
    } else if (shape === 'freshness') {
      out.samples = 1;
      probeKeys(data, s.top, '');
      (Array.isArray(data.schools) ? data.schools.slice(0, 3) : []).forEach(function (x) {
        probeKeys(x, s.school || [], 'schools[].');
      });
    } else {
      out.samples = 1;
      probeKeys(data, s.top, '');
      var items = Array.isArray(data.items) ? data.items : [];
      items.slice(0, 3).forEach(function (it) { probeKeys(it, s.item || [], 'items[].'); });
    }

    // 去重（同一个字段在多条 item 里会报多次）
    ['leaks', 'missing', 'unused'].forEach(function (k) {
      out[k] = out[k].filter(function (x, i, a) { return a.indexOf(x) === i; });
    });
    return out;
  }

  /** 给量具（_c-cdp.js 的 probe）和面板共用的结论。**别返回几千个叶子值**。 */
  function report() {
    var shapes = order.map(checkOne);
    var leaks = [];
    var missing = [];
    var unused = [];
    shapes.forEach(function (s) {
      s.leaks.forEach(function (x) { leaks.push(s.shape + '.' + x); });
      s.missing.forEach(function (x) { missing.push(s.shape + '.' + x); });
      s.unused.forEach(function (x) { unused.push(s.shape + '.' + x); });
    });
    return {
      samples: order.length,
      shapes: order,
      leaks: leaks,
      missing: missing,
      unused: unused,
      verdict: order.length ? (leaks.length ? 'leak' : 'ok') : 'no-sample'
    };
  }

  function panelHTML(r) {
    if (!r.samples) {
      return '<div class="drift-inner">' +
        '<p class="drift-head"><b>字段自检：这一页没有样本</b></p>' +
        '<p class="drift-note">本轮一个接口响应都没读到，所以<b>什么都查不出</b>。' +
        '「查不出」不等于「没泄漏」—— 这是这块自检自己的盲点，写在这里免得有人把它当通过。</p>' +
        '</div>';
    }
    var line = function (label, arr, cls) {
      if (!arr.length) return '';
      return '<p class="drift-row ' + cls + '"><b>' + label + '（' + arr.length + '）：</b>' +
        '<code>' + esc(arr.slice(0, 14).join('</code> <code>')) + '</code>' +
        (arr.length > 14 ? ' 等' : '') + '</p>';
    };
    return '<div class="drift-inner">' +
      '<p class="drift-head"><b>字段自检：' + r.samples + ' 个接口形状已对表</b>' +
      '（' + esc(r.shapes.join('、')) + '）</p>' +
      line('载荷里有、契约里没有（可能是泄漏）', r.leaks, 'drift-leak') +
      line('契约里有、这次载荷里没有（接口漂移）', r.missing, 'drift-missing') +
      line('契约有、载荷有、但屏幕上找不到这个值', r.unused, 'drift-unused') +
      (!r.leaks.length && !r.missing.length
        ? '<p class="drift-note">① 泄漏和 ② 漂移都是 0。' +
          '第三项是<b>启发式</b>（拿值去屏幕文字里找），它列出来的不一定是漏做 —— 可能只是不在这一页显示。</p>'
        : '') +
      '<p class="drift-copy"><button type="button" class="copy-btn" data-copy="' +
        esc(JSON.stringify(r, null, 1)) + '">复制自检报告</button></p>' +
      '</div>';
  }

  function render() {
    var host = document.querySelector('.foot');
    if (!host) return;
    var node = document.getElementById('drift-panel');
    if (!node) {
      node = document.createElement('details');
      node.id = 'drift-panel';
      node.className = 'drift';
      host.appendChild(node);
    }
    var r = report();
    node.innerHTML = '<summary>字段自检（契约漂移面板）' +
      (r.leaks.length ? ' <span class="drift-bad">⚠ ' + r.leaks.length + ' 处可疑</span>' : '') +
      '</summary>' + panelHTML(r);
  }

  window.__CONTRACT_REPORT__ = report;
  window.recordPayload = record;

  // 渲染完再对表（要读屏幕上的文字，太早读不到）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(render, 600); });
  } else {
    setTimeout(render, 600);
  }
})();
