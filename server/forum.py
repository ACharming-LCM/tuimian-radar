# -*- coding: utf-8 -*-
"""匿名论坛：帖子字段、匿名代号派生、审核状态机。

契约依据只有 `contracts/api.md` 第 4 节那张四行表 —— 字段表、`review_status`
枚举、举报阈值都是 B 定的默认值，已写进 `issues/007` 请 A/C 确认。
**改动成本都很低**（每处不超过十行），等拍板后照改。

三条这条代码要守住的底线：

1. **绝不返回任何可识别身份的信息。** `author_real_id` / `created_ip` 只进存储，
   响应里一个都不出现。这是"匿名"的全部意义，比功能完整重要。
2. **`pending` / `hidden` 的帖子绝不能出现在列表里**，否则审核队列形同虚设
   （举报了还在前台挂着，那举报入口就是装饰）。
3. **`author_display` 由服务端确定性派生**，不接受前端传值。
   前端传的话，作者就能伪装成别人；不确定派生的话，同一个人每次发帖
   代号都变，"匿名"会变成"查不到自己发过什么"。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime

# 审核状态机。前台可见的只有 visible / rejected。
REVIEW_STATUSES = ("visible", "pending", "hidden", "rejected", "deleted")
VISIBLE_STATUSES = ("visible", "rejected")      # rejected = 举报不成立、恢复展示

# 前台能看到的字段。**白名单**，不是黑名单 —— 加字段要显式改这里。
#
# 九个字段，与 `contracts/api.md` 第 154 行**逐字一致**（"一个不多一个不少"）。
#
# **`review_status` 不在里面，这是合规要求，不是"少给一个字段"：**
# `rejected` 的语义是"被举报过但举报不成立"。一旦公开，
# **任何人都能看出哪些帖子被举报过** —— 举报这件事在匿名论坛里本身就是敏感信息。
# `VISIBLE_STATUSES` 只保证 hidden/pending 的帖子不出现，
# 但可见帖子的状态仍然会泄露它被举报过。
# `review_status` / `author_real_id` / `created_ip` 只在 `/api/admin/queue` 返回。
#
# 踩过的坑：这个白名单原来带着 `review_status`，C 的前端有一道
# "前台字段自检"直接把泄漏标红了（`web/assets/forum.js:19` 的 FORBIDDEN_FIELDS）。
# 他写的是防线，不是误报。
PUBLIC_POST_FIELDS = ("id", "unit", "tag", "title", "body", "author_display",
                      "created_at", "reply_count", "report_count")

# 发帖时前端要提供的字段
REQUIRED_CREATE_FIELDS = ("title", "body")

# 举报到几次自动进审核队列。**3 是 B 拍的**，见 issues/007
REPORT_THRESHOLD = 3

# 匿名代号的词表。故意选和学校/院系无关的中性词，
# 免得代号本身泄漏"这人大概是哪个学院的"。
_DISPLAY_ADJ = ["安静的", "凌晨的", "爱提问的", "认真备考的", "蹲结果的",
                "翻旧帖的", "揣着offer的", "改方案的", "想回家乡的", "等通知的"]
_DISPLAY_NOUN = ["松鼠", "水獭", "夜鹭", "穿山甲", "小熊猫", "海獭",
                 "刺猬", "白鹭", "狸花猫", "北极狐"]


def display_name_of(real_id):
    """从真实 id 确定性派生匿名代号。

    同一 id 永远得到同一代号（sha1 取模选词）。不存表、不用随机数 ——
    随机数会让同一个人每次发言换个名字，那就不叫匿名，叫失忆。
    """
    if not real_id:
        real_id = "anonymous"
    h = hashlib.sha1(str(real_id).encode("utf-8")).hexdigest()
    adj = _DISPLAY_ADJ[int(h[0:4], 16) % len(_DISPLAY_ADJ)]
    noun = _DISPLAY_NOUN[int(h[4:8], 16) % len(_DISPLAY_NOUN)]
    return f"匿名·{adj}{noun}"


def client_ip(request):
    """取客户端 IP，只用于后台留痕（前台永不返回）。

    注意 `X-Forwarded-For` 是客户端**可以伪造**的头。演示环境没有反代，
    所以这里**不信任**它，只用真实的 `request.client.host`。
    真要接反代，得先配 `ProxyHeadersMiddleware` 并明确可信代理列表。
    """
    if request is None or getattr(request, "client", None) is None:
        return None
    return getattr(request.client, "host", None)


def build_post(store, payload, request=None):
    """组装一条新帖。返回 (post, error) —— error 非空表示请求不合法。"""
    for k in REQUIRED_CREATE_FIELDS:
        if not (payload.get(k) or "").strip():
            return None, {"code": "BAD_REQUEST", "message": f"缺少必填字段 {k}"}
    # 长度上限：公开部署后这是不可信输入。只拒"明显不可能是一条帖子"的。
    if len(str(payload.get("title") or "")) > 120:
        return None, {"code": "BAD_REQUEST", "message": "标题超过 120 字"}
    if len(str(payload.get("body") or "")) > 20000:
        return None, {"code": "BAD_REQUEST", "message": "正文超过 2 万字"}

    real_id = (payload.get("author_real_id") or "").strip()
    if not real_id:
        # 演示期允许没有身份，但**后台要能看出这是无身份的帖子**
        real_id = "anonymous"

    review_status = "visible"
    if payload.get("review_status") in REVIEW_STATUSES:
        # 只为演示审核后台留个入口。**不能让前端随便把帖子设成 deleted**，
        # 所以只允许"预先置为待审"这一种无害用法。
        review_status = "pending" if payload["review_status"] == "pending" else "visible"

    now = datetime.now()
    post = {
        "id": store.next_post_id(),
        "unit": (payload.get("unit") or "").strip(),
        "tag": (payload.get("tag") or "").strip(),
        "title": payload["title"].strip(),
        "body": payload["body"].strip(),
        # 前台可见
        "author_display": display_name_of(real_id),
        "created_at": now.isoformat(timespec="seconds"),
        "reply_count": 0,
        "report_count": 0,
        "review_status": review_status,
        # 后台留痕，**永不返回给前台**
        "author_real_id": real_id,
        "created_ip": client_ip(request),
        "reports": [],
    }
    return post, None


def public_view(post, body_limit=None):
    """投影出前台可见字段。**白名单**，所以 identity 字段不可能漏出去。"""
    d = {k: post.get(k) for k in PUBLIC_POST_FIELDS}
    if body_limit and d.get("body") and len(d["body"]) > body_limit:
        d["body"] = d["body"][:body_limit] + "…"
        d["body_truncated"] = True
    return d


def list_posts(store, unit=None, tag=None, page=1, size=50, q=None,
               include_hidden=False, body_limit=None):
    """列表。默认**只给前台可见的** —— `pending` / `hidden` / `deleted` 全部挡掉。

    `body_limit` 非空时截断正文（非订阅者只给前 200 字 + `truncated: true`）。
    """
    units = [u.strip() for u in (unit or "").split(",") if u.strip()]
    tags = [t.strip() for t in (tag or "").split(",") if t.strip()]

    def hit(needle, hay):
        return needle.lower() in (hay or "").lower()

    out = []
    for p in store.posts():
        if not include_hidden and p.get("review_status") not in VISIBLE_STATUSES:
            continue
        if units and not any(hit(u, p.get("unit")) for u in units):
            continue
        if tags and not any(hit(t, p.get("tag")) for t in tags):
            continue
        if q and not (hit(q, p.get("title")) or hit(q, p.get("body"))):
            continue
        out.append(p)

    out.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    total = len(out)
    start = (page - 1) * size
    return total, [public_view(p, body_limit=body_limit) for p in out[start:start + size]]


def report_post(store, post_id, reason=None, reporter_id=None):
    """举报。到阈值自动转 `pending` 进审核队列。返回 (post, error, deduped)。

    **同一 `reporter_id` 对同一帖只计一次**（契约 api.md 第 204 行）——
    否则找三个朋友就能把任何帖子压下去，匿名论坛会立刻变成互相举报的工具。
    这条是防滥用，不是可选项，所以重复举报返回 `deduped=True` 而不是静默 +1。
    """
    post = store.find_post(post_id)
    if post is None:
        return None, {"code": "NOT_FOUND", "message": "帖子不存在"}, False
    if post.get("review_status") == "deleted":
        return None, {"code": "GONE", "message": "帖子已删除"}, False

    reporter = (reporter_id or "").strip() or "unknown"
    # **"查重 + 追加 + 计数 + 转 pending"整段在锁里**（`state.transaction()`）：
    # 原来只在最后 `store.save()` 里拿锁，于是两个并发举报可能都通过查重、
    # 都 append —— 一个人的两次举报被算成两个人，帖子里"三个朋友"就能压下去。
    # 这是和 `consume_quota` 同一类"读-改-写"，只是后果从"少赚一次"变成"审核被滥用"。
    with store.transaction():
        reports = post.setdefault("reports", [])
        if any(r.get("reporter_id") == reporter for r in reports):
            # 已经举报过：不改计数，但要告诉调用方"这次没算"
            return post, None, True

        reports.append({
            "reason": (reason or "").strip() or "未填写理由",
            "reporter_id": reporter,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        post["report_count"] = len(reports)
        # **首次举报的时间**，处理时限（`due_at` / `overdue`）从它算起（契约第 13 节）。
        # 只记第一次：时限衡量的是"这条举报我们多久没管"，
        # 用最后一次举报时间会让持续被举报的帖子永远不超时 —— 那正好是最该被看到的那些。
        post.setdefault("first_reported_at", reports[0]["at"])
        if post["report_count"] >= REPORT_THRESHOLD and post["review_status"] == "visible":
            post["review_status"] = "pending"
            post["pending_since"] = datetime.now().isoformat(timespec="seconds")
    return post, None, False


def queue(store, status=None):
    """审核队列：默认给所有**非 visible** 的帖子（就是需要人看的那些）。

    每条都带上报理由 —— 审核的人要能看出为什么被举报，
    只给一个 `report_count=3` 是没法判的。
    """
    out = []
    for p in store.posts():
        if status:
            if p.get("review_status") != status:
                continue
        elif p.get("review_status") in ("visible", "rejected"):
            continue
        item = dict(p)                       # 后台可以看全字段，含 author_real_id
        item["reports"] = p.get("reports") or []
        out.append(item)
    out.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return out


def moderate(store, post_id, action, note=None, operator_id=None,
             reason_code=None):
    """审核动作。`hide` 下架 / `restore` 恢复 / `delete` 删除。

    **依据与前后状态由调用方（app.py）负责写审计** —— 这个函数只改帖子状态，
    保持它原来的职责单一。审计写在 `server/governance.py` 那一层，
    这样"哪些动作要留痕"是一个显式列表，而不是散落在这个函数的分支里。

    返回 `(post, error, from_status)`：第三个是**动作之前**的状态，
    审计要记 `from_status → to_status`（只看新状态无法回答
    "这条是原本好好的被下架了，还是本来就在待审里被判了"）。
    """
    post = store.find_post(post_id)
    if post is None:
        return None, {"code": "NOT_FOUND", "message": "帖子不存在"}, None
    mapping = {"hide": "hidden", "restore": "rejected", "delete": "deleted"}
    if action not in mapping:
        return None, {"code": "BAD_REQUEST",
                      "message": f"action 只能是 {' / '.join(mapping)}"}, None
    from_status = post.get("review_status")
    # 和 `report_post` 同一个理由：状态迁移 + 落盘要在同一段里，别让并发的
    # 另一个审核动作插在中间（表现是 `from_status` 记错、审计链断一环）。
    with store.transaction():
        post["review_status"] = mapping[action]
        post["moderated_at"] = datetime.now().isoformat(timespec="seconds")
        if operator_id:
            post["moderated_by"] = operator_id
        if reason_code:
            post["moderation_reason_code"] = reason_code
        if note:
            post["moderation_note"] = note
    return post, None, from_status


def facets(store, unit_suggestions=None):
    """已有的版块（unit）和标签（tag）取值 + 条数，**再加 A 产出的院系建议值**。

    为什么必须带建议值：论坛一开始没人发帖，光靠"已有取值"做下拉建议是空的，
    用户就会自由发挥 ——「物理学院」和「物理系」裂成两个版块。
    `unit_suggestions` 来自 `data/out/units.json`（含公告/导师计数）。

    **建议值的唯一键是 `(学校, 院系)`，不是院系名** ——
    `units.json` 里「研究生院」在哈工大/南开/电子科大/东南/西工大 5 所学校下都有，
    只按院系名去重会把它们并成一条，前端看到"研究生院"分不清是哪个学校。
    这个坑我踩了：一开始按 `unit` 去重，10 条建议变成 4 条。
    """
    def counts(key):
        d = {}
        for p in store.posts():
            if p.get("review_status") not in VISIBLE_STATUSES:
                continue
            v = p.get(key) or ""
            if v:
                d[v] = d.get(v, 0) + 1
        return d

    used_units = counts("unit")
    used_tags = counts("tag")

    def rows(d):
        return [{"value": k, "count": v} for k, v in
                sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

    # 建议值：按 (school, unit) 去重；标签按学校分组以便前端区分
    suggestion_rows = []
    seen = set()
    for s in (unit_suggestions or []):
        name, school = s.get("unit"), s.get("school")
        if not name:
            continue
        key = (school or "", name)
        if key in seen:
            continue
        seen.add(key)
        suggestion_rows.append({
            "value": name,
            # 论坛帖子的 unit 是自由文本、不带学校，所以 value 保持干净的院系名，
            # 学校放进 school 字段让前端自己决定要不要显示
            "school": school,
            "count": used_units.get(name, 0),
            "notice_count": s.get("notice_count", 0),
            "faculty_count": s.get("faculty_count", 0),
            "from_seed": True,
        })
    # 用户实际用过但不在建议表里的（自由文本本来就没法保证全覆盖）
    for name, n in used_units.items():
        if name not in {r["value"] for r in suggestion_rows}:
            suggestion_rows.append({"value": name, "count": n, "from_seed": False})
    suggestion_rows.sort(key=lambda r: (-r["count"], -r.get("notice_count", 0), r["value"]))

    return {
        "units": suggestion_rows,
        "tags": rows(used_tags),
        "total": sum(1 for p in store.posts()
                     if p.get("review_status") in VISIBLE_STATUSES),
        "schools": sorted({r["school"] for r in suggestion_rows if r.get("school")}),
        "_note": "units 里 from_seed=true 的来自 data/out/units.json 的建议值（按学校+院系去重），"
                 "不是论坛已有帖子",
    }
