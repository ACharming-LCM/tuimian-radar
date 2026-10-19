/* 截止日三态措辞 —— **全站唯一实现**（智能体 C）
 *
 * 依据：`contracts/api.md` §14.4 / §14.5，`contracts/events.schema.json`
 *
 * 为什么必须只有一个实现：
 * 三条规矩「显示日期 / 该公告未公布截止日 / 未能读取截止日，请点原文核对」
 * 如果每个页面各拼一遍，一定会拼出三种写法，而且**最容易漏掉 unknown 那一支**。
 * 漏掉就等于说了"没有截止日" —— 用户于是不去看原文。这是本项目反复在防的那类错。
 *
 * ⛔ 绝对禁止（契约原文）：
 *    把 unknown 显示成「没有截止日」「无截止日」「暂无」「——」、或者干脆不显示这一行。
 *    **我们不知道**，和**它没有**，必须是两句不同的话。
 *
 * 三种输入都要认，优先级从上到下：
 *   ① 接口已经拼好的 `deadline_display`（契约 §14.5 明说「直接用，不要自己拼」）
 *   ② 只有 `field_state.deadline`（events 事件就是这种，见 events.schema.json）
 *   ③ 两个都没有（老数据）→ **一律 unknown，且标 _source: inferred**（契约 §14.4）
 */
