# -*- coding: utf-8 -*-
"""推免雷达 · 后端服务（B 负责，只改 server/ 下的文件）

启动（**演示时一条命令，见项目根 RUN.md**）：
    D:\\python.3.10.11\\python.exe -m uvicorn server.app:app --port 8000

验收（B-KICKOFF 的第一件事）：
    curl "http://127.0.0.1:8000/api/notices?page=1&size=5"   -> total = 78

已实现（**23 个端点，全部可用**）：
    GET  /api/health              存活 + 数据加载自检
    GET  /api/facets              筛选项清单（带真实条数，避免前端出现空选项）
    GET  /api/notices             列表：筛选 + 排序 + 分页（形状 = fixtures）
    GET  /api/notices/{id}        详情：完整记录，含 body_text / attachments
    POST /api/match               背景匹配：可解释打分（形状 = fixtures/match.response.json）
    POST /api/letter              套磁信草稿：**模板版**，引文逐条可回链
    GET  /api/faculty             导师列表（含 group_kind）
    GET  /api/faculty/facets      导师筛选项 + 缺失统计
    POST /api/faculty/match       导师匹配（"专业契合的实验室"那一层）
    GET  /api/posts               论坛列表（只给前台可见的）
    GET  /api/posts/facets        版块/标签建议（含 A 产出的院系初始种）
    POST /api/posts               发帖（后台实名、前台匿名）
    POST /api/posts/{id}/report   举报，进审核队列（同一人只计一次）
    GET  /api/admin/queue         审核后台（演示用假鉴权）
    POST /api/admin/posts/{id}    审核动作：hide / restore / delete
    GET  /api/me                  登录态、订阅状态、今日剩余额度
    POST /api/subscribe           模拟支付开通订阅（不接真实通道）
    GET  /api/changes             今日更新：新增 / 有变化 / 本轮未抓到 / id 变更
    GET  /api/changes/history     更新历史（"每天在跑"的凭据）
    POST /api/watch               加一条关注规则（幂等：同规则重复 POST 只有一条）
    GET  /api/watch               列出关注规则
    DELETE /api/watch/{id}        取关（不存在的 id 返回 404，不静默成功）
    GET  /api/digest              **提醒摘要：跟我有关的、变了的东西 ← 月度订阅卖的就是这个**
    GET  /api/freshness           **数据新鲜度：每所学校什么时候抓的 ← "每天更新"的凭据**
    POST /api/admin/crawl         触发一次抓取（单飞，提交 + 轮询）
    GET  /api/admin/crawl/{id}    某次抓取的状态
    GET  /api/admin/crawl         抓取运行日志（最近几次）

最后四个是契约 api.md 第 11 节「关注与提醒」。`/api/changes` 回答"这一轮变了什么"
（所有人的、同一份），`/api/digest` 回答"**和我有关的**变了什么" —— 中间那段就是付费理由：
没有关注，日报每天长得一样，用户看两天就不打开了；有了关注，每天的结果都是他没见过的。
**`digest` 只发 `added` / `changed`**（"外面的世界变了"），
`reparsed` / `reidentified` / `removed` 一条都不出现（"我们的处理变了"）。

**没有一个端点在等别人**：`/api/letter` 用模板版先顶上（`method: "template"`），
不依赖 LLM 的模型选型与 key 归属。
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server import billing
from server import changes as changes_mod
from server import config
from server import crawl as crawl_mod
from server import credibility
from server import digest as digest_mod
from server import forum
from server import heartbeat as heartbeat_mod
from server import freshness as freshness_mod
from server import governance as gov
from server import letter as letter_mod
from server import live
from server import match as matching
from server import profile as profile_mod
from server import push as push_mod
from server import quality
from server import updates as updates_mod
from server import watch as watch_mod
from server.changes import ChangesStore
from server.crawl import CrawlRunner
from server.digest import DigestStore
from server.events import EventStore
from server.freshness import FreshnessStore
from server.history import HistoryStore
from server.state import StateStore
from server.store import FacultyStore, NoticeStore, load_health

DESCRIPTION = """\
推免/招生公告接口。数据源是 `data/out/notices.jsonl` 与 `data/out/faculty.jsonl`
（A 的管道产出，只读，**按 mtime 按需重载**：重跑管道后接口会自己跟上）。

