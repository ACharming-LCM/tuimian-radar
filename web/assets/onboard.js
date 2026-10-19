/* 我的画像页（智能体 C）
 *
 * 主线第一环：**填画像 → 看推送预览**。所以这一页只做一件事：
 * 让用户用最少的字，说清"我要什么"，然后立刻能证明"它真的按我说的筛"。
 *
 * 两块的存储**必须分开**，这是这一页最要紧的一个决定：
 *   · 画像（学校/专业/方向）→ **localStorage**。它是个人情况，不该上传。
 *   · 关注规则（关注哪些学校/单位）→ **`/api/watch`**（契约 §11 的真接口）。
 *     "关注什么"是需要服务端记住、并且每天按它筛的东西，存本地就筛不了。
 * 把两者混成一个"用户资料"上传，是这一个产品最不该做的事。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);

  const state = {
    me: null,
    watches: [],
    subLoaded: false,
    watchLoaded: false,
    freeLimit: null      // 从 /api/push/preview 的 tally.free_limit 读，读不到就是 null
  };

  const splitList = (s) => String(s == null ? '' : s)
    .split(/[、,，;；\s]+/).map((x) => x.trim()).filter(Boolean);

  function boot() {
    renderForm();
    el('profile-panel').hidden = false;
    loadSub();      // 独立加载，失败不影响画像表单
  }

  /* ---------- 画像表单（本地） ----------
   *
   * **这一页和匹配页的表单写的是同一份画像**（同一个 localStorage 键 `tuimian_profile`）。
   * 两处不是重复实现，是分工：
   *   · 这里 = **首次填写**，只要四行（学校/专业/意向学校/意向方向），门槛最低；
   *   · 匹配页 = **微调**，多出来的字段（成绩排名、英语、科研、目标学位、招生方式…）
   *     都在那边，因为它要现场改完立刻重新匹配。
   * 共用存储的代价是**字段语义必须一致** —— 比如 `majors` 两页都是数组。
   * 任何一边改了语义而另一边没改，就会变成静默的数据丢失（见下面保存那段）。 */

  function renderForm() {
    const p = loadProfile();
    const node = el('profile-form');
    if (!node) return;

    node.innerHTML =
      '<div class="ob-grid">' +
        '<label class="ob-field">本科院校' +
          '<input id="ob-school" type="text" autocomplete="off" placeholder="如 浙江大学" value="' +
            esc(p.school || '') + '"></label>' +
        '<label class="ob-field">本科专业（可填多个，逗号分隔）' +
          '<input id="ob-major" type="text" autocomplete="off" placeholder="如 物理学、应用物理" value="' +
            esc((p.majors || []).join('、')) + '"></label>' +
      '</div>' +
      '<label class="ob-field">意向学校（逗号分隔，可填多所）' +
        '<input id="ob-schools" type="text" autocomplete="off" placeholder="如 浙江大学、复旦大学、中国科学技术大学" value="' +
          esc((p.preferred_schools || []).join('、')) + '"></label>' +
      '<label class="ob-field">意向方向（逗号分隔，同义词自己写全）' +
        '<input id="ob-keywords" type="text" autocomplete="off" placeholder="如 凝聚态物理、超导、量子" value="' +
          esc((p.interest_keywords || []).join('、')) + '"></label>' +
      '<div class="ob-actions">' +
        '<button type="button" class="btn-primary" id="ob-save">保存画像</button>' +
        '<button type="button" class="btn-ghost" id="ob-demo">填示例值</button>' +
        '<a class="btn-ghost" id="ob-next" href="pushes.html">看推送预览 →</a>' +
        '<span class="pf-hint" id="ob-msg"></span>' +
      '</div>';

    el('ob-save').addEventListener('click', () => {
      const p2 = Object.assign({}, loadProfile());
      p2.school = val('ob-school');
      // **多值**：这两页写的是同一份画像（同一个 localStorage 键），
      // 早先这里只取 `majors[0]`，于是在这一页保存一次就把匹配页填的多个专业砍成一个
      // —— 静默的数据丢失，用户根本不会察觉。两套 UI 共用一个存储，字段就必须同语义。
      p2.majors = splitList(val('ob-major'));
      p2.preferred_schools = splitList(val('ob-schools'));
      p2.interest_keywords = splitList(val('ob-keywords'));

      // ① 先存本地 —— 离线（`?api=fixtures`）和匹配页都靠它，而且它一定成功。
      saveProfile(p2);
      // ② **再同步到服务端**。这一条是必须的，不是可选的：
      //    `/api/push/preview` 的匹配发生在**服务端**，它读 `server/var/state.json` 里的画像。
      //    只写 localStorage 的话，用户在画像页填完 → 推送页读到的还是服务端那份旧的（或者根本没有），
      //    于是"填了画像"和"看到的推送"是两个世界。
      //    （第一版就漏了这一步。B 问「你为什么选存 localStorage」时我才对上。）
      syncProfileToServer(p2);
    });

    el('ob-demo').addEventListener('click', () => {
      el('ob-school').value = '浙江大学';
      el('ob-major').value = '物理学';
      el('ob-schools').value = '浙江大学、中国科学技术大学、复旦大学';
      el('ob-keywords').value = '凝聚态物理、超导、量子';
      hint('已填入示例值 —— 还没保存，点「保存画像」。');
    });

    // 跳去推送页时带上 user，身份跟着走（和 pricing / detail 一致的假登录态）
    el('ob-next').addEventListener('click', (e) => {
      e.preventDefault();
      // 先保存再跳，否则"填了没生效"是最容易被投诉的一件事
      el('ob-save').click();
      location.href = 'pushes.html?user=' + encodeURIComponent(currentUser());
    });
  }

  const val = (id) => { const n = el(id); return n && n.value != null ? String(n.value).trim() : ''; };

  /** 把画像同步到服务端（`PUT /api/profile`，契约 §16.2：**整份替换、幂等**）。
   *
   *  三件事要说清：
   *  ① **本地那份仍然保留** —— 离线演示、匹配页都要用，服务端不可用时页面不能因此空白；
   *  ② 失败要**说出来**，不能静默：用户以为填了就生效，实际推送还按服务端的旧画像算，那是最坏的形态；
   *  ③ `school`（本科院校）服务端**只展示、不参与匹配**（契约 §16.2），
   *     所以"填了却没推送"可能只是意向学校/方向是空的 —— 这由服务端 `complete.message` 解释，前端不自己判。 */
  async function syncProfileToServer(p) {
    const msg = el('ob-msg');
    if (msg) { msg.textContent = '已存在这台浏览器里，正在同步到服务端…'; msg.className = 'pf-hint'; }
    try {
      const r = await fetch('/api/profile' + qs({ user: currentUser() }), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(p),
        cache: 'no-store'
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json().catch(() => null);
      const c = (d && d.complete) || {};
      if (msg) {
        // 服务端自己会说"填够了没、缺什么"（契约 §16.2：**别让前端自己判**）
        msg.textContent = '已保存并同步到服务端。' + (c.message ? '　' + c.message : '');
        msg.className = 'pf-hint' + (c.can_push ? ' pf-hint-ok' : ' pf-hint-warn');
      }
    } catch (e) {
      if (msg) {
        msg.textContent = '本地已保存，但**没同步到服务端**（' + (e && e.message ? e.message : e) +
          '）—— 推送页读的是服务端画像，所以它可能还是旧的。服务起来后再点一次保存即可。';
        msg.className = 'pf-hint pf-hint-warn';
      }
    }
  }

  function hint(t) {
    const n = el('ob-msg');
    if (!n) return;
    n.textContent = t;
    n.className = 'pf-hint pf-hint-ok';
    setTimeout(() => { n.textContent = ''; }, 4000);
  }

  /* ---------- 订阅与关注（服务端） ---------- */

  async function loadSub() {
    // 三件事并行：订阅状态、关注规则、免费条数。**任何一个失败都不该把另一个也拖黑** ——
    // 所以分别 catch，各自渲染。
    //
    // 第三件为什么值得单独发一个请求：免费条数是后端的 `config.PUSH_FREE_LIMIT`
    // （`PUSH_FREE_LIMIT=3`），`/api/me` **不暴露**它，只有 `/api/push/preview` 的
    // `tally.free_limit` 里有。而推送页那个数字就是读接口的 ——
    // **同一个数字不许两处两种做法**（一处读接口、一处写死），所以这里也读。
    // `limit=1` 只是为了少传数据；GET 是幂等的，**不消费推送额度**（`committed:false`）。
    // 三件事**并行**发出去。**别写成串行** —— 第一版我把第三个请求接在 Promise.all 之后，
    // 结果「接口超时」那一态下总耗时 = 8 秒 × 3 ≈ 24 秒才落屏。
    // 三个请求互不依赖，串行没有任何理由，只会让最坏情况从 8 秒变成 24 秒。
    const [meOk, watchOk] = await Promise.all([
      apiGet('/api/me', userQuery()).then((d) => { state.me = d; state.subLoaded = true; return true; }).catch(() => false),
      apiGet('/api/watch', userQuery()).then((d) => {
        state.watches = (d && d.watches) || [];
        state.watchLoaded = true;
        return true;
      }).catch(() => false),
      loadFreeLimit()          // 内部自己 catch，永远 resolve
    ]);

    // 这两个请求是**直接调的 apiGet**（不是 liveOrFixture），所以没人替我们设 `dataSource`。
    // 不设它，页脚会永远停在 HTML 里的静态默认值「读取中…」——
    // 等于一个页面读完数据了却不说自己读的是什么。失败路径同样要设（'none'）。
    dataSource = (meOk || watchOk) ? 'live' : 'none';
    markSource();
    renderSub(meOk, watchOk);
  }

  /** 免费身份能看到几条 —— 从 `/api/push/preview` 的 `tally.free_limit` 读。
   *  读不到就返回 null，界面上**不写数字**（写一个猜的数字比不写更坏）。 */
  async function loadFreeLimit() {
    try {
      const d = await apiGet('/api/push/preview', { user: currentUser(), limit: 1 });
      const n = d && d.tally && d.tally.free_limit;
      state.freeLimit = (typeof n === 'number' && n >= 0) ? n : null;
    } catch (e) {
      state.freeLimit = null;
    }
  }

  /** 免费身份的说明。**数字只从接口来**；接口没给就不提数字。 */
  function freeText() {
    const n = state.freeLimit;
    if (typeof n === 'number') return '免费身份每天只看到前 ' + n + ' 条推送。';
    return '免费身份只能看到前几条推送（具体条数以推送页为准）。';
  }

  function renderSub(meOk, watchOk) {
    const node = el('sub-body');
    const range = el('sub-range');
    if (!node) return;

    if (!meOk && !watchOk) {
      // **仅当两个都取不到时才走这里。** 正常路径走不到这一支 ——
      // 实测 `/api/me` 和 `/api/watch` 都返回 200（不是"都不通"）。
      // 这一块整块换成诚实的错误态，**但画像表单照常能用**（它只依赖 localStorage）。
      node.innerHTML = '<div class="state err">' +
        '<h3>订阅与关注读不到</h3>' +
        '<p>这一块要问服务端的 <code>/api/me</code> 和 <code>/api/watch</code>，这次两个都取不到。' +
        '<strong>上面的画像不受影响</strong>——它存在你的浏览器里。</p>' +
        '<div class="state-actions"><button type="button" class="btn-ghost" onclick="location.reload()">重试</button>' +
        '<a class="btn-ghost" href="pricing.html">订阅页</a></div></div>';
      if (range) range.textContent = '没读到';
      return;
    }

    const u = state.me && state.me.user;
    const sub = !!(u && u.subscribed);
    if (range) range.textContent = meOk ? (sub ? '已订阅' : '免费身份') : '订阅状态没读到';

    const subHTML = meOk
      ? '<div class="ob-sub-state ' + (sub ? 'is-sub' : '') + '">' +
          '<span class="ob-sub-badge">' + (sub ? '订阅身份' : '免费身份') + '</span>' +
          '<span class="ob-sub-text">' +
            (sub
              ? '今天的推送全部可见' + (u && u.expires_at ? '（有效期至 ' + esc(String(u.expires_at).slice(0, 10)) + '）' : '')
              : freeText() + '<a href="pricing.html">去订阅</a>') +
          '</span></div>'
      : '<div class="ob-sub-state ob-sub-warn"><span class="ob-sub-badge">没读到</span>' +
        '<span class="ob-sub-text">订阅状态读不到（<code>/api/me</code> 这次没通）。<a href="pricing.html">去订阅页</a>试试。</span></div>';

    node.innerHTML = subHTML + watchHTML(watchOk);
    wireWatch();
  }

  function watchHTML(ok) {
    if (!ok) {
      return '<div class="ob-block">' +
        '<h3 class="ob-h3">关注规则</h3>' +
        '<p class="pf-hint pf-hint-warn">读不到（<code>/api/watch</code> 没通）。' +
        '关注规则存在服务端，读不到就不知道你现在关注了什么。</p></div>';
    }

    const list = state.watches || [];
    const items = list.length
      ? '<ul class="watch-list">' + list.map((w) => {
          const parts = [w.school, w.unit].filter(Boolean).join(' · ');
          const kw = (w.keywords || []).filter(Boolean);
          return '<li class="watch-item">' +
            '<span class="watch-what">' + esc(parts || '（只按关键词）') +
              (kw.length ? '<span class="watch-kw">' + kw.map((k) => '<span class="why-chip">' + esc(k) + '</span>').join('') + '</span>' : '') +
            '</span>' +
            '<button type="button" class="link-btn watch-del" data-id="' + esc(w.id) + '">取消关注</button>' +
          '</li>';
        }).join('') + '</ul>'
      : '<p class="muted">还没有关注规则。下面可以从你的画像一键生成。</p>';

    return '<div class="ob-block">' +
      '<h3 class="ob-h3">关注规则</h3>' +
      '<p class="section-note">关注规则存在<strong>服务端</strong>（<code>/api/watch</code>）——' +
        '因为它要每天按它筛推送。<strong>画像存在本地</strong>，两者不是一回事。</p>' +
      items +
      '<div class="ob-actions">' +
        '<button type="button" class="btn-ghost" id="watch-from-profile">从画像生成关注（按意向学校）</button>' +
        '<span class="pf-hint" id="watch-msg"></span>' +
      '</div>' +
    '</div>';
  }

  function wireWatch() {
    const add = el('watch-from-profile');
    if (add) {
      add.addEventListener('click', async () => {
        const schools = loadProfile().preferred_schools || [];
        const msg = el('watch-msg');
        if (!schools.length) {
          if (msg) msg.textContent = '画像里还没有意向学校 —— 先在上面填，再点这里。';
          return;
        }
        add.disabled = true;
        if (msg) msg.textContent = '正在生成…';
        let okN = 0, failN = 0;
        for (const s of schools) {
          try {
            const r = await apiPost('/api/watch', {}, { user_id: currentUser(), school: s });
            state.watches = (r && r.watches) || state.watches;
            okN++;
          } catch (e) { failN++; }
        }
        add.disabled = false;
        if (msg) {
          msg.textContent = '已关注 ' + okN + ' 所学校' + (failN ? '，' + failN + ' 所失败（可能已关注过）' : '') + '。';
        }
        renderSub(true, true);
      });
    }

    // 取关：**必须真的调 DELETE，并且如实处理失败**。
    // 契约 §11 说得明白：取关不存在的 id 返回 404 而不是 200，
    // 就是为了不让"点了取消却还在收到提醒"这种事静悄悄发生。
    // 所以这里失败一定要说出来，不能把按钮文字改回去就当没事了。
    document.querySelectorAll('.watch-del').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = btn.getAttribute('data-id');
        if (!id) return;
        btn.disabled = true;
        btn.textContent = '取消中…';
        try {
          const resp = await fetch('/api/watch/' + encodeURIComponent(id) + qs(userQuery()), {
            method: 'DELETE', cache: 'no-store'
          });
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          const d = await resp.json();
          state.watches = (d && d.watches) || state.watches.filter((w) => w.id !== id);
          renderSub(true, true);
        } catch (e) {
          btn.disabled = false;
          btn.textContent = '取消关注';
          const msg = el('watch-msg');
          if (msg) {
            msg.textContent = '取消关注失败：' + (e && e.message ? e.message : e) +
              '　—— 这条规则**可能还在**，刷新确认一下。';
          }
        }
      });
    });
  }

  boot();
})();
