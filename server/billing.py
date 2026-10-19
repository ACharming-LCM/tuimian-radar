# -*- coding: utf-8 -*-
"""付费墙：套餐定义 + `/api/me` 的响应形状。

`fixtures/me.response.json` 就是这一层的验收标准 —— 顶层键是
`user` / `plans` / `_note`（**注意 `_note` 也是顶层键**，不是只有前两个）。

免费额度口径（`contracts/api.md` 第 104 行）：
- 每天可看 **3 条公告详情**；**列表不受限**
- `deadline` 提醒、套磁信、论坛全文归订阅

B 对"deadline 提醒归订阅"的处理**故意不打码列表里的 `deadline` 字段**：
一个公开公告的截止日期本来就能在原文看到，打码只会让 C 的卡片显示错误信息
（"截止日待确认"会变成谎话）。付费价值放在"截止日**推送提醒**"这种增量服务上。
这条判断已写进 issues/007 请 A 确认。
"""
from __future__ import annotations

from datetime import date

# 套餐：逐字对齐 fixtures/me.response.json，别自己改价
PLANS = [
    {
        "id": "monthly",
        "name": "月度订阅",
        "price": 29,
        "period": "月",
        "months": 1,
        "features": ["截止日提醒", "套磁信生成", "论坛全文", "变更推送"],
    },
    {
        "id": "season",
        "name": "推免季畅读",
        "price": 99,
        "period": "5 个月",
        "months": 5,
        "features": ["以上全部", "背景匹配报告", "面试题库"],
    },
]

# 回应里必须带的说明。**不许省** —— 评委看到"支付成功"要能看出是模拟的。
DEMO_NOTE = "D4 演示用模拟支付，不接真实支付通道（学生拿不到微信支付商户号）"

FREE_DAILY_DETAIL_QUOTA = 3


def find_plan(plan_id):
    for p in PLANS:
        if p["id"] == plan_id:
            return p
    return None


def plans_for_api():
    """给前端用的套餐列表：去掉内部的 `months`（那是服务端算到期日用的）。

    fixture 里没有 `months`，所以 API 响应里也不能有 —— 否则和样例对不上。
    """
    return [{k: v for k, v in p.items() if k != "months"} for p in PLANS]


def user_for_api(u, subscribed):
    """`/api/me` 里的 user 对象。字段与 fixture 逐字一致。"""
    return {
        "id": u["id"],
        "nickname": u["nickname"],
        "subscribed": subscribed,
        "expires_at": u.get("expires_at"),
        "daily_free_quota": u["daily_free_quota"],
        "daily_used": u["daily_used"],
    }


def me_response(u, subscribed):
    return {
        "user": user_for_api(u, subscribed),
        "plans": plans_for_api(),
        "_note": DEMO_NOTE,
    }


def quota_exceeded_payload(quota, used):
    """额度用尽的 402 响应体。

    不给正文 —— 返回了就等于没墙。但要**告诉用户怎么解锁**，
    只丢一个 403 不说明原因是最讨人厌的做法。
    """
    return {
        "error": {
            "code": "QUOTA_EXCEEDED",
            "message": f"今日免费额度已用完（{used}/{quota} 条公告详情）",
        },
        "quota": {"daily_free_quota": quota, "daily_used": used, "reset_at": f"{date.today().isoformat()}T24:00:00"},
        "upgrade_hint": {
            "title": "订阅后可无限查看公告详情",
            "plans": plans_for_api(),
            "endpoint": "POST /api/subscribe",
            "_note": DEMO_NOTE,
        },
    }