契约见 `contracts/api.md`：字段定义在 `contracts/notice.schema.json` 与
`contracts/faculty.schema.json`；打分规则见 `docs/matching-spec.md`。
列表与匹配的响应形状分别与 `contracts/fixtures/notices.list.json`、
`contracts/fixtures/match.response.json` 完全一致（回归测试盯着这两条）。
"""


def _json(payload, status_code=200, headers=None):
    # 契约要求 application/json; charset=utf-8。FastAPI 默认会给 charset，这里写死更保险。
    return JSONResponse(content=payload, status_code=status_code, headers=headers,
                        media_type="application/json; charset=utf-8")


def _pct(rate):
    """比率 → 人能读的百分数。`None`（没有记录）说"无"而不是 "0.0%" ——
    0% 和"没数据"是两回事（本项目反复出现的那条）。"""
    return "无" if rate is None else f"{rate * 100:.1f}%"


def _error(status_code, code, message):
    # 统一错误格式：{"error": {"code": ..., "message": ...}}（契约第 6 节）
    return _json({"error": {"code": code, "message": message}}, status_code=status_code)


def _public_blocked(app, user) -> bool:
    """公网演示模式：访客不能改演示身份的状态（guest_xxx 可以）。"""
    if not getattr(app.state, "public_mode", False):
        return False
    uid = (user or "u_demo").strip()
    return uid in config.DEMO_USERS


def _public_readonly_error(what="这个操作"):
    return _error(403, "PUBLIC_READONLY",
                  f"公开演示版暂不开放「{what}」—— 演示身份是只读的。"
                  "需要完整体验可以在本地运行项目（见 README / DEPLOY.md）。")


def _admin_ok(token) -> bool:
    """管理口令比对，**唯一实现**（以前 7 个路由各写一遍，其中一个漏过）。
    用常量时间比较，避免按响应时间逐字符猜口令。"""
    import secrets
    return bool(token) and secrets.compare_digest(str(token), config.ADMIN_TOKEN)


def _as_profile(body: Any) -> dict:
    """兼容两种请求体：裸画像 `{...}` 和包一层 `{"profile": {...}}`。

    契约 api.md 第 2 节给的是裸画像（和 fixture 里 `profile` 的内容一致），
    但 `POST /api/letter` 用的是包子形状 `{"notice_id": ..., "profile": {...}}`。
    C 两种写法都可能用，所以两种都收 —— 这不是放宽契约，是少一次返工。
    """
    if isinstance(body, dict) and isinstance(body.get("profile"), dict):
        return body["profile"]
    return body


# 变更报告的按需缓存：(mtime, size) -> ChangesStore
_CHANGES_CACHE: dict = {}
# 更新历史索引的按需缓存，同上
_HISTORY_CACHE: dict = {}
_DIGEST_CACHE: dict = {}
_FRESHNESS_CACHE: dict = {}
# 事件流（契约第 16 节）。key = (mtime_ns, size) 或 ("missing", "missing")。
_EVENTS_CACHE: dict = {}
_CRAWL_RUNNER = None
CRAWL_RUNS_LIST_DEFAULT = 20


def _default_crawler() -> CrawlRunner:
    """进程内**唯一**的抓取运行器。

    **必须是单例**：单飞的锁在那对象里。每次请求 new 一个的话，
    两个并发请求各拿一把新锁，就都认为自己该跑 —— 单飞形同虚设。
    调度器和 HTTP 端点也必须共用同一个，否则"定时器在跑"和"手动触发"
    会同时开两个子进程，正好是单飞要防的事。
    """
    global _CRAWL_RUNNER
    if _CRAWL_RUNNER is None:
        _CRAWL_RUNNER = CrawlRunner(state_path=config.CRAWL_RUNS_PATH,
                                    freshness_path=config.FRESHNESS_PATH)
    return _CRAWL_RUNNER


def _load_history_cached() -> HistoryStore:
    path = config.HISTORY_INDEX_PATH
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = ("missing", "missing")
    cached = _HISTORY_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = HistoryStore.load(path)
    _HISTORY_CACHE.clear()
    _HISTORY_CACHE[key] = loaded
    return loaded


def _load_changes_cached() -> ChangesStore:
    """按 mtime + size 判断要不要重读 `changes.json`。

    为什么不做"启动时读一次"：变更报告是**每次抓取后重写**的文件，
    服务开着的时候 A 完全可能再跑一轮 —— 启动时读一次会让页面永远停在旧报告。
    为什么不做"每次请求都读"：文件有 626 KB（含完整记录），能省就省。
    """
    path = config.CHANGES_PATH
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        # 文件不存在：交给 load() 返回"基线未建立"的空报告（不 500）
        key = ("missing", "missing")
    cached = _CHANGES_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = ChangesStore.load(path)
    _CHANGES_CACHE.clear()          # 只留最新一份，避免长跑服务堆积
    _CHANGES_CACHE[key] = loaded
    return loaded


def _load_digest_cached() -> DigestStore:
    """`/api/digest` 的数据源按 mtime 缓存。

    **比 changes 那边多一层**：digest 要读**一整个目录**（`history/*.json`），
    所以缓存键是"changes.json + 目录里每个文件"的 (mtime, size) 组合摘要 ——
    只盯 changes.json 的话，A 跑完 run_daily 归档出新的一天，
    digest 会看不见那一天的记录（而 history 接口看得见，两者就对不上了）。
    """
    parts = []
    try:
        st = os.stat(config.CHANGES_PATH)
        parts.append(("changes", st.st_mtime_ns, st.st_size))
    except OSError:
        parts.append(("changes", "missing", "missing"))
    hist = os.path.dirname(config.HISTORY_INDEX_PATH)
    if os.path.isdir(hist):
        for name in sorted(os.listdir(hist)):
            if not name.endswith(".json"):
                continue
            try:
                st = os.stat(os.path.join(hist, name))
                parts.append((name, st.st_mtime_ns, st.st_size))
            except OSError:
                parts.append((name, "missing", "missing"))
    key = tuple(parts)
    cached = _DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = DigestStore.load(config.CHANGES_PATH, hist)
    _DIGEST_CACHE.clear()
    _DIGEST_CACHE[key] = loaded
    return loaded


def _load_freshness_cached() -> FreshnessStore:
    """按 mtime + size 判断要不要重读 `freshness.json`。

    **用和公告/导师一样的那套（live 的思路），不是启动时读死。**
    这个文件每天被覆盖一次，读死会让 `/api/freshness` 永远停在启动那一刻 ——
    而它存在的唯一目的就是回答"数据是什么时候的"，答错了比没有更坏。
    """
    path = config.FRESHNESS_PATH
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = ("missing", "missing")
    cached = _FRESHNESS_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = FreshnessStore.load(path)
    _FRESHNESS_CACHE.clear()
    _FRESHNESS_CACHE[key] = loaded
    return loaded


def _load_events_cached():
    """按 `(mtime_ns, size)` 重载事件流（和新鲜度那套同一个思路）。

    **文件不存在不抛异常** —— `EventStore.load` 会给出 `source_kind="none"`，
    接口因此能诚实地说"事件文件还没写出来"，而不是 500。

    key 在文件不存在时是 `("missing", "missing")`；文件后来出现时 key 变了，
    所以**不用重启服务**就能读到新事件（和 `live.py` 同一个理由）。
    """
    path = config.EVENTS_PATH
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = ("missing", "missing")
    cached = _EVENTS_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = EventStore.load(path)
    _EVENTS_CACHE.clear()
    _EVENTS_CACHE[key] = loaded
    return loaded


def create_app(store: NoticeStore, faculty: FacultyStore | None = None,
               state: StateStore | None = None,
               changes: ChangesStore | None = None,
               digest: DigestStore | None = None,
               freshness: FreshnessStore | None = None,
               crawl: CrawlRunner | None = None,
               rate_limiter: gov.RateLimiter | None = None,
               events: EventStore | None = None,
               public_mode: bool | None = None) -> FastAPI:
    app = FastAPI(title="推免雷达 API", version="0.8.0", description=DESCRIPTION)
    # 公网演示模式（Dockerfile 里通过 TUIMIAN_PUBLIC=1 打开）。
    # 门禁可以显式传 public_mode=True 来测，不依赖进程环境变量。
    app.state.public_mode = (config.PUBLIC_MODE if public_mode is None else public_mode)

    @app.middleware("http")
    async def _public_guard(request, call_next):
        # ① 请求体上限：写接口都是 JSON，超大 body 直接拒
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > config.MAX_REQUEST_BYTES:
            return _error(413, "PAYLOAD_TOO_LARGE",
                          f"请求体超过 {config.MAX_REQUEST_BYTES // 1024} KB")
        response = await call_next(request)
        # ② 告诉前端"这是公网演示版"（前端据此给访客分配 guest_xxx 身份）
        if app.state.public_mode:
            response.set_cookie("tuimian_public", "1", samesite="lax", httponly=False)
        return response

    # **公告和导师都按需重载**（见 server/live.py 的模块 docstring）。
    # 踩过的坑（C 报的）：原来启动时读一次就再也不看磁盘，
    # 于是 A 每重跑一次管道，/api/notices 还在返回旧数据 ——
    # HTTP 200、形状对、就是内容旧的，属于"它不说谎，它只是过时"那类最难发现的 bug。
    #
    # 传实例就原样用（测试注入固定数据），传工厂就包成 LiveStore（生产路径）。
    # 注意：包了 LiveStore 之后闭包里要用 live_store，不能再用原来的 `store` 变量。
    live_store = live.as_live(store, config.NOTICES_PATH,
                              lambda: NoticeStore.load(config.NOTICES_PATH))
    app.state.store = live_store

    live_faculty = live.as_live(
        faculty, config.FACULTY_PATH, lambda: FacultyStore.load(config.FACULTY_PATH))
    app.state.faculty = live_faculty

    # 没注入 state 时（真启动）→ **首次运行自动建演示状态**（B26，2026-10-19）。
    # 用 `load_or_seed` 而不是 `load`：后者文件缺失时返回空状态且不落盘，
    # 新克隆的仓库第一次打开会是"没有 u_demo / u_num_demo"的空站。
    state = state if state is not None else StateStore.load_or_seed(config.STATE_PATH)
    app.state.state = state
    # 变更报告：按 mtime+size 按需重读（见 _load_changes_cached），
    # 测试可以注入一个固定实例。
    app.state.changes = changes
    # 提醒摘要的数据源（每日差异报告）。同样可以注入固定实例 ——
    # **门禁里必须注入**：契约第 11 节的 7 条用例明确"不许依赖磁盘上恰好是什么数据"。
    app.state.digest = digest
    # 新鲜度（契约第 12 节）。注入固定实例 = 门禁不依赖磁盘上恰好是什么数据。
    app.state.freshness = freshness
    # 抓取运行器（单飞 + 子进程）。**测试注入假命令**，绝不在门禁里真抓 37 所学校。
    app.state.crawl = crawl
    # 事件流（契约第 16 节）。注入固定实例 = 门禁不依赖磁盘上恰好是什么数据
    # （而且 `data/out/events.jsonl` 今天是空的，赌它等于没验）。
    app.state.events = events

    # 限速器（契约第 13 节）。**每个 app 实例一个**：
    # 生产环境 `create_app` 只被调用一次，所以它天然跨请求累计（限速才有意义）；
    # 而门禁里每个用例 `create_app` 一次 → 各自一个干净的计数器。
    #
    # 我第一版把它放成**模块级单例**，结果门禁里发帖几次之后就开始 429 ——
    # 用例之间通过一个谁也没注意到的全局变量互相污染。
    # 和之前 state 那个坑是同一类：**跨用例的共享可变状态**。
    rate_limiter = rate_limiter if rate_limiter is not None else gov.RateLimiter()
    app.state.rate_limiter = rate_limiter

    # ---------- 自检 ----------

    @app.get("/api/health/data", summary="数据心跳：我们最近跑成功了吗")
    def health_data():
        """**"定时任务还在跑吗"**（契约 §21）。

        和 `/api/freshness`（§12）的分工：
        - `/api/freshness`：**每所学校**上次抓到什么时候（数据源的状况）；
        - 这里：**我们这条流水线**跑成功没有（我们自己的状况）。

        为什么必须有这个接口：任务没跑的时候，页面上一切正常 —— 接口 200、
        形状对、数据是上一次的。**它不说谎，它只是过时**，而没有任何东西会报警。

        `ok` 是三态：`true` / `false` / **`null`（不知道）**。
        `null` 不是失败，是"日志里既没有退出码也没有走到最后那行的证据"。
        把 `null` 说成 `false` 会让人去查一个没坏的东西；
        说成 `true` 会让你在任务真的挂掉时以为一切正常。
        """
        # 把**已经加载在内存里**的记录传进去：不传的话它会把 10 MB 的
        # notices.jsonl 再解析一遍（实测 218 ms），而这个接口挂在页头上。
        ev_recs = None
        try:
            ev_recs = _load_events_cached().events()
        except Exception:                       # noqa: BLE001
            ev_recs = None                      # 读不到就走它自己的兜底
        return _json(heartbeat_mod.summary(
            notice_records=live_store.records, event_records=ev_recs))

    @app.get("/api/health", summary="存活 + 数据加载自检")
    def health():
        meta = live_store.load_meta
        fr = (freshness if freshness is not None
              else _load_freshness_cached()).response()
        # **加载自检要能被机器判断，不能只是一串裸数。**
        # 原来这里只有 `loaded: 195` / `skipped_lines: 1` 两个数字：
        # 缺了一条公告、和一条都不缺，响应长得几乎一样（都是 200、都有 loaded），
        # 只有把两个数**对着比**才看得出来 —— 而那正是没有人会做的事。
        # 现在多一个 `load.state`（ok / degraded / empty）和一句人话 message：
        #   degraded = 数据源里有行坏了、被跳过，服务仍可用但少数据（要响）
        #   empty    = 文件读到了但 0 条（不是"筛选没命中"）
        # 判据只有一处：`server/store.py` 的 `load_health()` —— 门禁也调它，
        # 所以"接口说没事"和"门禁说没事"不可能分叉。
        load = load_health(meta)
        # **必须走 `_json()`，不能直接返回 dict。**
        # 直接返回 dict 时 FastAPI 给的是 `application/json`（**不带 charset**），
        # 而按 HTTP 规范没有 charset 默认是 Latin-1 —— 守规范的客户端会把
        # "文档"解成 `ææ¡£`。这个坑真踩了：/api/health 是演示时第一个被看到的接口，
        # 却是唯一漏掉 charset 的那个。有测试守着全部接口。
        return _json({
            "status": "ok",
            "data": {
                "path": meta["path"],
                "loaded": meta["loaded"],
                "unique_ids": meta["unique_ids"],
                # 重复 id 归零是 A 归一化 url 的结果。留着这个数是为了它一旦
                # 重新涨上去能被看见，而不是悄悄复发（历史上是 8 组）。
                "duplicate_ids": len(meta["duplicate_ids"]),
                "skipped_lines": meta["skipped"],
                # ---- 加载自检（机器可判断）----
                # 平铺一份 `load_state` 给"一眼看"，完整结构在 `load` 里。
                "load_state": load["state"],
                "load": load,
                "faculty_loaded": len(live_faculty.records),
                "posts": len(state.posts()),
                # 按需重载的次数。**演示前瞄一眼这个数**：如果 A 刚重跑过管道，
                # 它应该 > 0（说明接口已经跟上了最新数据，不用重启服务）。
                "store_reloads": getattr(live_store, "reload_count", 0),
                "faculty_reloads": getattr(live_faculty, "reload_count", 0),
                # ---- 新鲜度（契约第 12 节）----
                # **"数据是什么时候的"必须能从这里一眼看到。**
                # 产品的头号承诺是"每天更新"，在这两个字段之前它不可验证。
                "oldest_hours": fr["oldest_hours"],
                "stale_schools": fr["totals"]["stale_schools"],
                "failed_schools": fr["totals"]["failed_schools"],
                "empty_schools": fr["totals"]["empty_schools"],
                "freshness_worst": fr["worst"],
                "data_generated_at": fr["generated_at"],
            },
            "public_mode": bool(app.state.public_mode),
            # 鉴权方式在这里**说明**，但口令本身绝不经接口返回
            # （以前这里会原样返回 admin_token，部署到公网等于没有鉴权）。
            "demo_auth": {
                "admin_header": "X-Admin-Token",
                "token_source": "env" if config.ADMIN_TOKEN_FROM_ENV else "random_per_start",
                "note": "单一共享口令，不是权限系统，别拿它当安全边界；"
                        "口令见环境变量 TUIMIAN_ADMIN_TOKEN 或服务端启动日志",
            },
            "state": {
                "path": state.path,
                "note": "服务端运行时状态（帖子/订阅/额度），不在 data/out/ 下",
                # ---- 状态文件体检（契约第 15 节 / 环 4）----
                # `ok: false` = **这次启动是从一个坏掉的状态文件里重建的**：
                # 原文件已被改名为 `<path>.broken-<时间戳>`（内容没丢），
                # 但帖子 / 订阅 / 关注 / 审计**一条都没有恢复**。
                #
                # **静默重建是不能接受的**：审计是法律意义上要留痕的东西，
                # 而"从空状态重启"和"新装了一个站"在界面上长得一模一样。
                # 所以这个字段和 `recovery.message` 一起给出去 —— 人能读，机器能判。
                "ok": state.recovery is None,
                "recovery": state.recovery,
            },
        })

    # ---------- 公告 ----------

    @app.get("/api/facets", summary="筛选项清单（真实条数）")
    def facets():
        # 同上：走 `_json()` 才有 charset
        return _json(live_store.facets())

    @app.get("/api/notices", summary="公告列表：筛选 + 排序 + 分页")
    def list_notices(
        request: Request,
        school: str | None = Query(None, description="学校全称，子串匹配"),
        unit: str | None = Query(None, description="院系，子串匹配，逗号分隔可多选"),
        apply_mode: str | None = Query(None, description="推免/统考/申请考核/夏令营，逗号分隔可多选"),
        level: str | None = Query(None, description="硕士/博士/直博，逗号分隔可多选"),
        year: int | None = Query(None, description="招生年份，精确匹配 target_year"),
        q: str | None = Query(None, description="关键词：先匹配标题/单位/专业/标签，再退到正文"),
        sort: str = Query("publish_desc", description="publish_desc（默认）/ deadline_asc"),
        page: int = Query(1, ge=1, description="页码，从 1 开始"),
        size: int | None = Query(None, ge=1, le=config.MAX_PAGE_SIZE,
                                 description=f"每页条数，上限 {config.MAX_PAGE_SIZE}（默认 10）"),
        limit: int | None = Query(
            None, ge=1, le=config.MAX_PAGE_SIZE,
            description="`size` 的别名（2026-10-19 补：它是合法参数，不能只白名单不生效）"),
        offset: int | None = Query(
            None, ge=0, description="从第几条开始（优先于 page，B25）"),
        meta: bool = Query(False, description="是否附带 warnings / extract_meta（前端标'疑似'用）"),
        age_tier: str | None = Query(
            None,
            description="时效分层：live / recent / archive，逗号分隔可多选"
                        "（默认返回全部；前端默认口径是 live,recent）"),
        include_junk: bool = Query(
            False,
            description="是否包含 record_quality=junk 的脏记录（默认拒收；要复查时才开）"),
        notice_kind: list[str] | None = Query(
            None,
            description="公告类型：announcement / catalog / emba / other，"
                        "可重复传或逗号分隔多选（2026-10-19 新增，B24）"),
        topic: str | None = Query(
            None, description="主题：apply / non_apply / other，逗号分隔可多选（B25）"),
        deadline_within: int | None = Query(
            None, description="只看 N 天内截止（1/3/7/30，只算 found、不含已截止，B25）"),
        include_expired: bool = Query(
            False,
            description="`sort=deadline_asc` 时是否把已截止的也列出来（默认不列；"
                        "想看已过期的用 `sort=deadline_desc`）"),
    ):
        # ---- 未知查询参数：**不许静默忽略**（B24，2026-10-19）----
        #
        # 事故：`?notice_kind=announcement` 返回的 total 和不传时**一模一样** ——
        # FastAPI 默认忽略不认识的查询参数，于是前端照着文档做的筛选是假功能。
        # 现在把不认识的参数名原样返回，让它**看得出来**。
        unknown = [k for k in request.query_params.keys()
                   if k not in config.NOTICES_QUERY_PARAMS]

        kinds = []
        for raw in (notice_kind or []):
            kinds += [x.strip() for x in raw.split(",") if x.strip()]
        bad_kinds = [k for k in kinds if k not in config.NOTICE_KINDS]
        if bad_kinds:
            # **不许静默当成没筛**：传错值 = 0 条，比报错更难查
            return _error(400, "BAD_NOTICE_KIND",
                          "notice_kind 只能是 %s（收到：%s）"
                          % (" / ".join(config.NOTICE_KINDS), "、".join(bad_kinds)))
        tiers = [t.strip() for t in (age_tier or "").split(",") if t.strip()]
        bad_tiers = [t for t in tiers if t not in config.AGE_TIERS]
        if bad_tiers:
            return _error(400, "BAD_AGE_TIER",
                          "age_tier 只能是 %s（收到：%s）"
                          % (" / ".join(config.AGE_TIERS), "、".join(bad_tiers)))
        topics = [t.strip() for t in (topic or "").split(",") if t.strip()]
        bad_topics = [t for t in topics if t not in config.TOPICS]
        if bad_topics:
            return _error(400, "BAD_TOPIC",
                          "topic 只能是 %s（收到：%s）"
                          % (" / ".join(config.TOPICS), "、".join(bad_topics)))
        if deadline_within is not None and deadline_within not in config.DEADLINE_WINDOWS:
            # 只给 1/3/7/30 四档：`0` / `9999` 这类边界各写一套语义，
            # 而用户要的是"这周/这个月"，不是一个可以随便填的整数。
            return _error(400, "BAD_DEADLINE_WITHIN",
                          "deadline_within 只能是 %s（收到：%d）"
                          % (" / ".join(str(x) for x in config.DEADLINE_WINDOWS),
                             deadline_within))

        # `limit` / `offset`（B25）：**是别名，不是摆设**。
        # 同时给 `size` 和 `limit` 时以 `size` 为准（更明确的名字优先）。
        eff_size = size if size is not None else (limit if limit is not None else 10)
        eff_page = page
        if offset is not None:
            eff_page = offset // eff_size + 1       # 响应里的 page 要和 offset 自洽

        if sort not in config.ALLOWED_SORTS:
            return _error(400, "BAD_SORT",
                          f"sort 只能是 {' / '.join(config.ALLOWED_SORTS)}")
        payload = live_store.list_response(
            school=school, unit=unit, apply_mode=apply_mode, level=level, year=year,
            q=q, sort=sort, page=eff_page, size=eff_size, with_meta=meta,
            include_junk=include_junk, age_tier=age_tier,
            notice_kind=",".join(kinds) or None,
            topic=",".join(topics) or None,
            offset=offset, deadline_within=deadline_within,
            include_expired=include_expired,
        )
        # ---- 按 `notice_kind` 筛时，把这一列**回显**到条目上 ----
        #
        # 它不在默认投影里（`LIST_FIELDS` 是 A 的 `make_fixtures.py` 定的，
        # 而这份 fixture 是逐字段比对的标准；B 不能为了加一个键去改 collector）。
        # 但"筛完看不见被筛的字段"就是这个项目一再要消灭的静默失效 ——
        # 所以**只在筛它的时候**补上，默认形状仍然严格等于 fixture。
        if kinds:
            for it in payload.get("items") or []:
                rec = live_store.get(it.get("id")) or {}
                it["notice_kind"] = rec.get("notice_kind")
        if unknown:
            payload["unknown_params"] = unknown
            payload["_unknown_note"] = (
                "以下查询参数我们不认识，**没有生效**：%s。"
                "参数表见契约 api.md §3。" % "、".join(unknown))
        return _json(payload)

    @app.get("/api/notices/{notice_id}", summary="公告详情：完整记录（免费额度受限）")
    def notice_detail(
        notice_id: str,
        meta: bool = Query(False),
        user: str = Query("u_demo", description="演示用假登录态，见 B-KICKOFF 第 87 行"),
    ):
        rec = live_store.get(notice_id)
        if rec is None:
            # 404 不扣额度：查一个不存在的 id 不该消耗用户的次数
            return _error(404, "NOT_FOUND", "公告不存在")

        # 付费墙：免费用户每天 3 条详情（契约 api.md 第 104 行）。
        # **只在真的能返回记录时才扣**，且扣不动就 402 —— 不许"扣不动就放行"。
        u = state.user(user)
        allowed, remaining = state.consume_quota(u, 1)
        if not allowed:
            payload = billing.quota_exceeded_payload(
                u["daily_free_quota"], u["daily_used"])
            return _json(payload, status_code=402)

        # 默认 = 完整记录本身，键集合和 `fixtures/notices.detail.json` 一致。
        # **额度信息走响应头，不进 body** —— fixture 就是一条原始记录、没有任何额外键，
        # 塞个 `_quota` 进去会让"响应和 fixture 一致"这条硬约束失效。
        # 而且额度是**访问控制状态**，不是公告数据，混在一起早晚有人误用。
        payload = dict(rec)
        # 质量/时效四个字段规范化（契约 §19）：有就透传，缺就补 `unknown`/`null`。
        # 详情里**不做 junk 拦截** —— 按 id 能查到是"可追溯"的底线：
        # 用户报"你们少了一条"，我们得能把那条原样拿出来给他看。
        payload.update(quality.of(rec))
        headers = {}
        if remaining is not None:
            headers = {
                "X-Quota-Limit": str(u["daily_free_quota"]),
                "X-Quota-Used": str(u["daily_used"]),
                "X-Quota-Remaining": str(remaining),
            }
        if meta:
            payload["warnings"] = live_store.warnings_of(rec)
            payload["extract_meta"] = live_store.extract_meta(rec)
            # ---- 字段可信度（契约第 14 节）----
            # **`field_state` 是数据字段**：A 一落盘，`dict(rec)` 就会自动带上它，
            # 这个接口不用改。这里给的是**规范化后的**版本 —— 老记录缺键时补全，
            # 前端照四个键渲染即可，`_source: "inferred"` 说明这是推断的。
            payload["field_state"] = credibility.field_state_of(rec)
            payload["field_trust"] = credibility.untrusted_summary(rec)
            payload["deadline_display"] = credibility.deadline_display(rec)
            variants = live_store.variants(notice_id)
            if len(variants) > 1:
                # 同一 id 有多个 url（历史上浙大物理 8 组 http/https 各抓了一次）。
                # 留给防御：去重万一复发，前端能看见，而不是拿到两条一模一样的卡片。
                payload["_duplicate_variants"] = [v.get("url") for v in variants]
        return _json(payload, headers=headers) if headers else _json(payload)

    # ---------- 匹配（契约 api.md 第 2 节） ----------

    @app.post("/api/match", summary="背景匹配：按契合度排序，每条给出理由")
    def post_match(
        profile: dict = Body(..., description="用户画像，见 docs/matching-spec.md 第二节"),
        limit: int = Query(30, ge=1, le=200, description="最多返回多少条"),
    ):
        # 打分规则照 docs/matching-spec.md；实现是 collector/tools/match_preview.py
        # 的移植（server/match.py），规则一个字没改。
        items, stats = matching.match_notices(
            live_store.records, _as_profile(profile), date.today(), limit=limit)
        return _json({"profile": _as_profile(profile), "stats": stats, "items": items})

    # ---------- 导师（契约 api.md 第 8 节） ----------

    @app.get("/api/faculty", summary="导师列表：筛选 + 分页")
    def list_faculty(
        school: str | None = Query(None),
        unit: str | None = Query(None, description="院系，逗号分隔可多选"),
        group: str | None = Query(None, description="研究所/课题组名，逗号分隔可多选"),
        group_kind: str | None = Query(None, description="身份 / 机构 / 其他，逗号分隔可多选"),
        q: str | None = Query(None, description="在姓名和研究方向里搜"),
        matchable: bool | None = Query(
            None, description="true=只看可匹配的 / false=只看名录级 / 不传=全给"),
        page: int = Query(1, ge=1),
        size: int = Query(50, ge=1, le=config.MAX_PAGE_SIZE),
    ):
        return _json(live_faculty.list_response(
            school=school, unit=unit, group=group, group_kind=group_kind,
            q=q, matchable=matchable, page=page, size=size))

    @app.get("/api/faculty/facets", summary="导师筛选项（含缺失统计）")
    def faculty_facets():
        return _json(live_faculty.facets())

    @app.post("/api/faculty/match", summary="导师匹配：兴趣词 × 研究方向")
    def post_faculty_match(
        profile: dict = Body(..., description="画像，用 interest_keywords（可选）"),
        limit: int = Query(10, ge=1, le=100),
    ):
        items = matching.match_faculty(live_faculty.records, _as_profile(profile))
        return _json({"profile": _as_profile(profile), "items": items[:limit]})

    # ---------- 论坛（契约 api.md 第 4 节） ----------
    #
    # 这一节的字段表、review_status 枚举、举报阈值都是 B 定的默认值 ——
    # api.md 只有一张四行表，没有字段表也没有 fixture。已开 issues/007 请 A/C 确认。
    # 三条不可让步的：匿名代号服务端派生 / 非 visible 的帖子不出现在列表 /
    # author_real_id 与 created_ip 永不返回前台。

    @app.get("/api/posts", summary="论坛列表（只给前台可见的）")
    def list_posts(
        unit: str | None = Query(None, description="院系，逗号分隔可多选"),
        tag: str | None = Query(None, description="标签，逗号分隔可多选"),
        q: str | None = Query(None, description="标题+正文关键词"),
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=config.MAX_POST_PAGE_SIZE),
        user: str = Query("u_demo", description="演示用假登录态"),
    ):
        # 全文归订阅：非订阅者只给前 200 字 + truncated 标记（契约 api.md 第 5 节）
        u = state.user(user)
        limit = None if state.is_subscribed(u) else config.POST_BODY_FREE_LIMIT
        total, items = forum.list_posts(state, unit=unit, tag=tag, q=q,
                                        page=page, size=size, body_limit=limit)
        return _json({"total": total, "page": page, "size": size, "items": items})

    @app.get("/api/posts/facets", summary="论坛版块/标签建议（带 A 产出的院系初始种）")
    def post_facets():
        return _json(forum.facets(state, unit_suggestions=live_faculty.unit_suggestions()))

    @app.post("/api/posts", summary="发帖（后台实名、前台匿名）", status_code=201)
    def create_post(request: Request, payload: dict = Body(...)):
        # 公网演示版：论坛只读（匿名发帖在公开页面不可控）
        if app.state.public_mode:
            return _public_readonly_error("发帖")
        # **限速**（契约第 13 节）：去重管"同一条"，限速管"广撒网"。
        # 举报去重拦不住刷屏发帖 —— 那是两个不同的问题。
        body = payload or {}
        who = (body.get("author_real_id") or "").strip() or forum.client_ip(request) or "anonymous"
        limited = rate_limiter.check("发帖", who, config.POST_RATE_LIMIT,
                                     config.POST_RATE_WINDOW_S)
        if limited is not None:
            # **带原因和重试时间**，不是笼统 429 —— 只给状态码用户只能反复重试
            return _json({"error": limited},
                         status_code=429,
                         headers={"Retry-After": str(limited["retry_after_s"])})
        post, err = forum.build_post(state, body, request=request)
        if err:
            return _error(400, err["code"], err["message"])
        # **校验过了才记账**：一条 400 什么内容都没产生，不该吃掉配额
        # （否则用户表单填错三次就被锁一小时）。
        rate_limiter.record("发帖", who)
        state.add_post(post)
        # 返回的是**前台视图**：author_real_id / created_ip 不出现
        return _json(forum.public_view(post), status_code=201)

    @app.post("/api/posts/{post_id}/report", summary="举报，进审核队列")
    def report_post(post_id: str, payload: dict = Body(default={})):
        if app.state.public_mode:
            return _public_readonly_error("举报")
        body = payload or {}
        reporter = (body.get("reporter_id") or "").strip()
        # **举报也要限速**：去重只保证"同一条不重复计数"，
        # 一个人对 50 个不同帖子各举报一次在去重下是完全合法的刷法。
        limited = rate_limiter.check("举报", reporter or "unknown",
                                     config.REPORT_RATE_LIMIT,
                                     config.REPORT_RATE_WINDOW_S)
        if limited is not None:
            return _json({"error": limited}, status_code=429,
                         headers={"Retry-After": str(limited["retry_after_s"])})

        before_status = None
        p = state.find_post(post_id)
        if p is not None:
            before_status = p.get("review_status")

        post, err, deduped = forum.report_post(
            state, post_id, reason=body.get("reason"), reporter_id=body.get("reporter_id"))
        if err:
            return _error(404 if err["code"] == "NOT_FOUND" else 400,
                          err["code"], err["message"])

        # ---- 审计（契约第 13 节）：收到举报 + 到阈值自动转待审，两条都留痕 ----
        # 重复举报**不写审计**：它没有改变任何状态，写进去只会让日志变得没法读
        # （同一条举报被点十次就有十条记录，真正的那次淹没在里面）。
        if not deduped:
            # 同上：**只有真的新增了一条举报才记账**，重复举报不吃配额
            rate_limiter.record("举报", reporter or "unknown")
            state.append_audit(gov.build_audit(
                "report", post, actor_id=reporter or "unknown", actor_role="reporter",
                reason_note=body.get("reason"),
                from_status=before_status, to_status=post.get("review_status"),
                extra={"report_count": post.get("report_count"),
                       "threshold": forum.REPORT_THRESHOLD}))
            if (before_status != "pending"
                    and post.get("review_status") == "pending"):
                state.append_audit(gov.build_audit(
                    "auto_pending", post, actor_id="system", actor_role="system",
                    reason_code="other", from_status=before_status,
                    to_status="pending",
                    extra={"report_count": post.get("report_count")},
                    with_snapshot=True))

        # 重复举报返回 200 + deduped=true 而不是静默 +1 —— 前端要能区分
        # "这次算数"和"你已经举报过了"，否则用户会以为举报了两遍。
        return _json({
            "id": post["id"],
            "report_count": post["report_count"],
            "review_status": post["review_status"],
            "threshold": forum.REPORT_THRESHOLD,
            "deduped": deduped,
            "note": ("你已经举报过这条了，本次未重复计数" if deduped
                     else "已进入审核队列" if post["review_status"] == "pending"
                     else "已记录举报"),
        })

    @app.get("/api/posts/{post_id}/moderation", summary="处置结果可查（按关系裁剪）")
    def post_moderation(post_id: str,
                        user: str = Query("u_demo", description="演示用假登录态")):
        """**处置结果可查，但别让举报人看到举报人是谁。**

        同一个接口按调用方和帖子的关系返回不同的量：
        - **作者**：状态、**依据类别**（不是举报人）、时间、申诉进展；
        - **举报人**：只有"已处理"三件套 —— 给他细节等于把
          "这条是不是因为我举报才被处理"反推出去，而且他不需要知道；
        - **路人**：看不到。

        类别给作者、举报人给结果，这条线就是"合规"和"匿名"之间的那根线。
        """
        post = state.find_post(post_id)
        if post is None:
            return _error(404, "NOT_FOUND", "帖子不存在")
        view, err = gov.moderation_view(post, user)
        if err:
            return _error(403, err["code"], err["message"])
        return _json(view)

    @app.post("/api/posts/{post_id}/appeal", summary="作者申诉（hidden/deleted）")
    def post_appeal(post_id: str, payload: dict = Body(...)):
        """**申诉带原文快照** —— 不能因为下架就丢原文。

        恢复之后审计里能看到完整链条：举报 → 下架 → 申诉 → 恢复。
        """
        if app.state.public_mode:
            return _public_readonly_error("申诉")
        body = payload or {}
        post = state.find_post(post_id)
        ok, err = gov.can_appeal(post)
        if not ok:
            code = 404 if err["code"] == "NOT_FOUND" else (
                409 if err["code"] == "CONFLICT" else 400)
            return _error(code, err["code"], err["message"])

        author = (body.get("author_real_id") or "").strip()
        if not author or author != post.get("author_real_id"):
            # **只有作者能申诉。** 不校验的话任何人都能把别人的帖子推进申诉队列。
            return _error(403, "FORBIDDEN", "只有作者本人可以申诉")

        reason = (body.get("reason") or "").strip()
        if not reason:
            return _error(400, "BAD_REQUEST", "申诉要说明理由")
        if len(reason) > config.APPEAL_REASON_MAX:
            return _error(400, "BAD_REQUEST",
                          f"申诉理由太长（上限 {config.APPEAL_REASON_MAX} 字）")

        now = datetime.now().isoformat(timespec="seconds")
        appeal = {
            "appeal_id": state.next_appeal_id(),
            "post_id": post["id"],
            "author_real_id": author,
            "reason": reason,
            "submitted_at": now,
            "status": "pending",
            "resolved_at": None,
            "resolved_by": None,
            "result_note": None,
            # **原文快照**：下架时正文可能已经不在前台，但申诉必须能对着原文判
            "post_snapshot": gov.snapshot_of(post),
            "status_when_appealed": post.get("review_status"),
        }
        # 改帖子的两个字段 + 落盘，包成一段（`transaction()` 退出时 save）——
        # 不要把"改状态"和"save"分开写，那会让并发请求的修改撞进 json.dump。
        with state.transaction():
            post["appeal"] = appeal
            post["appeal_status"] = "pending"
        # 审计里也带快照（审计是"说得出依据"的凭据，自己要有原文）
        state.append_audit(gov.build_audit(
            "appeal", post, actor_id=author, actor_role="author",
            reason_note=reason, from_status=post.get("review_status"),
            to_status=post.get("review_status"),
            extra={"appeal_id": appeal["appeal_id"]}, with_snapshot=True))
        return _json({"appeal_id": appeal["appeal_id"], "status": "pending",
                      "post_id": post["id"], "submitted_at": now,
                      "appeal_status": "pending",
                      "_note": "申诉已进队列；处置后会写审计（维持 / 恢复）"})

    @app.get("/api/admin/queue", summary="审核后台（假鉴权，演示用）")
    def admin_queue(
        status: str | None = Query(None, description="visible/pending/hidden/rejected/deleted"),
        x_admin_token: str = Header(default=""),
    ):
        if not _admin_ok(x_admin_token):
            return _error(401, "UNAUTHORIZED",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        raw = forum.queue(state, status=status)
        # 每条补上时限三件套（`reported_at` / `age_hours` / `due_at` / `overdue`）
        items = [gov.queue_item(p) for p in raw]
        # **排序按"最该先看的"**：超时的在前，然后等得最久的在前。
        # 原来的 created_at 倒序是"最新的在前"，对审核队列恰恰是反的 ——
        # 最老的举报最容易被一直压在最底下。
        items.sort(key=lambda x: (not x.get("overdue"), -(x.get("age_hours") or 0)))
        overdue = [x for x in items if x.get("overdue")]
        return _json({
            "total": len(items),
            "items": items,
            # **"我们积压了多少"必须是一个能看到的数**，不能是感觉
            "overdue_count": len(overdue),
            "due_hours": config.MODERATION_DUE_HOURS,
            "pending_count": sum(1 for x in items
                                 if x.get("review_status") == gov.PENDING_STATUS),
            "appeal_pending_count": sum(
                1 for x in items if (x.get("appeal_status") or "none") == "pending"),
            "_note": "演示用假鉴权，不是权限系统。overdue 只对 pending 判定 —— "
                     "hidden/deleted/rejected 都是已经处置过的，不算积压。",
        })

    @app.post("/api/admin/posts/{post_id}", summary="审核动作：hide / restore / delete")
    def admin_moderate(
        post_id: str,
        action: str = Query(..., description="hide / restore / delete"),
        note: str | None = Query(None),
        reason_code: str | None = Query(None, description="依据类别，见契约第 13 节"),
        x_admin_token: str = Header(default=""),
    ):
        if not _admin_ok(x_admin_token):
            return _error(401, "UNAUTHORIZED",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        post, err, from_status = forum.moderate(
            state, post_id, action, note=note,
            operator_id="admin", reason_code=gov.normalize_reason_code(reason_code)
            if reason_code else None)
        if err:
            return _error(404 if err["code"] == "NOT_FOUND" else 400,
                          err["code"], err["message"])
        # **审计：谁 / 何时 / 哪条 / 什么动作 / 依据 / 前值→后值，一个都不少。**
        # 前值必须记：只看新状态无法回答"这条是原本好好的被下架了，
        # 还是本来就在待审里被判了" —— 而这两件事的责任完全不同。
        state.append_audit(gov.build_audit(
            action, post, actor_id="admin", actor_role="moderator",
            reason_code=gov.normalize_reason_code(reason_code) if reason_code else None,
            reason_note=note, from_status=from_status,
            to_status=post.get("review_status"), with_snapshot=True))
        return _json({"id": post["id"], "review_status": post["review_status"],
                      "audited": True})

    @app.post("/api/admin/appeals/{appeal_id}",
              summary="处置申诉：uphold 维持 / restore 恢复")
    def admin_resolve_appeal(
        appeal_id: str,
        action: str = Query(..., description="uphold（维持）/ restore（恢复）"),
        note: str | None = Query(None),
        x_admin_token: str = Header(default=""),
    ):
        if not _admin_ok(x_admin_token):
            return _error(401, "UNAUTHORIZED",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        if action not in ("uphold", "restore"):
            return _error(400, "BAD_REQUEST", "action 只能是 uphold / restore")

        post = None
        for p in state.posts():
            ap = p.get("appeal") or {}
            if ap.get("appeal_id") == appeal_id:
                post = p
                break
        if post is None:
            return _error(404, "NOT_FOUND", f"没有这条申诉：{appeal_id}")
        ap = post["appeal"]
        if ap.get("status") != "pending":
            return _error(409, "CONFLICT",
                          f"这条申诉已经结案（{gov.appeal_result_label(ap.get('status'))}）")

        now = datetime.now().isoformat(timespec="seconds")
        from_status = post.get("review_status")
        # 同上：状态迁移 + 落盘一段完成（并发审核也不会把 from_status 记串）
        with state.transaction():
            if action == "restore":
                post["review_status"] = "rejected"   # 恢复展示（VISIBLE_STATUSES 里）
                ap["status"] = "restored"
                audit_action = "appeal_restored"
            else:
                ap["status"] = "upheld"
                audit_action = "appeal_upheld"
            ap["resolved_at"] = now
            ap["resolved_by"] = "admin"
            ap["result_note"] = note
            post["appeal_status"] = ap["status"]
            post["moderated_at"] = now

        state.append_audit(gov.build_audit(
            audit_action, post, actor_id="admin", actor_role="moderator",
            reason_note=note, from_status=from_status,
            to_status=post.get("review_status"),
            extra={"appeal_id": appeal_id, "appeal_result": ap["status"]},
            with_snapshot=True))
        return _json({
            "appeal_id": appeal_id, "post_id": post["id"],
            "result": ap["status"],
            "result_label": gov.appeal_result_label(ap["status"]),
            "review_status": post["review_status"],
            "resolved_at": now,
            # 恢复之后，整条链在审计里能一次看全。
            # **反转成旧→新**：这是一条"故事线"（举报 → 下架 → 申诉 → 恢复），
            # 读起来必须是从头到尾；而 `audit_log` 本身是新的在前（列表页要看最近的）。
            "audit_chain": [r.get("action") for r in
                            reversed(state.audit_log(post_id=post["id"]))],
        })

    @app.get("/api/admin/audit", summary="审核审计日志（append-only，只读）")
    def admin_audit(
        post_id: str | None = Query(None, description="只看某条帖子的"),
        operator_id: str | None = Query(None, description="只看某个操作人的"),
        action: str | None = Query(None, description="只看某类动作"),
        limit: int = Query(100, ge=1, le=1000),
        x_admin_token: str = Header(default=""),
    ):
        """**只读，而且接口层面没有写/改/删。**

        审计是"我说得出依据"的唯一凭据，所以它是 append-only 的 ——
        而且这个性质是**结构上的**：`StateStore` 上只有 `append_audit` 和
        `audit_log`，没有修改、没有删除，路由里也不提供。

        它是**独立的一份**，不混进帖子记录：帖子可能被删，审计不能跟着消失。
        每条还自带当时的**原文快照**，所以即使帖子没了，仍能回答
        "当时下架的是什么内容、依据是什么"。
        """
        if not _admin_ok(x_admin_token):
            return _error(401, "UNAUTHORIZED",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        rows = state.audit_log(post_id=post_id, actor_id=operator_id,
                               action=action, limit=limit)
        return _json({
            "total": len(rows),
            "items": rows,
            "actions": list(gov.AUDIT_ACTIONS),
            "reason_codes": [{"code": c, "label": l} for c, l in gov.REASON_CODES],
            "_note": "append-only：接口只提供读，没有修改和删除。"
                     "审计不随帖子删除而消失，每条自带当时的原文快照。",
        })

    # ---------- 订阅 / 付费墙（契约 api.md 第 5 节） ----------

    @app.get("/api/me", summary="登录态、订阅状态、今日剩余额度")
    def get_me(user: str = Query("u_demo", description="演示用假登录态")):
        u = state.user(user)
        return _json(billing.me_response(u, state.is_subscribed(u)))

    @app.post("/api/subscribe", summary="模拟支付开通订阅（不接真实通道）")
    def post_subscribe(payload: dict = Body(...)):
        plan = billing.find_plan((payload or {}).get("plan_id"))
        if plan is None:
            return _error(400, "BAD_PLAN",
                          f"plan_id 只能是 {' / '.join(p['id'] for p in billing.PLANS)}")
        user_id = (payload or {}).get("user_id") or "u_demo"
        if _public_blocked(app, user_id):
            return _public_readonly_error("开通订阅")
        order = state.subscribe(user_id, plan, plan["months"])
        u = state.user(user_id)
        return _json({
            "user": billing.user_for_api(u, state.is_subscribed(u)),
            "order": order,
            "plans": billing.plans_for_api(),
            # 和 /api/me 一样必须带这行：评委要能一眼看出是模拟支付
            "_note": billing.DEMO_NOTE,
        })



    # ---------- 增量：今日更新（契约 api.md 第 10 节） ----------
    #
    # 这是"月度订阅"唯一可演示的落点：用户买的是"哪一条是今天新出现的、
    # 哪一条被悄悄改了"。在此之前的做法是 C 的页面直接读 data/out/changes.json，
    # 跨过了接口层 —— A 一改那个文件的字段，前端会在没人察觉的时候坏掉。
    #
    # **不缓存到启动时**：变更报告是"每次抓取后重写"的文件，
    # 服务开着的时候 A 完全可能再跑一轮。启动时读一次会让页面永远停在旧报告。
    # 所以按 mtime + size 判断是否需要重读（文件 626 KB，每次请求都读也不贵，
    # 但没必要）。

    @app.get("/api/changes", summary="今日更新：新增 / 有变化 / 本轮未抓到 / id 变更")
    def get_changes():
        if app.state.changes is not None:
            return _json(app.state.changes.response())
        store_obj = _load_changes_cached()
        return _json(store_obj.response())

    @app.get("/api/changes/history", summary="更新历史：过去 N 天每天抓了多少、增删改多少")
    def get_changes_history(
        detail: bool = Query(False, description="是否附带每天完整变更报告（默认只给索引）"),
    ):
        """**这是"月度订阅"唯一能拿出来的凭据。**

        被问"你怎么证明每天在跑"时，答案不该是"我们的脚本能跑"（承诺），
        而是"这是过去 N 天的记录"（事实）。数据源是 A 的每日归档。
        """
        return _json(_load_history_cached().response(detail=detail))

    # ---------- 关注与提醒（契约 api.md 第 11 节） ----------
    #
    # 这一节回答的是"**和我有关的**变了什么"，而上面 /api/changes 回答的是
    # "这一轮变了什么"（所有人的、同一份）。中间那段就是月度订阅的付费理由：
    # 没有关注，日报每天长得一样（反正是那几条），用户看两天就不再打开；
    # 有了关注，每天的结果都是他没见过的。
    #
    # 鉴权沿用现有的假登录态（`?user_id=`，和 /api/notices/{id}、/api/me、/api/posts 一致），
    # **不新造一套 token**。落盘也沿用 server/var/state.json。

    @app.post("/api/watch", summary="加一条关注规则（幂等）")
    def post_watch(payload: dict = Body(...)):
        """重复关注同一条规则**不报错**，返回 `created: false` 和已有的那条。

        幂等是**从内容派生的 id** 带来的，不是额外加的判断 —— 见 server/watch.py。
        """
        body = payload or {}
        user_id = body.get("user_id") or "u_demo"
        if _public_blocked(app, user_id):
            return _public_readonly_error("关注")
        try:
            rule = watch_mod.normalize_rule(body.get("school"), body.get("unit"),
                                            body.get("keywords"))
        except watch_mod.WatchError as e:
            return _error(400, "BAD_REQUEST", str(e))

        existing = state.find_watch(user_id, watch_mod.watch_id_of(rule))
        if existing is not None:
            created, watch = False, existing
        else:
            res = state.add_watch(watch_mod.build_watch(user_id, rule))
            created, watch = res["created"], res["watch"]
        watches = state.watches(user_id)
        return _json({
            "user_id": user_id,
            "created": created,
            "watch": watch_mod.public_view(watch),
            "total": len(watches),
            "watches": [watch_mod.public_view(w) for w in watches],
            "_note": "演示用假登录态；关注规则存在 server/var/state.json",
        }, status_code=201 if created else 200)

    @app.get("/api/watch", summary="列出关注规则")
    def get_watch(user: str = Query("u_demo", description="演示用假登录态")):
        watches = state.watches(user)
        payload = {
            "user_id": user,
            "total": len(watches),
            "watches": [watch_mod.public_view(w) for w in watches],
            # 给前端画"你关注了哪些学校/院系"的标签用的去重清单
            "has_watches": bool(watches),
            "_note": "演示用假登录态；关注规则存在 server/var/state.json",
        }
        payload.update(watch_mod.facets(watches))
        return _json(payload)

    @app.delete("/api/watch/{watch_id}", summary="取关")
    def delete_watch(watch_id: str,
                     user: str = Query("u_demo", description="演示用假登录态")):
        """**取关不存在的 id 返回 404，不是 200。**

        "反正结果都是它不在了"听起来合理，但会把前端传错 id 这类 bug 静默吃掉：
        用户以为取关成功、规则还在、下次 digest 又冒出提醒，而日志里全是 200。
        演示时更怕这个 —— 点了"取消关注"看起来成功却还在收提醒，是当场翻车的形态。
        """
        if _public_blocked(app, user):
            return _public_readonly_error("取关")
        if not state.remove_watch(user, watch_id):
            return _error(404, "NOT_FOUND", "没有这条关注规则（可能已经取关过了）")
        watches = state.watches(user)
        return _json({
            "user_id": user,
            "deleted": watch_id,
            "total": len(watches),
            "watches": [watch_mod.public_view(w) for w in watches],
        })

    @app.get("/api/digest", summary="提醒摘要：跟我有关的、变了的东西")
    def get_digest(
        user: str = Query("u_demo", description="演示用假登录态"),
        since: str | None = Query(None, description="起始日期 YYYY-MM-DD，默认回看 7 天"),
        until: str | None = Query(None, description="结束日期 YYYY-MM-DD，默认到最新记录"),
    ):
        """**读差异报告，不读公告库** —— 提醒是关于"变化"的。

        只发 `added` / `changed`（"外面的世界变了"）；`reparsed` / `reidentified` /
        `removed` 一条都不出现（"我们的处理变了"）。把 `reparsed` 发进提醒等于
        告诉用户"学校更新了"而公告一个字没动 —— **提醒只要开始说谎就没人再看它**。

        边界（空关注 / 关注了无变化 / `since` 早于记录 / 完全没有记录）都在
        `DigestStore.build` 里决定，并且**都用不同的 `empty_reason` 区分开**。
        """
        store_obj = digest if digest is not None else _load_digest_cached()
        watches = state.watches(user)
        return _json(store_obj.build(user, watches, since=since, until=until))

    # ---------- 数据新鲜度与调度（契约 api.md 第 12 节） ----------
    #
    # 产品最核心的承诺是"每天更新"。在这一节之前，**这句话不可验证**：
    # 没有任何接口能回答"数据是什么时候的"；没有调度（只有手动跑脚本）；
    # 抓取失败完全不可见（某校今天没抓到，界面上和"没变化"长得一样）。

    @app.get("/api/quality", summary="字段可信度：我们哪块数据弱（可查的数）")
    def get_quality(
        school: str | None = Query(None, description="只看某所学校"),
        field: str | None = Query(None, description="只看某个字段"),
    ):
        """按 **学校 × 字段** 给三态比例；整体给两个头条比率。

        这一环和 `/api/freshness` 是一对：那边是**数据侧**可观测性
        （每所学校什么时候抓的），这边是**字段侧**可观测性（抓到的值可不可信）。

        **做这个的目的是让"我们数据很全"这句话可以被查。**
        以后谁想说这句话，先来这里看一眼 —— 而不是靠印象。
        所以数字**全部按真文件现算，一个都不硬编码**（门禁里有一条盯这个）。
        """
        # **只统计当前这份数据**（不做分页截断）：比例必须按全集算，
        # 取一页算出来的比率会随分页变化，那就不是"我们数据怎么样"而是"这一页怎么样"。
        recs = live_store.records
        if school:
            recs = [r for r in recs if r.get("school") == school]

        fields = list(credibility.TRACKED_FIELDS)
        if field:
            if field in credibility.TRACKED_FIELDS:
                fields = [field]
            elif field == "body":
                fields = []                     # body 单独在下面给
            else:
                return _error(400, "BAD_REQUEST",
                              f"field 只能是 {' / '.join(credibility.TRACKED_FIELDS)} / body")

        # ---- 按学校 × 字段 ----
        by_school = {}
        for r in recs:
            by_school.setdefault(r.get("school") or "(未知)", []).append(r)
        schools_out = []
        for name in sorted(by_school):
            rows = by_school[name]
            entry = {"school": name, "total": len(rows), "fields": {}}
            for f in fields:
                entry["fields"][f] = credibility.tally(rows, f)
            if not field:
                entry["fields"]["body"] = credibility.body_tally(rows)
            # "这所学校哪个字段最弱" —— 一眼能能出来的那个数。
            # **只在三态字段里选**：`body` 是另一个轴（抽取方式），
            # 混进来会让"哪个字段弱"变成"哪个字段老数据多"，那是两件事。
            entry["worst_field"] = max(
                ((f, st["unknown_rate"] or 0.0) for f, st in entry["fields"].items()
                 if f in credibility.TRACKED_FIELDS and "unknown_rate" in st),
                key=lambda x: x[1], default=(None, None))[0]
            schools_out.append(entry)

        # ---- 整体 ----
        overall = {"total": len(recs), "schools": len(schools_out), "fields": {}}
        for f in fields:
            overall["fields"][f] = credibility.tally(recs, f)
        if not field:
            overall["fields"]["body"] = credibility.body_tally(recs)

        # 两个头条比率
        dl = credibility.tally(recs, "deadline")
        body = credibility.body_tally(recs)
        deadline_unknown_rate = dl["unknown_rate"]
        body_generic_rate = body["generic_rate"]

        # 一个能直接读的句子。**数字一定是现算的**，不许出现写死的百分数。
        # `body_generic_rate` 为 None 时（一条都没记抽取方式）说"无"，
        # **不说 0.0%** —— 0% 和"没数据"是两回事，这是本项目一贯的规矩。
        head = (f"{len(recs)} 条记录：截止日 unknown {_pct(deadline_unknown_rate)}、"
                f"正文 generic {_pct(body_generic_rate)}")
        if body["known"] == 0:
            head += "（正文抽取方式一条都没记录，generic 比例无从计算）"
        return _json({
            "total": len(recs),
            "deadline_unknown_rate": deadline_unknown_rate,
            "body_generic_rate": body_generic_rate,
            "overall": overall,
            "schools": schools_out,
            "states": list(credibility.FIELD_STATES),
            "body_states": list(credibility.BODY_STATES),
            "summary": head,
            "_note": "unknown = 我们判不了（必须让用户点原文），"
                     "和 absent = 正文读到了、确实没有，是**两件事**。"
                     "rate 的分母见 `_rate_base`；三态字段按全部算，"
                     "body 按「记了抽取方式的那些」算（一个都没记时是 null，不是 0）。"
                     "这里的比率按当前数据全集现算，不硬编码。",
            "_why": "让'我们数据很全'这句话变成能查的数，而不是印象。",
        })

    @app.get("/api/freshness", summary="数据新鲜度：每所学校什么时候抓的")
    def get_freshness():
        """**按 mtime 重载**，不是启动时读死 —— 见 `_load_freshness_cached`。

        两个自己算、不信文件的字段：`age_hours` 按 `last_ok` 现算
        （文件里的 `freshness_hours` 是生成时刻的快照，文件不动它永远是 0）。
        """
        store_obj = freshness if freshness is not None else _load_freshness_cached()
        return _json(store_obj.response())

    @app.post("/api/admin/crawl", summary="触发一次抓取（单飞）")
    def post_crawl(x_admin_token: str | None = Header(None),
                   payload: dict | None = Body(None)):
        """**单飞**：已经在跑就返回同一个 `run_id`，`joined_existing: true`。

        抓取会**打对方学校的服务器**，所以并发跑两轮不只是浪费，是对别人不礼貌，
        而且 `crawl.py` 是"读旧文件 → 覆盖写"，两轮并发会互相踩。
        调度器 + 手动同时触发是正常情况，所以 join 返回 202 而不是 409。

        真抓一轮是分钟级，所以是**提交 + 轮询** `GET /api/admin/crawl/{run_id}`。
        """
        if not _admin_ok(x_admin_token):
            return _error(403, "FORBIDDEN",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        runner = crawl if crawl is not None else _default_crawler()
        trigger = (payload or {}).get("trigger") or "manual"
        view, joined = runner.trigger(trigger=trigger)
        view = dict(view or {})
        view["joined_existing"] = joined
        return _json(view, status_code=202)

    @app.get("/api/admin/crawl", summary="抓取运行日志（最近几次）")
    def list_crawl(x_admin_token: str | None = Header(None),
                   limit: int = Query(CRAWL_RUNS_LIST_DEFAULT, ge=1,
                                      le=crawl_mod.MAX_RUNS)):
        # **这一条一开始漏了鉴权**（声明了 x_admin_token 却没用它）——
        # 是门禁里"三个 crawl 端点都要 403"那条用例抓出来的。
        # 它和 /api/admin/queue 一样属于运维面，不能公开：
        # 里面的 tail 会带出子进程输出，可能含内部路径。
        if not _admin_ok(x_admin_token):
            return _error(403, "FORBIDDEN",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        runner = crawl if crawl is not None else _default_crawler()
        runs = runner.runs(limit=limit)
        return _json({
            "total": len(runs),
            "runs": runs,
            "current": runner.current,
            "schedule_enabled": runner.schedule_enabled,
            "interval_hours": runner.interval_hours,
            "first_delay_min": runner.first_delay_min,
            "_note": "trigger=manual/schedule 是有人点或定时器到点；"
                     "skipped_fresh 表示定时器到点但数据还新、没抓。",
        })

    @app.get("/api/admin/crawl/{run_id}", summary="某次抓取的状态")
    def get_crawl(run_id: str, x_admin_token: str | None = Header(None)):
        if not _admin_ok(x_admin_token):
            return _error(403, "FORBIDDEN",
                          "需要 X-Admin-Token 请求头（演示用假鉴权，见 /api/health）")
        runner = crawl if crawl is not None else _default_crawler()
        view = runner.get(run_id)
        if view is None:
            return _error(404, "NOT_FOUND", f"没有这次抓取记录：{run_id}")
        return _json(view)

    # ---------- 画像 / 订阅 / 推送（契约 api.md 第 16 节） ----------
    #
    # 今天这条链是：**用户填画像 → 数据层产出事件 → 后端匹配并生成推送 → 前端展示预览**。
    # 这三个接口就是中间那一环，一头吃 A 的 `events.jsonl`，一头给 C 渲染用的形状。

    def _plan_block(u):
        """推送/订阅共用的"你现在是什么身份"。**别让两处各算一遍**（会漂）。"""
        active = state.is_subscribed(u)
        if active:
            plan_id = u.get("plan_id")
            name = next((p["name"] for p in billing.PLANS if p["id"] == plan_id), "订阅")
            return {
                "active": True, "plan_id": plan_id, "plan_name": name,
                "expires_at": u.get("expires_at"), "reason": "active",
                "text": f"{name}：推送 + 匹配排序 + 套磁信",
            }
        if u.get("expires_at"):
            return {
                "active": False, "plan_id": u.get("plan_id"),
                "plan_name": None, "expires_at": u.get("expires_at"),
                "reason": "expired",
                "text": "订阅已过期：推送和截止提醒已停，浏览不受影响",
            }
        return {
            "active": False, "plan_id": None, "plan_name": None,
            "expires_at": None, "reason": "free",
            "text": "免费版：可以浏览公告、看三态、点原文链接",
        }

    @app.get("/api/profile", summary="读用户画像（没填过返回 200 + null，不是 404）")
    def get_profile(user: str = Query("u_demo", description="演示用假登录态")):
        prof, updated = state.profile_of(user)
        return _json({
            "user_id": user,
            "profile": prof,
            "complete": profile_mod.completeness(prof),
            "updated_at": updated,
            "_note": "画像只存在本机 server/var/state.json，不发送到任何第三方。演示用假登录态。",
        })

    @app.put("/api/profile", summary="写画像（整份替换，幂等）")
    def put_profile(payload: dict = Body(...), user: str = Query("u_demo")):
        """**整份替换，不合并。** 合并会让"我把专业删了"变成一次无效操作。

        宽容处理：某个字段格式不对 → **丢掉那个字段并说出来**，不整份 400。
        用户填了 5 项、1 项格式错，正确的做法是收下 4 项。
        """
        body = payload or {}
        # `user_id` 在 body 里也收 —— 和 `/api/watch` 一样，两条路都通，
        # 免得调用方因为参数位置不对而静默写到 u_demo 头上。
        uid = (body.get("user_id") or "").strip() or user
        if _public_blocked(app, uid):
            return _public_readonly_error("修改画像")
        raw = body.get("profile") if isinstance(body.get("profile"), dict) else body
        raw = {k: v for k, v in (raw or {}).items() if k not in ("user", "user_id")}
        try:
            norm = profile_mod.normalize(raw)
        except ValueError as e:
            return _error(400, "BAD_PROFILE", str(e))
        clean = norm["profile"]
        if not clean:
            return _error(400, "EMPTY_PROFILE",
                          "画像里没有任何可识别的字段（认的是 school / majors / "
                          "preferred_schools / interest_keywords / preferred_units）")
        updated = state.set_profile(uid, clean)
        out = {
            "user_id": uid,
            "profile": clean,
            "complete": profile_mod.completeness(clean),
            "updated_at": updated,
            # **认不出的字段要报出来**（契约 §16.2，B19）。
            # 恒给一个数组（可能是空）：前端判断 `unknown_fields` 为真即可，
            # 不用先判断键在不在 —— "字段在不在"是接口层最容易被漏掉的分支。
            "unknown_fields": norm["unknown_fields"],
            "_note": "画像只存在本机 server/var/state.json，不发送到任何第三方。",
        }
        if norm["mapped"]:
            # 近似字段被自动改名了 → 说清"按哪个字段处理了"
            out["mapped_fields"] = norm["mapped"]
        if norm["field_note"]:
            # 一句人话，前端可以直接显示（映射 + 未知字段合成一句）
            out["field_note"] = norm["field_note"]
        if norm["dropped"]:
            # **丢掉了什么必须说出来** —— 否则用户以为自己填上了，而推送永远匹配不到。
            out["dropped"] = norm["dropped"]
        return _json(out)

    @app.delete("/api/profile", summary="删画像（删完不再产生推送）")
    def delete_profile(user: str = Query("u_demo")):
        if _public_blocked(app, user):
            return _public_readonly_error("删除画像")
        had = state.delete_profile(user)
        return _json({
            "deleted": had,
            "user_id": user,
            "pushes_stopped": True,
            "_note": ("画像已删除，之后不再产生推送。"
                      "推送去重记录**不清**：重新填画像不会把你已经收过的旧事件再推一遍。"),
        })

    @app.get("/api/subscriptions", summary="订阅状态 + 渠道可用性")
    def get_subscriptions(user: str = Query("u_demo")):
        u = state.user(user)
        return _json({
            "user_id": user,
            "plan": _plan_block(u),
            "channels": [
                {"channel": "preview", "available": True,
                 "enabled": state.is_subscribed(u),
                 "text": "站内预览：这条推送长什么样，页面直接渲染（不需要任何外部服务）"},
                {"channel": "email", "available": False, "enabled": False,
                 "text": "邮件通道还没接：没有发信服务，也不会问你要邮箱"},
                {"channel": "webhook", "available": False, "enabled": False,
                 "text": "Webhook 还没接：留了适配位置，演示不碰"},
            ],
            "activation": {
                "method": "manual_code",
                "code_required": True,
                "_note": "**没接支付通道**。开通用的是手动开通码（演示用），不是付款。",
            },
            "_note": "D4 演示用，未接真实支付；开通码由课程助教/评委人工发放",
        })

    @app.post("/api/subscriptions", summary="开通订阅（手动开通码，不是支付）")
    def post_subscriptions(payload: dict = Body(default={}),
                           user: str | None = Query(None, description="演示用假登录态")):
        """**`user` 在 query 里，和全项目其他端点一致**（issue 008 的第 2 条）。

        上一版只从 body 读 `user_id`，于是 `?user=u_x` + body 只给码 →
        开通到了 `u_demo`，而预览查 `u_x` 显示未订阅 —— 看起来像"开通了但没生效"。
        这种"两个端点的取值位置不一致"本身就是坑，不靠文档提醒来兜。

        仍然兼容 body 里的 `user_id`（老调用方不该因为这次修正而坏），
        但**优先级**是 query > body > `u_demo`，和别的端点行为一致。
        """
        body = payload or {}
        uid = (user or body.get("user_id") or body.get("user") or "u_demo").strip()
        if _public_blocked(app, uid):
            return _public_readonly_error("开通订阅")
        code = (body.get("code") or "").strip()
        if not code:
            return _error(400, "CODE_REQUIRED",
                          "需要开通码（演示用）。没有支付通道，所以开通用人工发码。")
        if code != config.PUSH_ACTIVATION_CODE:
            # **不区分"码不对"和"码过期"** —— 区分了就等于告诉别人码对不对。
            return _error(400, "BAD_CODE", "开通码不对或已过期")
        plan = billing.find_plan(body.get("plan_id") or "monthly")
        if plan is None:
            return _error(400, "BAD_PLAN", "plan_id 只能是 monthly / season")
        state.subscribe(uid, plan, plan["months"])
        u = state.user(uid)
        return _json({
            "activated": True,
            "user_id": uid,
            "plan": _plan_block(u),
            "method": "manual_code",
            "_note": "演示用手动开通码；本次没有发生任何支付",
        })

    @app.delete("/api/subscriptions", summary="取消订阅（回到免费身份，订单历史保留）")
    def delete_subscription(user: str = Query("u_demo")):
        """**取消整个订阅**，不只是关渠道。

        在这之前只有 `DELETE /api/subscriptions/{channel}`（关站内预览渠道），
        **没有任何接口能退回免费身份** —— 演示前想把状态复位只能手改
        `server/var/state.json`，而手改会带出更脏的状态（C 被这条卡过）。

        **`orders` 一条都不删**：取消订阅改变的是"以后还享不享受服务"，
        不是"你从来没买过"。抹掉订单历史是另一种不诚实。
        """
        if _public_blocked(app, user):
            return _public_readonly_error("取消订阅")
        was = state.cancel_subscription(user)
        u = state.user(user)
        return _json({
            "cancelled": was,
            "user_id": user,
            "plan": _plan_block(u),
            "orders_kept": len(state.state.get("orders") or []),
            "_note": "已回到免费身份（每天 3 条详情）。**订单历史保留** —— "
                     "取消订阅不是抹掉购买记录。演示用模拟支付，未接真实通道。",
        })

    @app.delete("/api/subscriptions/{channel}", summary="关掉某个推送渠道（只支持 preview）")
    def delete_subscription_channel(channel: str, user: str = Query("u_demo")):
        if _public_blocked(app, user):
            return _public_readonly_error("修改订阅渠道")
        if channel != "preview":
            # 其余渠道本来就不可用 —— 关一个不存在的东西返回 404，不静默成功。
            return _error(404, "CHANNEL_NOT_AVAILABLE",
                          f"渠道 {channel} 还没接（email / webhook 的 available 恒为 false）")
        return _json({
            "channel": "preview",
            "enabled": False,
            "user_id": user,
            "_note": "站内预览已关。**这不等于取消订阅**，也不影响其他功能。",
        })

    @app.get("/api/push/preview", summary="推送预览：你会收到的那条（只读，幂等）")
    def get_push_preview(
        user: str = Query("u_demo"),
        limit: int = Query(config.MAX_PUSH_LIMIT, description="本次最多看几条"),
        source: str | None = Query(None, description="只接受 sample：读演示样例事件并标注"),
    ):
        # **注意：`_push_preview` 直接返回响应对象**（已经过 `_json`），
        # 这里不能再包一层 —— 包了就变成"把 JSONResponse 当 dict 序列化"。
        return _push_preview(user, limit, source, commit=False)

    @app.post("/api/push/preview", summary="接收这批推送（记录去重与限频）")
    def post_push_preview(
        payload: dict = Body(default={}),
        user: str = Query("u_demo"),
        limit: int = Query(config.MAX_PUSH_LIMIT),
        source: str | None = Query(None),
    ):
        """**"我接收这批"。**

        和 GET 的区别只有一个：这里会**记录**（去重 + 限频）。

        为什么不让 GET 顺便记：预览是"看一眼"。看一眼就消耗掉一条推送的话，
        用户刷新一次页面就会看到"你已经收到过了" —— 那是把一个展示行为
        当成了消费行为。
        """
        body = payload or {}
        uid = (body.get("user_id") or "").strip() or user
        # 公开演示版：访客可以看推送，但**不能把演示身份的"已推送"记录消费掉**
        if _public_blocked(app, uid):
            return _public_readonly_error("接收推送")
        return _push_preview(uid, limit, source, commit=True)

    @app.get("/api/push/suppressed", summary="被压掉的推送：计数 + 分页查全部")
    def get_push_suppressed(
        user: str = Query("u_demo"),
        reason: str | None = Query(None, description="只这一种原因：backfill / junk / merged / duplicate / rate_limited"),
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        source: str | None = Query(None, description="只接受 sample"),
    ):
        """**"被压掉的 2739 条到底都是什么"**（契约 §16.8）。

        为什么要有这个接口：`/api/push/preview` 里 `suppressed` 只给**每种原因前 3 条**
        样例（全量是 523 KB，占整个响应的 98%）。样例够了就不给全量 ——
        但"我就是要翻一遍"这件事也得能做成，所以给它一个分页出口。

        注意 `tally` 里的数（`suppressed` / 各原因计数）在 preview 里**一直是真的**，
        不会因为它只给样例就变小。
        """
        d = _push_payload(user, None, source, commit=False, suppressed_mode="full")
        if hasattr(d, "headers"):          # 是错误响应（bad source）
            return d
        sup = d["suppressed"]
        if reason:
            sup = [x for x in sup if x.get("reason") == reason]
        total = len(sup)
        page = sup[offset:offset + limit]
        return _json({
            "user_id": d["user_id"],
            "reason": reason,
            "offset": offset,
            "limit": limit,
            "total": total,
            "items": page,
            # 每种原因的**全局**计数（不受 reason 过滤影响），和 preview 的完全一致
            "suppressed_tally": d["suppressed_tally"],
            "suppressed_total": d["suppressed_total"],
            "_note": ("这里是被 `/api/push/preview` 压掉的事件全量（分页）。"
                      "`preview` 只给每种原因前 3 条样例，是为了把响应从 523 KB 压到 3 KB。"),
        })

    def _push_payload(user, limit, source, commit, suppressed_mode="capped"):
        """组装 + 返回**字典**（或错误响应）。`_push_preview` 负责包 `_json`。

        拆出来是为了给 `/api/push/suppressed` 复用同一套组装逻辑 ——
        那个接口要 `suppressed_mode="full"` 才能翻全部。
        """
        src = (source or "").strip().lower()
        if src and src != "sample":
            # **不猜别的值**：传了 `source=xxx` 就明确告诉他不认识。
            # 静默回落到真实源会让人以为自己在看样例。
            return _error(400, "BAD_SOURCE", "source 只接受 sample（演示样例事件）")
        if src == "sample":
            ev = EventStore.load_sample(config.EVENTS_SAMPLE_PATH)
        elif events is not None:
            ev = events
        else:
            ev = _load_events_cached()

        # `limit` 越界**截断不报错**（契约 §16.4）。
        # 注意不能用 `limit or MAX` —— `0` 是 falsy，会被静默当成"没传"，
        # 而"要 0 条"和"没传"是两件事（后者才该给默认）。
        limit = config.MAX_PUSH_LIMIT if limit is None else int(limit)
        limit = max(1, min(limit, config.MAX_PUSH_LIMIT))

        u = state.user(user)
        prof, _updated = state.profile_of(user)
        plan = _plan_block(u)
        # ---- 把公告侧的质量/主题挂到事件上（契约 §19）----
        #
        # `events.jsonl` 里没有 `record_quality` / `topic`（它们是公告的属性，
        # 不是"发生了什么变化"的属性），所以在这里按 `notice_id` 关联一次。
        #
        # 为什么在 app 层做而不是在 `push.py` 里：`push.py` 只认识事件流，
        # 它拿不到公告库。让 `push.py` 去要一个 store 进来，
        # 那它就再也不是"纯函数式的 事件×画像"了。
        enriched = []
        for e in ev.events():
            rec = live_store.get(e.get("notice_id"))
            e2 = dict(e)
            if rec is not None:
                e2["record_quality"] = rec.get("record_quality")
                e2["topic"] = rec.get("topic")
                # pply_mode（推免/统考/夏令营…）：推送和匹配必须**同一套硬过滤**，
                # 否则同一个画像会出现匹配说不行、推送照推（B18）。
                e2["apply_mode"] = rec.get("apply_mode")
                # `notice_kind`（A29）：**首屏按它分层**（B22，契约 §18.9）。
                # 它在公告侧、不在事件流里 —— 和 topic 同一个道理。
                # 老事件可能取不到（rec 为 None），那就保持 None：
                # `push.tier_of` 会退回旧规则，条数记进 `tally.kind_unknown`。
                e2["notice_kind"] = rec.get("notice_kind")
                # 招生年份：推送用它挡掉往届通知（「2025 级选拔」对 2027 考生无意义）
                e2["target_year"] = rec.get("target_year")
                # **事件可能没有 `publish_date`**（实测清华那两条 `added` 是空串），
                # 而公告库里有。空着会连锁出两个错：新鲜度算成"读不到"（0.5），
                # 60 天窗口判成"不在窗口内"（B22）—— 一条 9 月发的推免通知
                # 会被当成过期藏掉。**从公告补，不是编：同一个字段、同一条记录。**
                if not (e2.get("publish_date") or "").strip():
                    e2["publish_date"] = rec.get("publish_date")
            enriched.append(e2)
        out = push_mod.assemble(
            events=enriched,
            profile=prof,
            completeness=profile_mod.completeness(prof),
            user_id=user,
            plan=plan,
            plan_active=plan["active"],
            today=date.today(),
            limit=limit,
            source_meta=ev.meta(),
            state=state,
            commit=commit,
            suppressed_mode=suppressed_mode,
        )
        # ---- 第三层「参考资料」+ 池子有多大（B22 / B23，契约 §18.9）----
        #
        # 两个都**从公告库现算**，不从事件流来：
        # - 目录/简章的价值是"随时可查"，不是"今天变了"；从事件流取会漏掉
        #   今天没变化的绝大部分目录；
        # - 「本周值得看」的分母是**整个库**（2751），不是这个用户的推送条数。
        if hasattr(out, "headers"):          # 错误响应（比如 BAD_SOURCE）
            return out
        today = date.today()
        out["reference"] = push_mod.reference_of(live_store.records, prof, today)
        out["tally"]["apply_fresh_announcement"] = (
            push_mod.apply_fresh_announcement_count(live_store.records, today))
        # 「其中 N 条对口你」——和首屏一级同一套硬门槛（B23）。
        out["tally"]["apply_fresh_announcement_matched"] = (
            push_mod.apply_fresh_announcement_matched_count(
                live_store.records, prof, today))
        return out

    def _push_preview(user, limit, source, commit, suppressed_mode="capped"):
        d = _push_payload(user, limit, source, commit, suppressed_mode)
        return d if hasattr(d, "headers") else _json(d)

    # ---------- 今日更新 / 资料库（契约 api.md 第 20 节） ----------

    def _events_source(source):
        """选事件源。和 `_push_preview` 同一套规矩：**不认识的 source 明确报错，不静默回落**。"""
        src = (source or "").strip().lower()
        if src and src != "sample":
            return None, _error(400, "BAD_SOURCE", "source 只接受 sample（演示样例事件）")
        if src == "sample":
            return EventStore.load_sample(config.EVENTS_SAMPLE_PATH).events(), None
        if events is not None:
            return events.events(), None
        return _load_events_cached().events(), None

    @app.get("/api/updates", summary="今日/本周更新：只给新鲜且未过期的事件")
    def get_updates(
        days: int = Query(updates_mod.DEFAULT_WINDOW_DAYS, ge=1,
                          le=updates_mod.MAX_WINDOW_DAYS,
                          description="窗口天数，默认 7，上限 30（= 新的判据）"),
        limit: int = Query(50, ge=1, le=config.MAX_PUSH_LIMIT,
                           description="最多给几条（判据要求 ≤50）"),
        school: str | None = Query(None, description="学校，子串匹配"),
        source: str | None = Query(None, description="只用 sample（演示样例事件）"),
    ):
        """**"今天/本周该看什么"。**

        和 `/api/changes`（§10）不是一回事：那个回答"我们这轮发现了哪些**变化**"，
        这个回答"**现在**有哪些值得看的新公告"。前者是差分，后者是分区。

        与 `/api/library` 的关系（契约 §20.1）：
        **并集 = 事件全集，交集 = 0**，因为 library 是 updates 的补集。
        """
        evs, err = _events_source(source)
        if err is not None:
            return err
        today = date.today()
        ups, _lib = updates_mod.split(evs, today, days)
        items = [updates_mod.item_of(e, today) for e in ups]
        if school:
            kw = school.strip()
            items = [i for i in items if kw in (i.get("school") or "")]
        # 发布日降序；没有发布日的沉底（**不是排最前**：不知道新旧就不该当最新）
        items.sort(key=lambda i: (i.get("publish_date") or ""), reverse=True)
        total = len(items)
        items = items[:limit]
        return _json({
            "window_days": days,
            "total": total,
            "items": items,
            "empty_reason": None if items else "no_fresh_events",
            "empty_text": None if items else (
                "这个窗口里没有新公告。注意这不等于「今天没有更新」—— "
                "可能是抓取管道还没跑，可以看 /api/freshness 的数据时间。"),
            "_note": ("只含发布日在新近窗口内、未过期、且非 junk 的记录；"
                      "更早的历史公告在 /api/library，两者不重叠。"),
        })

    @app.get("/api/library", summary="资料库：历史公告（明确标注历史参考）")
    def get_library(
        school: str | None = Query(None, description="学校，子串匹配"),
        year: int | None = Query(
            None, description="年份：匹配 发布年 / 招生年(admission_year) / 目标年(target_year) 任一"),
        topic: str | None = Query(None, description="apply / non_apply / other"),
        historical: bool = Query(
            False, description="只给真正的历史参考（30 天以上或已截止）"),
        days: int = Query(updates_mod.DEFAULT_WINDOW_DAYS, ge=1,
                          le=updates_mod.MAX_WINDOW_DAYS,
                          description="**必须和 /api/updates 传同一个值**，否则并集不是全集"),
        page: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=config.MAX_PAGE_SIZE),
        source: str | None = Query(None),
    ):
        """历史公告。**每一条都带 `is_historical` 和一句 `history_reason`。**

        `is_historical` 按"30 天以上 **或** 已截止"判，**不是**"它出现在这个接口里"——
        一条 20 天前的公告也会落在这里（不在本周窗口内），但它不叫历史。
        要看真正的历史，用 `?historical=true`（或者 `?year=` 把时间推远）。
        """
        evs, err = _events_source(source)
        if err is not None:
            return err
        today = date.today()
        _ups, lib = updates_mod.split(evs, today, days)
        # 招生年 / 目标年不在事件里（事件只有 publish_date），要回公告库取一次。
        # 一次建表，不逐条查询 —— 1100 条逐条 `get()` 会让这个接口慢到不能用。
        meta = {}
        for e in lib:
            rec = live_store.get(e.get("notice_id"))
            if rec is not None:
                meta[e.get("event_id")] = rec
        items = []
        for e in lib:
            it = updates_mod.item_of(e, today)
            rec = meta.get(e.get("event_id")) or {}
            it["admission_year"] = rec.get("admission_year")
            it["target_year"] = rec.get("target_year")
            items.append(it)
        if school:
            kw = school.strip()
            items = [i for i in items if kw in (i.get("school") or "")]
        if year is not None:
            y = str(year)
            items = [i for i in items
                     if (i.get("publish_date") or "")[:4] == y
                     or str(i.get("admission_year")) == y
                     or str(i.get("target_year")) == y]
        if topic:
            items = [i for i in items if i.get("topic") == topic]
        if historical:
            items = [i for i in items if i.get("is_historical")]
        items.sort(key=lambda i: (i.get("publish_date") or ""), reverse=True)
        total = len(items)
        start = (page - 1) * size
        return _json({
            "total": total,
            "page": page,
            "size": size,
            "items": items[start:start + size],
            "_note": ("资料库是历史参考，**不是今日更新**；"
                      "它与 /api/updates 不重叠，并集是事件全集。"),
        })

    # ---------- 套磁信（契约 api.md 第 3 节，模板版） ----------

    @app.post("/api/letter", summary="套磁信草稿（模板版，引文逐条可回链）")
    def post_letter(payload: dict = Body(...)):
        """**模板版，不是 LLM 生成。** 响应里 `method="template"` 就是标这个。

        为什么模板版是对的：演示时最有价值的不是信写得好不好，
        是 `citations` 逐条能点回原文 —— 那是防"模型编造导师信息"的手段。
        接上 LLM 后 `body` 换成模型产出，`citations` 仍须逐条可回链。
        """
        body = payload or {}
        notice_id = body.get("notice_id")
        if not notice_id:
            return _error(400, "BAD_REQUEST", "缺少 notice_id")
        rec = live_store.get(notice_id)
        if rec is None:
            return _error(404, "NOT_FOUND", "公告不存在")
        profile = _as_profile(body.get("profile") or {})
        letter = letter_mod.build_letter(rec, profile, tone=body.get("tone") or "正式")
        return _json(letter)

    # ---------- 静态前端（B-KICKOFF 第 2 步：同源挂载，C 就不用碰 CORS） ----------

    @app.get("/", include_in_schema=False)
    def index():
        index_file = os.path.join(config.WEB_DIR, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return _json({
            "service": "推免雷达 API",
            "docs": "/docs",
            "notices": "/api/notices?page=1&size=5",
            "note": "web/ 还没就绪，挂上后这里会自动变成 C 的首页",
        })

    if os.path.isdir(config.WEB_DIR):
        app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")

    return app


def _empty_store() -> NoticeStore:
    """数据源缺失时的兜底：起得来、但明确报告 0 条，而不是崩在启动阶段。"""
    return NoticeStore(config.NOTICES_PATH, [], {
        "path": os.path.abspath(config.NOTICES_PATH), "lines": 0, "loaded": 0,
        "skipped": 0, "bad_lines": [], "unique_ids": 0, "duplicate_ids": [],
    })


def _default_store() -> NoticeStore:
    return NoticeStore.load(config.NOTICES_PATH)


# 注意：`create_app()` 不传 state 时会自己 `StateStore.load(config.STATE_PATH)`，
# 也就是用 server/var/state.json 里持久化的用户/帖子/订阅。
# 测试要隔离就是显式传一个 `StateStore.load(":memory:")`（`run.py` 就是这么做的）。
#
# **传的是工厂 `_default_store`，不是 `_default_store()`** ——
# 传函数进去，`create_app` 才会把它包成 `LiveStore`（文件改了自动重载）；
# 传已经加载好的实例，就退化成"启动时读一次"，那正是 C 报的那个 bug。
try:
    app = create_app(_default_store)
except FileNotFoundError as exc:      # 数据没就位也要能起来，返回空列表而不是崩
    import warnings

    warnings.warn(f"{exc}；以空数据集启动，/api/notices 会返回 total=0", RuntimeWarning, stacklevel=2)
    app = create_app(_empty_store())

# 进程内定时抓取（契约第 12 节）。
#
# **只在这里起，不放在 `create_app` 里** —— 放进去的话，门禁里每个
# `create_app(...)` 都会拉一个后台线程起来，测试会变得不确定（而且会真的去抓）。
# 所以门禁注入的 app 永远没有调度器；调度器只属于真正被 uvicorn 跑起来的那一个。
#
# **启动时不立刻抓**：第一次检查延后 `TUIMIAN_CRAWL_FIRST_DELAY_MIN` 分钟（默认 5）。
# 演示时服务器一起来就开始抓 37 所学校，界面卡、网络吵，很难看。
_default_crawler().start_scheduler()


if __name__ == "__main__":
    import uvicorn

    # **单 worker**：状态是单文件 JSON + 进程内锁，多 worker 并发写会丢更新。
    # 演示够用；要上多 worker 就得把 StateStore 换成 SQLite。见 server/state.py。
    uvicorn.run("server.app:app", host="127.0.0.1", port=8000, reload=True)