(function () {
  'use strict';

  /* 契约 §14.5 的两句原话。一个字都不许改。 */
  const TEXT = {
    absent: '该公告未公布截止日',
    unknown: '未能读取截止日，请点原文核对'
  };

  function esc(s) {
    if (typeof window.esc === 'function') return window.esc(s);
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function safeUrl(u) {
    if (!u) return null;
    if (typeof window.safeUrl === 'function') return window.safeUrl(u);
    try {
      const x = new URL(u, location.href);
      return (x.protocol === 'http:' || x.protocol === 'https:') ? u : null;
    } catch (e) {
      return null;
    }
  }

  /** 纯函数：把一条记录（事件 / 公告 / 详情）变成截止日的三态描述。 */
  function deadlineDisplay(rec) {
    const UNKNOWN = {
      state: 'unknown', text: TEXT.unknown, value: null,
      url: rec && rec.url ? rec.url : null,
      must_check_original: true, source: 'unknown'
    };

    if (!rec || typeof rec !== 'object') return UNKNOWN;

    // ① 接口拼好的：直接用（契约 §14.5 / §16.5 都说「不要自己拼」）
    //
    // 两种包装都认：
    //   · `deadline_display`（公告接口 §14.5 的形状）
    //   · `deadline` 本身就是一个对象（推送项 §16.5 就是这么给的：
    //     `{state, text, value, url, must_check_original, days_left}`）
    const dd = (rec.deadline_display && typeof rec.deadline_display === 'object')
      ? rec.deadline_display
      : ((rec.deadline && typeof rec.deadline === 'object') ? rec.deadline : null);
    if (dd && (dd.state === 'found' || dd.state === 'absent' || dd.state === 'unknown')) {
      // rec.deadline 可能是对象（§16.5 那种），所以只在它是标量时才拿来当值用，
      // 否则会渲染出 [object Object]，那比不显示更糟。
      const v = (dd.value != null) ? dd.value
        : ((rec.deadline != null && typeof rec.deadline !== 'object') ? rec.deadline : null);
      let text;
      if (dd.state === 'found') text = String(v == null ? '' : v);
      else text = dd.text || TEXT[dd.state] || TEXT.unknown;
      return {
        state: dd.state,
        text: text,
        value: v,
        url: dd.url || rec.url || null,
        must_check_original: (dd.must_check_original !== undefined)
          ? !!dd.must_check_original : (dd.state === 'unknown'),
        source: 'deadline_display'
      };
    }

    // ② field_state 三态
    //
    // 三种来源都吃，因为它们出现在不同的接口上：
    //   · `field_state`（详情 `?meta=1`、事件流）
    //   · `field_trust.states`（**列表 `?meta=1`** —— 契约 §14.2，列表不带 field_state）
    //   · 两个都没有 → unknown
    // 不统一收在这里的话，页面就会各判各的，然后同一个字段在三个页面三种说法。
    const fs = rec.field_state;
    let st = (fs && typeof fs === 'object') ? fs.deadline : null;
    let stSource = 'field_state';
    if (!st) {
      const ft = rec.field_trust;
      const ftStates = (ft && typeof ft === 'object') ? ft.states : null;
      if (ftStates && typeof ftStates === 'object' && ftStates.deadline) {
        st = ftStates.deadline;
        stSource = 'field_trust';
      }
    }

    if (st === 'found' && typeof rec.deadline !== 'object' && rec.deadline != null && String(rec.deadline).trim() !== '') {
      return {
        state: 'found', text: String(rec.deadline), value: String(rec.deadline),
        url: rec.url || null, must_check_original: false, source: stSource
      };
    }
    if (st === 'absent') {
      return {
        state: 'absent', text: TEXT.absent, value: null,
        url: rec.url || null, must_check_original: false, source: stSource
      };
    }

    // ③ 剩下的全部是 unknown。包括：
    //    · st === 'unknown'
    //    · st === 'found' 但 deadline 是空的（自相矛盾，按没读到来算）
    //    · 根本没有 field_state / field_trust（**匹配页 `/api/match` 就是这种**，
    //      它的 item 里不带任何三态信息 —— 这种只能按"没读到"说，那是最安全的一句：
    //      它让用户去点原文。要区分 absent 得让后端在 match 的 item 里加 `field_trust`。）
    return {
      state: 'unknown', text: TEXT.unknown, value: null,
      url: rec.url || null, must_check_original: true,
      source: st ? 'field_state' : 'inferred'
    };
  }

  /** 渲染成一行 HTML。四种状态各有一句话（C14 定的口径）：
   *    found 未过期 → 「还剩 7 天（10-25 截止）」，3 天内红色、7 天内琥珀色
   *    found 已过期 → 「已于 10-10 截止」+ 已过期徽标
   *    absent       → 「该公告未公布截止日」
   *    unknown      → 「未能读取截止日，请点原文核对」+ 原文链接
   *  `opts.daysLeft` 可显式给（有就以它为准，推送页那条就是服务端算的）。 */
  /* ---------- 「已过期」徽标 ----------
   *
   * **规则和服务端 `push.is_expired()` 一字不差**：`state === 'found'` 且 `days_left < 0`。
   * `absent` 是"公告没公布截止日"，`unknown` 是"我们没读到" ——
   * 把这两种标成"已过期"就是**编造一个我们没有的事实**，
   * 和契约里那条"宁可说不知道，也不说没有"是同一条底线。
   *
   * 数据来源按可靠性排序：
   *   ① 调用方显式传的 `opts.daysLeft`（匹配页就是这么传的）
   *   ② `deadline.days_left`（推送项 §16.5 里服务端算好的）
   *   ③ 都没有（**详情页就是这种**）→ 按 ISO 日期字符串比今天。
   *      用字符串比而不是 `new Date(v) < today`：后者把 `"2026-10-21"` 按 **UTC** 解析，
   *      在东八区会早 8 小时，跨日的时候会**差一天** —— 截止日本来就只剩一天的公告会被算错。
   *      `"2026-10-21" < "2026-10-18"` 这种比较对 `YYYY-MM-DD` 是精确的，也没有时区。
   */
  const EXPIRED_TITLE = '此公告截止日已过，可能因学校延期或我们抽取有误仍有效，请点原文核对。';

  function todayISO() {
    const d = new Date();
    const two = (n) => (n < 10 ? '0' + n : String(n));
    return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate());
  }

  /* ---------- 「还剩几天」：**列表页也要看到**（C14） ----------
   *
   * 三个数据源里只有推送项带服务端算好的 `days_left`；
   * `/api/notices`、`/api/match` 的 item 都没有这个字段 ——
   * 而 C14 的要求是「列表页就能看到剩余天数，不用点进去」。
   *
   * 所以补第四条路：**按 ISO 日期在本地算**。
   * 刻意只用 `Date.UTC` 把两个"日历日"规约到 UTC 零点再相减，全程不做时区换算 ——
   * 于是「10-19 看 10-25 截止」恒等于 6 天，不会因为本机时区、
   * 也不会因为 23:59 跨日而少一天。
   * （`new Date('2026-10-25')` 会按 UTC 解析，在东八区正好差 8 小时，
   *   跨日时差一天 —— 这就是不能用它的原因。）
   *
   * ⚠️ 已知口径差异：服务端用服务器日历日，这里是本机日历日。同一台机器上两者相同；
   * 跨时区用户可能差一天。已写进 journal 转给 B：让 `/api/notices`、`/api/match`
   * 也透传 `days_left`，这里就退化成兜底。
   */
  function isoDaysFromToday(v, todayStr) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v == null ? '' : v).trim().slice(0, 10));
    const t = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(todayStr || todayISO()));
    if (!m || !t) return null;
    const a = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    const b = Date.UTC(Number(t[1]), Number(t[2]) - 1, Number(t[3]));
    return Math.round((a - b) / 86400000);
  }

  /** 展示用短日期：同年只写 MM-DD（`10-25`），跨年写全（`2027-01-05`）。
   *  用户看的是"还有几天"，年份只有在真的跨年时才有信息量。 */
  function shortDate(v, todayStr) {
    const s = String(v == null ? '' : v).trim().slice(0, 10);
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!m) return s;
    const y = String(todayStr || todayISO()).slice(0, 4);
    return (m[1] === y) ? (m[2] + '-' + m[3]) : s;
  }

  function daysLeftOf(rec, dd, o) {
    if (o && typeof o.daysLeft === 'number') return o.daysLeft;
    if (dd && typeof dd.days_left === 'number') return dd.days_left;
    if (rec && rec.deadline && typeof rec.deadline === 'object'
        && typeof rec.deadline.days_left === 'number') return rec.deadline.days_left;
    // 兜底：拿日期自己算（只对 found 有意义）
    const v = (dd && dd.state === 'found')
      ? (dd.value != null ? dd.value
        : ((rec && typeof rec.deadline === 'string') ? rec.deadline : null))
      : null;
    return v ? isoDaysFromToday(v, todayISO()) : null;
  }

  function isExpiredFound(rec, dd, o, left) {
    if (!dd || dd.state !== 'found') return false;   // absent / unknown 一律不算过期
    if (left !== null) return left < 0;              // 有服务端算的数，直接用
    const v = dd.value || (rec && typeof rec.deadline === 'string' ? rec.deadline : null);
    return !!(v && /^\d{4}-\d{2}-\d{2}$/.test(v) && v < todayISO());
  }

  function expiredBadgeHTML() {
    return ' <span class="badge-expired" title="' + esc(EXPIRED_TITLE) +
      '" aria-label="' + esc(EXPIRED_TITLE) + '">已过期</span>';
  }

  function deadlineLineHTML(rec, opts) {
    const d = deadlineDisplay(rec);
    const o = opts || {};

    if (d.state === 'found') {
      const left = daysLeftOf(rec, d, o);
      const expired = isExpiredFound(rec, d, o, left);
      const day = shortDate(d.text);
      // 已过期：一句话说完（「已于 10-10 截止」）。
      // 徽标保留 —— 它说的不是"过期了"（这句已经说过了），而是"可能延期/请核对原文"。
      if (expired) {
        return '<span class="dl-line dl-found is-expired">已于 ' + esc(day) + ' 截止' +
          expiredBadgeHTML() + '</span>';
      }
      // 未过期：**剩余天数在前**（那才是用户要的答案），日期退到括号里。
      // 脸色（C17 定的）：≤3 天红（来不及了）；**其余未来日期一律琥珀** ——
      // 原来只在 ≤7 天给琥珀、更远的保持淡蓝，于是「还剩 12 天」看起来跟"没这条信息"一样。
      if (left !== null && left >= 0) {
        const cls = left <= 3 ? ' dl-urgent' : ' dl-soon';
        const head = left === 0 ? '<strong>今天截止</strong>' : '<strong>还剩 ' + left + ' 天</strong>';
        return '<span class="dl-line dl-found' + cls + '" title="' +
          esc('截止 ' + d.text + '（天数按本机日期算）') + '">' + head +
          '（' + esc(day) + ' 截止）</span>';
      }
      // 有日期但算不出天数（例如"10月下旬"这种非 ISO 写法）：老实只给日期
      return '<span class="dl-line dl-found">截止 <strong>' + esc(d.text) + '</strong></span>';
    }

    if (d.state === 'absent') {
      // 「该公告未公布截止日」—— 这是一个**结论**（正文读了，确实没有），所以不用带链接
      return '<span class="dl-line dl-absent">' + esc(d.text) + '</span>';
    }

    // unknown：必须带原文链接。没有链接时**不能装作正常**，要把缺失说出来。
    //
    // 刻意**不把链接塞进句子里**：那一句是契约里的原话，一个字都不该被拆开改掉。
    // 整句原样输出，链接单独跟在后面 —— 读者看到的仍是完整的那句话。
    const u = safeUrl(d.url);
    const link = u
      ? ' <a class="dl-check" href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">看原文 ↗</a>'
      : ' <span class="dl-check dl-check-missing" title="这条记录没有原文链接，属于数据缺陷">（缺原文链接）</span>';
    return '<span class="dl-line dl-unknown" title="我们没能读到这条的截止日 —— 这不等于它没有截止日">' +
      '⚠ ' + esc(d.text || TEXT.unknown) + link + '</span>';
  }

  window.deadlineDisplay = deadlineDisplay;
  window.deadlineLineHTML = deadlineLineHTML;
  /** 只问"还剩几天"（number / null），不渲染。详情页「下一步」要用它写那句引导语。 */
  window.deadlineDaysLeft = function (rec) {
    return daysLeftOf(rec, deadlineDisplay(rec), null);
  };
  // 「已过期」徽标也要导出：推送页要把它挂在服务端那行文字后面
  // （那一页对 found 不重复渲染截止日，所以不能靠 deadlineLineHTML 带出来）。
  window.expiredBadgeHTML = expiredBadgeHTML;
  window.DEADLINE_TEXT = TEXT;
})();
