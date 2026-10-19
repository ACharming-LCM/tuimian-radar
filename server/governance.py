# -*- coding: utf-8 -*-
"""内容审核与合规。契约见 `contracts/api.md` 第 13 节。

## 为什么匿名论坛是整个项目法律风险最高的功能

评审只要问一句"**有人在上面骂了某个实验室，你们怎么处理**"，
没有这一层就拿不出答案。而《民法典》通知—删除要的不是"我们会处理"，
是四件事：**收得到、处理得掉、留得下痕、说得出依据**。

反过来这是加分项：能把这条链说清楚的匿名论坛，比"我们做了个论坛"强得多。

## 三条设计原则

**1. 审计是 append-only，而且结构上做不到改。**
`StateStore` 上只有 `append_audit` 和 `audit_log` —— **没有修改、没有删除**，
接口层也不提供。一条可以被改的审计记录，在法律意义上等于没有审计。
所以它是**独立的一份**，不混进帖子记录：帖子可能被删，审计不能跟着消失。

**2. 审计自带原文快照。**
每条状态变更的记录里都存着**当时的标题和正文**。这样即使 `posts` 被清空，
仍然能回答"当时下架的是什么内容、依据是什么" —— 那是"说得出依据"的字面意思。
（冗余是刻意的：审计宁可丑、宁可重复，**不能丢**。）

**3. 时限是一个能被看到的数，不是感觉。**
队列每条带 `age_hours` / `due_at` / `overdue`，整体给 `overdue_count`。
"我们积压了多少"必须是界面上的一个数字，否则它会变成一个每个人都说
"还好吧"的话题。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from server import config

# ---------------------------------------------------------------------------
# 处置类别。**给用户看的是类别，不是举报人是谁。**
#
# 「依据」要落成可枚举的类别，不能是自由文本 —— 自由文本没法统计、
# 也没法在界面上给作者一个不会暴露举报人的说法。
# ---------------------------------------------------------------------------
REASON_CODES = (
    ("illegal", "涉嫌违法违规"),
    ("harassment", "人身攻击 / 侮辱"),
    ("privacy", "泄露个人隐私"),
    ("defamation", "造谣 / 诽谤机构或个人"),
    ("spam", "广告 / 刷屏"),
    ("irrelevant", "与版块无关"),
    ("other", "其他"),
)
REASON_LABELS = dict(REASON_CODES)
DEFAULT_REASON_CODE = "other"

# 审核状态机里"我们还没处理"的那些 —— 只有 `pending` 需要我们动手，
# 所以**超时只对 `pending` 判定**（见 `overdue_of` 的说明）。
PENDING_STATUS = "pending"

# 状态变更动作（会写审计的）
AUDIT_ACTIONS = (
    "report",          # 收到举报
    "auto_pending",    # 举报到阈值，自动转待审
    "hide",            # 管理员下架
    "restore",         # 管理员恢复
    "delete",          # 管理员删除
    "appeal",          # 作者申诉
    "appeal_upheld",   # 申诉被驳回（维持原处置）
    "appeal_restored", # 申诉成立，恢复
)

# 申诉的终态。到了这两个之一就不能再动，也不接受第二次申诉。
APPEAL_TERMINAL = ("upheld", "restored")
APPEAL_STATUSES = ("none", "pending") + APPEAL_TERMINAL


def reason_label(code):
    return REASON_LABELS.get(code or DEFAULT_REASON_CODE, REASON_LABELS[DEFAULT_REASON_CODE])


def normalize_reason_code(code):
    """未知类别一律落到 `other`，**不报错** —— 举报入口不该因为
    类别写错就拒绝一条真实举报（那是拿格式问题挡内容问题）。"""
    return code if code in REASON_LABELS else DEFAULT_REASON_CODE


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def snapshot_of(post):
    """下架/删除/申诉时的**原文快照**。

    为什么必须存：帖子记录**可能被删**，而"我们当时为什么下架它"
    这个问题在事后必须还能回答。只留一个 `post_id` 是不够的 ——
    那个 id 指向的内容可能已经不在了。
    """
    if not post:
        return None
    return {
        "id": post.get("id"),
        "title": post.get("title"),
        "body": post.get("body"),
        "unit": post.get("unit"),
        "tag": post.get("tag"),
        "author_display": post.get("author_display"),
        "created_at": post.get("created_at"),
        # **不含** author_real_id / created_ip —— 审计是给人看的记录，
        # 里面有身份信息就多一份泄露面；身份留在帖子记录里即可。
        "_note": "下架/删除时的原文快照，用于事后说明依据",
    }


def build_audit(action, post=None, actor_id=None, actor_role="system",
                reason_code=None, reason_note=None, from_status=None,
                to_status=None, extra=None, with_snapshot=False):
    """组装一条审计。**只描述发生了什么，不做判断。**

    `from_status` → `to_status` 必须都记：只看 `to_status` 无法回答
    "这条是原本就好好的被下架了，还是本来就在待审里被判了" ——
    而这两件事的责任完全不同。
    """
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "post_id": (post or {}).get("id"),
        "actor_id": actor_id or "system",
        "actor_role": actor_role,
        "reason_code": reason_code,
        "reason_label": reason_label(reason_code) if reason_code else None,
        "reason_note": reason_note,
        "from_status": from_status,
        "to_status": to_status,
    }
    if with_snapshot:
        rec["post_snapshot"] = snapshot_of(post)
    if extra:
        rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# 处理时限
# ---------------------------------------------------------------------------

def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def due_at_of(reported_at):
    dt = _parse_dt(reported_at)
    if dt is None:
        return None
    return (dt + timedelta(hours=config.MODERATION_DUE_HOURS)).isoformat(timespec="seconds")


def overdue_of(post, now=None):
    """这条**超时了没有**。返回 `(reported_at, age_hours, due_at, overdue)`。

    **只对 `pending` 判定。** 理由：
    - `hidden` / `deleted` / `rejected` 都是**已经处置过**的（我们动过手），
      它们不需要再被计进"积压"；
    - `visible` 且被举报过但没到阈值 —— 那是设计上**自动不予处置**的，
      不是积压（阈值机制就是"多数人认为有问题才人工看"）。
    所以"积压"= 已经进队列但还没被处置的，正好是 `pending`。

    没有 `reported_at` 的记录（老数据 / 从没被举报）返回 `overdue=False`
    而不是 `True` —— **不知道不能算成"超时了"**。
    """
    now = now or datetime.now()
    reported = post.get("first_reported_at")
    if not reported:
        return None, None, None, False
    dt = _parse_dt(reported)
    if dt is None:
        return reported, None, None, False
    age = round((now - dt).total_seconds() / 3600.0, 2)
    due = due_at_of(reported)
    if post.get("review_status") != PENDING_STATUS:
        return reported, age, due, False
    return reported, age, due, age > config.MODERATION_DUE_HOURS


def queue_item(post, now=None):
    """队列里的一条。审核的人要能一眼看出**为什么**被举报（原有的 reports 保留）
    和**等了多久**（新增的时限三件套）。"""
    reported, age, due, over = overdue_of(post, now=now)
    item = dict(post)                      # 后台可以看全字段，含 author_real_id
    item["reports"] = post.get("reports") or []
    item["reported_at"] = reported
    item["age_hours"] = age
    item["due_at"] = due
    item["overdue"] = over
    item["due_hours"] = config.MODERATION_DUE_HOURS
    item["appeal_status"] = post.get("appeal_status") or "none"
    item["appeal"] = post.get("appeal")
    return item


# ---------------------------------------------------------------------------
# 限速
# ---------------------------------------------------------------------------
#
# **去重管"同一条"，限速管"广撒网"。**
#
# 举报去重（同一 reporter 对同一帖只计一次）拦不住一个人去举报 50 个不同的帖子，
# 也拦不住刷屏发帖 —— 那是两个不同的问题，需要两个机制，都不能省。
#
# **窗口在内存里，不跨重启**（契约第 13 节写明了这是最小实现）。
# 落盘一份时间戳会让 state.json 一直涨，而它防的是"短时间广撒网"，
# 重启清零对这个威胁几乎没有损失。
# ---------------------------------------------------------------------------

class RateLimiter:
    """滑动窗口限速。**查和记账是分开的两步** —— 见 `check` 的说明。"""

    def __init__(self):
        self._hits = {}                    # (bucket, key) -> [ts, ...]

    def _prune(self, bucket, key, window_s, now):
        k = (bucket, str(key))
        hits = [t for t in self._hits.get(k, []) if now - t < window_s]
        if hits:
            self._hits[k] = hits
        else:
            self._hits.pop(k, None)
        return k, hits

    def check(self, bucket, key, limit, window_s, now=None):
        """**只查，不记账。** 返回 `None` 表示放行；否则返回带原因和重试时间的 dict。

        **为什么查和记账要分开**：记账应当在"这次真的产生了内容"之后做。
        把校验失败的请求也计入配额，等于**用户表单填错三次就被锁一小时** ——
        限速是防"广撒网发内容"，而一条 400 什么内容都没产生。
        （我第一版就是查和记账一体，门禁里那条"缺必填字段要 400"的用例
        正好帮我把这件事想清楚了。）

        **不返回笼统的"429 太多"**：调用方要知道是哪个限制、上限多少、
        窗口多长、还要等多久。只给一个 429 状态码，用户只能反复重试。
        """
        now = now if now is not None else time.time()
        k, hits = self._prune(bucket, key, window_s, now)
        if len(hits) >= limit:
            retry_after = int(window_s - (now - hits[0])) + 1
            return {
                "code": "RATE_LIMITED",
                "bucket": bucket,
                "limit": limit,
                "window_s": window_s,
                "used": len(hits),
                "retry_after_s": max(1, retry_after),
                "message": (f"{bucket} 太频繁：{window_s} 秒内最多 {limit} 次，"
                            f"已用 {len(hits)} 次，请 {max(1, retry_after)} 秒后再试"),
            }
        return None

    def record(self, bucket, key, now=None):
        """记一次**成功的**动作。只在内容真的产生之后调。"""
        now = now if now is not None else time.time()
        k = (bucket, str(key))
        hits = self._hits.setdefault(k, [])
        hits.append(now)
        return len(hits)

    def reset(self, bucket=None, key=None):
        """测试用。**不是给业务调的** —— 生产上放行记录只由时间窗口过期。"""
        if bucket is None:
            self._hits.clear()
            return
        if key is None:
            for k in [k for k in self._hits if k[0] == bucket]:
                self._hits.pop(k, None)
            return
        self._hits.pop((bucket, str(key)), None)

    def used(self, bucket, key, window_s, now=None):
        now = now if now is not None else time.time()
        return len([t for t in self._hits.get((bucket, str(key)), [])
                    if now - t < window_s])


# ---------------------------------------------------------------------------
# 申诉
# ---------------------------------------------------------------------------

def can_appeal(post):
    """能不能申诉。返回 `(ok, error)`。

    只允许 `hidden` / `deleted` —— 那是作者**会想要一个说法**的两种状态。
    `visible` / `rejected` 本来就在台上，没什么可申诉的；
    `pending` 是还没处置，等结果就行。

    **顺序要紧：先查申诉状态、再查帖子状态。**
    一个已经申诉并恢复的帖子，两个条件同时不满足（状态成了 `rejected`、
    申诉也结案了）。这时更有信息量的是"你已经申诉过且结案了"（409 ——
    请求本身没问题，只是和已有状态冲突），而不是笼统的"当前状态不能申诉"（400）。
    我第一版顺序反了，门禁里那条"结案后不能重复申诉"的用例把它指出来了。
    """
    if post is None:
        return False, {"code": "NOT_FOUND", "message": "帖子不存在"}
    ap_status = post.get("appeal_status") or "none"
    if ap_status == "pending":
        return False, {"code": "CONFLICT", "message": "这条帖子已经有一个待处理的申诉"}
    if ap_status in APPEAL_TERMINAL:
        return False, {"code": "CONFLICT",
                       "message": f"这条帖子的申诉已经结案（{appeal_result_label(ap_status)}），"
                                  f"不能重复申诉"}
    st = post.get("review_status")
    if st not in ("hidden", "deleted"):
        return False, {"code": "BAD_REQUEST",
                       "message": f"当前状态 {st} 不能申诉（只有 hidden / deleted 可以）"}
    return True, None


def appeal_result_label(status):
    return {"pending": "待处理", "upheld": "维持原处置", "restored": "申诉成立，已恢复"}.get(
        status, "未申诉")


# ---------------------------------------------------------------------------
# 处置结果可查（**别让举报人看到举报人是谁**）
# ---------------------------------------------------------------------------

def viewer_role(post, user):
    """这个 `user` 和这条帖子是什么关系。决定他能看到多少。

    这是"处置结果可查"和"匿名"之间的那根线：
    - 作者该知道自己的帖子怎么了、**按什么类别**（类别不是举报人）；
    - 举报人只该知道"我们处理过了" —— 告诉他处置细节，等于把
      "谁举报的、举报了什么"这些信息通过结果反推出去；
    - 路人什么都不该知道。
    """
    if not user:
        return "stranger"
    if user == post.get("author_real_id"):
        return "author"
    for r in (post.get("reports") or []):
        if r.get("reporter_id") == user:
            return "reporter"
    return "stranger"


def moderation_view(post, user):
    """处置结果视图。**按关系裁剪**，不是把后台那条记录直接给出去。"""
    role = viewer_role(post, user)
    if role == "stranger":
        return None, {"code": "NOT_FOUND",
                      "message": "看不到这条帖子的处置信息（只有作者和举报人可以查）"}

    status = post.get("review_status")
    reported, age, due, over = overdue_of(post)

    if role == "reporter":
        # **只给"已处理"三件套。** 不给类别、不给备注、不给时间线 ——
        # 那些会让举报人反推出处置是不是因为自己那条举报，
        # 更重要的是：举报人不需要知道，给了就多一份泄露面。
        handled = status not in ("visible", "pending")
        out = {
            "post_id": post.get("id"),
            "your_role": "reporter",
            "handled": handled,
            "message": "已处理" if handled else "已收到，正在处理",
        }
        if not handled:
            out["due_at"] = due
        return out, None

    # ---- 作者：该知道的都给 ----
    out = {
        "post_id": post.get("id"),
        "your_role": "author",
        "status": status,
        "status_label": {
            "visible": "正常展示", "pending": "待审核（暂不展示）",
            "hidden": "已下架", "rejected": "已恢复展示", "deleted": "已删除",
        }.get(status, status),
        # **类别，不是举报人**：作者有权知道依据，无权知道是谁
        "reason_code": post.get("moderation_reason_code"),
        "reason_label": (reason_label(post.get("moderation_reason_code"))
                         if post.get("moderation_reason_code") else None),
        "moderated_at": post.get("moderated_at"),
        "reported_at": reported,
        "due_at": due,
        "overdue": over,
        "appeal_status": post.get("appeal_status") or "none",
        "appeal_status_label": appeal_result_label(post.get("appeal_status") or "none"),
    }
    ap = post.get("appeal")
    if ap:
        out["appeal"] = {
            "appeal_id": ap.get("appeal_id"),
            "submitted_at": ap.get("submitted_at"),
            "status": ap.get("status"),
            "resolved_at": ap.get("resolved_at"),
            "result_note": ap.get("result_note"),
        }
    return out, None
