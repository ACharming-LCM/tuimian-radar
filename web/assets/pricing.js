/* 订阅 / 付费墙页（智能体 C）
 * 数据源：contracts/fixtures/me.response.json（GET /api/me）
 * 支付是**模拟的**：只改本页内存状态，不跳转、不请求任何支付通道。
 * 理由写在数据里（me.response.json 的 _note），本页从数据读，不自己编口径。
 */
(function () {
  'use strict';

  const el = (id) => document.getElementById(id);
  const state = { me: null, selected: null, paid: false };

  async function boot() {
    try {
      const me = await liveOrFixture(() => apiGet('/api/me', userQuery()), 'me.response.json');
      state.me = me;
      markSource();
      renderSwitcher();
      renderAccount();
      renderPlans();
      renderCompare();
      renderMockNote();
      el('state').hidden = true;
    } catch (err) {
      showError(el('state'), err);
    }
  }

  const isSubscribed = () => state.paid || (state.me.user && state.me.user.subscribed === true);

  /* ---------- 演示身份切换（假登录态） ----------
   * 契约 api.md 第 5 节：演示要**两个身份** ——
   * 订阅身份让墙不要打断流程，免费身份用来**主动演示墙本身**。
   * 切换只改 ?user= 这个假登录态，不涉及真实账号。 */
  function renderSwitcher() {
    const node = el('switcher');
    if (!node) return;
    const cur = currentUser();
    const btn = (id, label, cls) =>
      '<button type="button" class="sw-btn ' + cls + '" data-user="' + esc(id) + '"' +
      (cur === id ? ' aria-pressed="true"' : '') + '>' + esc(label) + '</button>';

    node.innerHTML =
      '<span class="sw-label">演示身份</span>' +
      btn(DEFAULT_USER, '免费用户', '') +
      btn(SUB_USER, '订阅用户', 'sw-sub') +
      '<span class="sw-hint">切的是假登录态（<code>?user=</code>），不涉及真实账号；' +
        '订阅身份会先走一次模拟支付</span>';

    node.addEventListener('click', async (e) => {
      const b = e.target.closest('.sw-btn');
      if (!b) return;
      const id = b.dataset.user;
      if (id === SUB_USER && useLive()) {
        // 服务端按 user_id 记账，所以订阅身份要真的开通一次，否则仍会被额度拦住
        try { await apiPost('/api/subscribe', {}, { user_id: id, plan_id: 'monthly' }); } catch (err) { /* 已订阅也算成功 */ }
      }
      setCurrentUser(id);
      const url = new URL(location.href);
      url.searchParams.set('user', id);
      location.href = url.toString();
    });
  }

  /* ---------- 账户与免费额度 ---------- */

  function renderAccount() {
    const u = state.me.user || {};
    // 接口没给额度字段时，**不能显示成 0 条／天** —— 那是在替接口说一个错误的数字。
    // （四态走查的空数据格抓到过：额度那行写「0 条／天」，下面的对比表却写「每天 3 条」，同一屏自相矛盾。）
    const quotaKnown = typeof u.daily_free_quota === 'number';
    const quota = quotaKnown ? u.daily_free_quota : 3;
    const used = typeof u.daily_used === 'number' ? u.daily_used : 0;
    const left = Math.max(0, quota - used);
    const pct = quota > 0 ? Math.round((used / quota) * 100) : 0;

    let badge;
    if (isSubscribed()) {
      badge = '<span class="pill-sub">已订阅' + (state.paid ? '（演示状态）' : (u.expires_at ? '至 ' + esc(u.expires_at) : '')) + '</span>';
    } else {
      badge = '<span class="pill-unknown">未订阅</span>';
    }

    el('account').innerHTML =
      '<div class="acct-row">' +
        '<span class="acct-name">' + esc(u.nickname || u.id || '未登录用户') + '</span>' + badge +
      '</div>' +
      (isSubscribed()
        ? '<p class="acct-line">订阅中：截止日提醒、套磁信、论坛全文、公告变更推送都已解锁。' +
          (state.paid ? '<span class="acct-demo">这是演示状态，刷新页面即还原。</span>' : '') + '</p>'
        : '<p class="acct-line">今天还能免费看 <strong>' + left + '</strong> 条公告详情（额度 ' + quota + ' 条／天，已用 ' + used + ' 条）。公告列表不限量。' +
          (quotaKnown ? '' : '<span class="acct-demo">接口这次没返回额度字段，按 3 条／天显示。</span>') + '</p>' +
          '<div class="quota"><i style="width:' + pct + '%"></i></div>') +
      '<p class="acct-line muted">额度只限制「详情」，列表和匹配结果一直免费——我们不想让你连有没有这个机会都看不到。</p>' +
      (state.order
        ? '<p class="acct-line">刚才的订单：<code>' + esc(state.order.order_id || '') + '</code>　' +
          '¥' + esc(String(state.order.amount)) + '　状态 ' + esc(state.order.status || '') +
          (state.order._note ? '　<span class="acct-demo">' + esc(state.order._note) + '</span>' : '') + '</p>'
        : '');

    el('account-wrap').hidden = false;
  }

  /* ---------- 套餐 ---------- */

  function renderPlans() {
    const plans = state.me.plans || [];
    // 套餐读不到时不能留一片空白（四态走查的空数据格：右列整块是空的，看着像页面坏了）
    el('plans').innerHTML = plans.length
      ? plans.map(planHTML).join('')
      : '<p class="muted">这次没读到套餐信息（接口的 <code>plans</code> 是空的）。' +
        '刷新一下，或者用免费的「公告列表 / 匹配结果」——那两块不依赖这个接口。</p>';
    el('plans-wrap').hidden = false;

    el('plans').addEventListener('click', (e) => {
      const btn = e.target.closest('.plan-btn');
      if (!btn) return;
      const plan = plans.find((p) => p.id === btn.dataset.plan);
      if (!plan) return;
      state.selected = plan;
      renderPay();
    });
  }

  function planHTML(p) {
    const perMonth = (p.id === 'season' && p.price && p.period && /月/.test(p.period))
      ? '折合每月 ¥' + (p.price / parseInt(p.period, 10)).toFixed(1)
      : '';
    const feats = (p.features || []).map((f) => '<li>' + esc(f) + '</li>').join('');
    const selected = state.selected && state.selected.id === p.id;
    return '<article class="plan' + (p.id === 'season' ? ' plan-best' : '') + (selected ? ' plan-on' : '') + '">' +
      (p.id === 'season' ? '<span class="plan-tag">更划算</span>' : '') +
      '<div class="plan-name">' + esc(p.name) + '</div>' +
      '<div class="plan-price"><span class="cur">¥</span>' + esc(String(p.price)) +
        '<span class="per">／' + esc(p.period) + '</span></div>' +
      (perMonth ? '<div class="plan-permonth">' + esc(perMonth) + '</div>' : '') +
      '<ul class="plan-feats">' + feats + '</ul>' +
      '<button type="button" class="plan-btn" data-plan="' + esc(p.id) + '">' +
        (isSubscribed() ? '已是订阅状态' : '选择' + esc(p.name)) + '</button>' +
    '</article>';
  }

  /* ---------- 模拟支付 ---------- */

  function renderPay() {
    if (!state.selected || isSubscribed()) { el('pay-wrap').innerHTML = ''; return; }
    const p = state.selected;
    el('pay-wrap').innerHTML =
      '<div class="pay">' +
        '<div class="pay-head">确认开通：' + esc(p.name) + '　¥' + esc(String(p.price)) + '／' + esc(p.period) + '</div>' +
        '<p class="pay-warn"><strong>这是模拟支付。</strong>不会产生任何真实扣款，不会跳转到任何支付页面，' +
          '也不会要求你填任何账号。' + (useLive() ? '它会调 <code>POST /api/subscribe</code>，' : '') +
          '把状态改成「已订阅」。</p>' +
        (state.payError ? '<p class="pay-error">开通失败：' + esc(state.payError) + '</p>' : '') +
        '<div class="pay-actions">' +
          '<button type="button" class="btn-primary" id="pay-ok">确认开通（演示）</button>' +
          '<button type="button" class="btn-ghost" id="pay-cancel">取消</button>' +
        '</div>' +
      '</div>';

    el('pay-ok').addEventListener('click', async () => {
      const btn = el('pay-ok');
      const planId = state.selected.id;
      state.payError = null;
      if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
      try {
        if (useLive()) {
          // 响应 {user, order, plans}；order._note 就是"这是模拟支付"那句（契约要求留着）
          const res = await apiPost('/api/subscribe', {}, { user_id: currentUser(), plan_id: planId });
          state.me.user = res.user || state.me.user;
          state.me.plans = res.plans || state.me.plans;
          state.order = res.order || null;
        } else {
          state.paid = true;
        }
        state.selected = null;
        renderAccount();
        renderPlans();
        renderPay();
        const acct = el('account-wrap');
        if (acct && acct.scrollIntoView) acct.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } catch (err) {
        state.payError = err.message;
        renderPay();
      }
    });
    el('pay-cancel').addEventListener('click', () => {
      state.selected = null;
      renderPlans();
      renderPay();
    });
  }

  function renderMockNote() {
    const note = state.me._note;
    el('mock-note').textContent = note
      ? '支付说明（来自接口数据）：' + note
      : '支付说明：本页支付为模拟，不接真实支付通道。';
  }

  /* ---------- 免费 vs 订阅 ---------- */

  function renderCompare() {
    const plans = state.me.plans || [];
    const monthly = plans.find((p) => p.id === 'monthly') || plans[0] || { features: [] };
    const season = plans.find((p) => p.id === 'season');
    const extra = season ? (season.features || []).filter((f) => f !== '以上全部') : [];

    const quota = typeof state.me.user.daily_free_quota === 'number' ? state.me.user.daily_free_quota : 3;
    const free = ['公告列表：无限浏览', '每天 ' + quota + ' 条公告详情（详情额度，和推送额度分开）',
                  '匹配结果 + 每条的理由'];
    const paid = (monthly.features || []).slice();

    el('compare').innerHTML =
      '<div class="cmp-col">' +
        '<div class="cmp-head">免费</div>' +
        '<ul class="cmp-list">' + free.map((f) => '<li>' + esc(f) + '</li>').join('') + '</ul>' +
      '</div>' +
      '<div class="cmp-col cmp-paid">' +
        '<div class="cmp-head">订阅</div>' +
        (paid.length
          ? '<ul class="cmp-list">' + paid.map((f) => '<li>' + esc(f) + '</li>').join('') + '</ul>'
          : '<p class="muted">这次没读到套餐权益（接口的 <code>plans[].features</code> 是空的）。</p>') +
        (extra.length
          ? '<div class="cmp-sub">推免季畅读额外包含</div><ul class="cmp-list">' +
            extra.map((f) => '<li>' + esc(f) + '</li>').join('') + '</ul>'
          : '') +
      '</div>';

    el('compare-wrap').hidden = false;
  }

  boot();
})();
