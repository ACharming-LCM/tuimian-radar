/* 「快截止」入口（智能体 C，C17，2026-10-19）
 *
 * 学生打开保研工具第一个动作几乎一定是"哪些快来不及了"。这一行就是回答那个问题。
 *
 * ## 数字只从 /api/facets 来，不在页面上自己数
 *
 * 判据是「入口的数字与 `/api/facets` 的 within_7 count 一致」。自己在页面上按
 * `deadline` 数一遍，只要有一条边界条件不同（今天算不算、当天截止算不算"7 天内"），
 * 就会和下拉里那个数字对不上 —— 而用户会同时看到这两个数字。
 * 所以：**入口读 facets，列表用 `?deadline_within=7`**，两边都是服务端那一份定义。
 *
 * ## 读不到时不能静默消失
 *
 * 入口是导航。读不到就整行消失，用户会以为"这个功能没了"；所以读不到要说一句
 * 「这一行没读出来，下面的列表照常能用」。
 */
(function () {
  'use strict';

  function mount() {
    const box = document.getElementById('deadline-entry');
    if (!box) return;

    apiGet('/api/facets').then(function (d) {
      const buckets = (d && d.deadline_buckets) || [];
      const pick = function (k) { return buckets.filter(function (b) { return b.value === k; })[0] || null; };
      const w7 = pick('within_7');
      if (!w7) {                      // 老版本接口没有这个字段：明确说没有，不编一个数字
        box.hidden = false;
        box.innerHTML = '<p class="dl-entry-note dl-entry-warn">' +
          '「快截止」这一行需要接口的 <code>deadline_buckets</code>，这个服务端还没有 —— 列表照常能用。</p>';
        return;
      }

      const w1 = pick('within_1');
      const exp = pick('expired');
      const unk = (pick('unknown') || { count: 0 }).count + (pick('absent') || { count: 0 }).count;

      box.hidden = false;
      box.innerHTML =
        '<a class="dl-entry" href="notices.html?deadline=within_7&sort=deadline">' +
          '<span class="dl-entry-ico" aria-hidden="true">⏰</span>' +
          '<strong>7 天内截止 · ' + Number(w7.count || 0) + ' 条</strong>' +
          (w1 && w1.count ? '<span class="dl-entry-sub">1 天内 ' + w1.count + ' 条</span>' : '') +
          '<span class="dl-entry-more">看清单 →</span>' +
        '</a>' +
        '<p class="dl-entry-note">另外：已截止 <strong>' + Number((exp || {}).count || 0) +
          '</strong> 条、截止日未知 <strong>' + unk + '</strong> 条（没读到或没公布）。' +
          '未知的<strong>不当作已过期</strong>，也不藏起来。</p>';
    }).catch(function (e) {
      box.hidden = false;
      box.innerHTML = '<p class="dl-entry-note dl-entry-warn">「快截止」这一行没读出来（' +
        esc((e && e.message) || e) + '），下面的列表照常能用。</p>';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
