# -*- coding: utf-8 -*-
"""演示冒烟测试：把彩排脚本变成一条命令。

用法（项目根目录）：

    D:\\python.3.10.11\\python.exe server/tests/demo_rehearsal.py

它做三件事：
1. 在**本机随机空闲端口**起一个服务实例（不碰你在跑的 8000）；
2. 按演示 6 步**依次打真实 HTTP 接口**，每步断言"有东西可看"；
3. 打一张表，**指出是哪一步断了**，而不是只给一个红绿。

## 为什么要有它

`docs/DEMO-CHECKLIST.md` 有 183 行手工打勾，做得很扎实 ——
但**紧张的时候会被跳过**。脚本的价值在于"能看出是哪一步断了"，
以及演示前 5 分钟能跑一条命令。

## 断言只绑结构和不变量，**绝不绑数字**

这一晚已经因为"数据规模"这类假设栽过三次（额度状态、条数、页码）。
所以这里断言的是：
- `score` 单调不降（`/api/match` 的排序契约）
- 每条引文的 `url` 非空且能指回公告（防编造的唯一手段）
- 引文的 `quote` **逐字能在那条公告的正文里找到**
- 举报够阈值后**前台真的少了**（不是只看状态码）
- 字段集合 = fixture 的键集合（形状契约）

而**不是** `total == 72` 这种。数据变了脚本不该红。

## 隔离

每次跑都用一个**临时状态文件**（`TUIMIAN_STATE`）和临时端口，
所以它不会往 `server/var/state.json` 里塞测试帖子 ——
演示前跑它不会把演示环境搞脏。
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

LINE = "─" * 78


# ---------------------------------------------------------------------------
# 起一个隔离的服务实例
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerHandle:
    def __init__(self):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._tmpdir = tempfile.mkdtemp(prefix="tuimian_rehearsal_")
        self._thread = None
        self._server = None

    def start(self, timeout=25):
        # **隔离**：状态写到临时目录，不碰 server/var/state.json
        os.environ["TUIMIAN_STATE"] = os.path.join(self._tmpdir, "state.json")
        # 让 config 重新读（config 在 import 时读过一次）
        from server import config
        config.STATE_PATH = os.environ["TUIMIAN_STATE"]

        import uvicorn

        cfg = uvicorn.Config("server.app:app", host="127.0.0.1", port=self.port,
                             log_level="warning", access_log=False,
                             # 线程里不能装信号处理器
                             lifespan="on")
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        t0 = time.time()
        while time.time() - t0 < timeout:
            if getattr(self._server, "started", False):
                # 再确认一次真能应答
                try:
                    self.get("/api/health")
                    return self
                except Exception:
                    pass
            time.sleep(0.15)
        raise RuntimeError(f"服务在 {timeout}s 内没起来（端口 {self.port}）")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=8)
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ---- HTTP ----

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=15)
            raw = r.read().decode("utf-8")
            return r.status, dict(r.headers), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            return e.code, dict(e.headers), (json.loads(raw) if raw else None)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)


# ---------------------------------------------------------------------------
# 步骤框架
# ---------------------------------------------------------------------------

STEPS = []


def step(no, title):
    def deco(fn):
        STEPS.append((no, title, fn))
        return fn
    return deco


class Fail(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Fail(msg)


def _load_json(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 六步（2026-10-18 加第 ⑥ 步：今日更新 / 资料库 / 数据心跳）
# ---------------------------------------------------------------------------

@step("①", "今日更新")
def step_changes(srv):
    st, _, d = srv.get("/api/changes")
    need(st == 200, f"HTTP {st}")
    need(set(d["counts"]) >= {"previous", "current", "added", "changed",
                              "reparsed", "removed", "reidentified", "unchanged"},
         f"counts 缺键：{sorted(d['counts'])}")
    for k in ("added", "changed", "reparsed", "removed", "reidentified"):
        need(k in d and isinstance(d[k], list), f"{k} 不是数组")
    # reparsed 每条必须带 why（界面要如实说明归因原因）
    for x in d["reparsed"]:
        need(x.get("why"), f"reparsed 条目缺 why：{x.get('id')}")
        need("body_text" not in (x.get("record") or {}), "reparsed 泄漏了正文")
    # **reparsed 绝不能并进 changed** —— 契约反复强调的那条
    ch = {x["id"] for x in d["changed"]}
    rp = {x["id"] for x in d["reparsed"]}
    need(not (ch & rp), "reparsed 和 changed 混了")
    note = (f"counts.reparsed={d['counts']['reparsed']} "
            f"changed={d['counts']['changed']} 全部带 why")
    return note


@step("②", "公告列表")
def step_notices(srv):
    st, _, d = srv.get("/api/notices?page=1&size=5")
    need(st == 200, f"HTTP {st}")
    need(d["items"], "列表是空的 —— 演示第 2 步没东西可看")
    # 形状 = fixture 的键集合（形状契约，与条数无关）
    fx = _load_json("contracts/fixtures/notices.list.json")
    need(set(d["items"][0]) == set(fx["items"][0]),
         f"字段集合不对：{sorted(set(d['items'][0]) ^ set(fx['items'][0]))}")
    # 默认排序的**不变量**：relevance 单调不增（档内再按日期降序）
    import importlib.util
    cdir = os.path.join(ROOT, "collector")
    if cdir not in sys.path:
        sys.path.insert(0, cdir)
    spec = importlib.util.spec_from_file_location("_rf", os.path.join(cdir, "fields.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    full = srv.get("/api/notices?page=1&size=50")[2]["items"]
    recs = {}
    with open(os.path.join(ROOT, "data", "out", "notices.jsonl"), encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                r = json.loads(ln)
                recs[r["id"]] = r
    rels = []
    for it in full:
        r = recs.get(it["id"])
        rels.append(mod.relevance_of(r["title"], r.get("board"), r.get("apply_mode"))
                    if r else 0)
    if rels != sorted(rels, reverse=True):
        # **台上的报错要说人话。** 这条断了有两种原因，而"
        # relevance 不是降序"读起来像是排序代码坏了 —— 2026-10-18 实际是
        # **数据里存的 relevance 没重算**（A6 捞回标题之后）。见 issues/010。
        stale = [r for r in recs.values()
                 if recs.get(r["id"]) is not None
                 and mod.relevance_of(r["title"], r.get("board"),
                                      r.get("apply_mode")) != r.get("relevance")]
        hint = (f"（{len(stale)} 条记录的 relevance 与现算不一致 —— "
                "数据层没重算，见 issues/010；排序代码没问题）" if stale else
                "（数据里的 relevance 与现算一致 —— 这次真是排序键坏了）")
        need(False, f"relevance 不是降序：{rels[:10]} {hint}")
    # 同档内日期降序
    for lo in set(rels):
        dates = [it["publish_date"] for it, r in zip(full, rels) if r == lo]
        need(dates == sorted(dates, reverse=True), f"档 {lo} 内日期不是降序")
    return (f"前 5 条 relevance={rels[:5]}（首档 {max(rels)} 在打头）"
            f" 字段 {len(fx['items'][0])} 个")


@step("③", "背景匹配")
def step_match(srv):
    prof = json.load(open(os.path.join(ROOT, "collector", "tools", "profile-example.json"),
                          encoding="utf-8-sig"))
    st, _, d = srv.post("/api/match?limit=30", prof)
    need(st == 200, f"HTTP {st}")
    need(set(d) == {"profile", "stats", "items"}, f"顶层键不对：{sorted(d)}")
    need(d["items"], "匹配结果为空 —— 演示第 3 步没东西可看")
    scores = [x["score"] for x in d["items"]]
    need(scores == sorted(scores, reverse=True), "score 不是降序")
    need(d["profile"] == prof, "profile 没有原样回显")
    # 每条必须有理由（产品的差异化）和三个"同一响应里给全"的字段
    for x in d["items"]:
        need(x.get("reasons"), f"{x['id']} 没有 reasons")
        need(isinstance(x.get("has_attachments"), bool), f"{x['id']} 缺 has_attachments")
        need(isinstance(x.get("extract", {}).get("confidence"), (int, float)),
             f"{x['id']} 缺 extract.confidence")
    return (f"{len(d['items'])} 条，score 降序 {scores[0]:.4f}→{scores[-1]:.4f}"
            f"，全部带 reasons")


@step("④", "生成套磁信")
def step_letter(srv):
    """挑一条公告生成信，验引文都能回链。

    ## 两个坑，都是这一步自己踩出来的

    **坑 1：「第一页就够」**（和门禁里那几条同一个病）。原来只在第一页 50 条里找
    正文 > 800 字的，新数据下第一页（按 relevance 排）恰好全都不够长，这一步直接断。
    → 改成**翻页扫候选**，而且**不硬性要求"必须有长正文"**：
    一条只有标题 + 材料 + 截止日的公告也能生成合格的信（那三条都是真实信息）。
    这一步真正要验的是**引文可回链**。

    **坑 2：静默降级 —— 被免费额度挡住了却报"正文很短"。**
    `/api/notices/{id}` 是**唯一**卡免费额度的地方（3 条/天），
    第 4 次起返回 402 且**响应里没有 `body_text`**。
    于是脚本扫 120 条时，除了前 3 条全是 402，最后报告成"正文 478 字" ——
    **看起来像数据质量差，实际是被额度挡住了。**
    这类"把拒绝说成别的东西"正是本项目反复在防的。
    → 这一步**先用一个订阅用户**（`POST /api/subscribe`），
    并且**把 402 变成明确失败**而不是悄悄当成空正文。

    （顺带这也是给演示的提醒：主流程用订阅用户走完，
    否则点开第 4 条公告就会被墙挡住。RUN.md 里写了。）
    """
    # 先订阅一个专用用户：详情接口的免费额度是 3 条/天，不订阅就没法扫候选
    demo_user = "u_rehearsal"
    srv.post("/api/subscribe", {"user_id": demo_user, "plan_id": "monthly"})

    items, page = [], 1
    while page <= 20:
        body = srv.get(f"/api/notices?page={page}&size=50")[2]
        got = body.get("items") or []
        items.extend(got)
        if not got or len(items) >= body.get("total", 0):
            break
        page += 1
    need(items, "公告列表是空的")

    best, best_len, blocked = None, -1, 0
    for it in items[:120]:                      # 有上限，避免几百次详情请求
        st, _, full = srv.get(f"/api/notices/{it['id']}?user={demo_user}")
        if st == 402:
            # **不许静默降级**：被额度挡住必须是明确失败，不能当成"这条正文为空"
            blocked += 1
            need(False, f"详情接口返回 402（免费额度）—— 订阅用户不该被挡：{it['id']}")
        need(st == 200, f"取详情失败 HTTP {st}：{it['id']}")
        need("body_text" in full,
             f"详情响应里没有 body_text（可能是被额度挡住但状态码不是 402）：{it['id']}")
        n = len(full.get("body_text") or "")
        if n > best_len:
            best, best_len = full, n
        if n > 800:
            break
    target = best
    need(target is not None, "一条公告都取不到详情")

    st, _, out = srv.post("/api/letter", {"notice_id": target["id"]})
    need(st == 200, f"HTTP {st}")
    need(out.get("method") == "template", "没标明是模板生成")
    need(out.get("_note"), "缺 _note（要说清不是模型生成的）")
    need(out["citations"], "citations 是空的 —— 那是防编造的唯一手段")

    # **每条引文都要能回链**
    body_text = target.get("body_text") or ""
    for c in out["citations"]:
        need(c.get("url"), f"引文缺 url：{c.get('label')}")
        need(c.get("quote"), f"引文缺 quote：{c.get('label')}")
        need(c.get("source") in ("title", "body_text", "materials", "deadline"),
             f"未知 source：{c.get('source')}")
        if c["source"] == "body_text":
            need(c["quote"] in body_text, f"引文不在原文里，无法回链：{c['quote'][:50]}")
        # 结构化字段拼的引文**不许**标成"公告原文"（诚信问题）
        if c["source"] in ("materials", "deadline"):
            need(c["label"] != "公告原文", f"{c['source']} 被标成了公告原文")
    # cited_from 必须和 citations 一致
    need(set(out["cited_from"]) == {c["source"] for c in out["citations"]},
         "cited_from 和 citations 对不上")
    # 留白处一处不少，而且**每个都说得清缺什么**（B24，2026-10-19）
    for need_txt in ("____（请补充你的学校）", "____（请补充GPA 排名）",
                     "____（请补充姓名）"):
        need(need_txt in out["body"], f"正文占位符没有提示：{need_txt}")

    return (f"引文 {len(out['citations'])} 条（{'+'.join(out['cited_from'])}）"
            f"，url 全部非空、逐字可回链；候选扫了 {min(len(items), 120)} 条，"
            f"正文 {best_len} 字，无一条被额度挡住")


@step("⑤", "论坛发帖 → 举报 → 审核")
def step_forum(srv):
    fx = _load_json("contracts/fixtures/posts.list.json")
    st, _, post = srv.post("/api/posts", {
        "title": "演示冒烟：物理学院推免时间线",
        "body": "这是一条冒烟测试帖，用来确认论坛还能用。",
        "unit": "物理学院", "tag": "推免经验", "author_real_id": "u_rehearsal"})
    need(st in (200, 201), f"发帖 HTTP {st}")
    # 公开字段 = fixture 的键集合（形状），且三个敏感字段一个都不能有
    need(set(post) == set(fx["items"][0]),
         f"公开字段不对：{sorted(set(post) ^ set(fx['items'][0]))}")
    for bad in ("author_real_id", "created_ip", "review_status"):
        need(bad not in post, f"发帖响应泄漏了 {bad}")
    pid = post["id"]

    # 举报到阈值 -> 前台必须**真的少了**（不是只看状态码）
    before = srv.get("/api/posts")[2]["total"]
    note = "没到阈值，未进队列"
    from server import forum as F
    for i in range(F.REPORT_THRESHOLD):
        srv.post(f"/api/posts/{pid}/report",
                 {"reason": f"冒烟举报{i}", "reporter_id": f"u_r{i}"})
    after = srv.get("/api/posts")[2]["total"]
    need(after == before - 1, f"举报到阈值后前台条数没减：{before} -> {after}")
    note = f"举报 {F.REPORT_THRESHOLD} 次后前台 {before}→{after}"

    # 后台能看，且**没有 token 看不到**
    need(srv.get("/api/admin/queue")[0] == 401, "审核后台没鉴权就放行了")
    from server import config as C
    q = srv.get("/api/admin/queue", headers={"X-Admin-Token": C.ADMIN_TOKEN})[2]
    need(q["total"] >= 1, "进了审核队列却查不到")
    item = next((x for x in q["items"] if x["id"] == pid), None)
    need(item is not None, "这条帖子不在审核队列里")
    need(item.get("author_real_id") == "u_rehearsal", "后台看不到实名留痕")
    need(item.get("reports"), "后台看不到举报理由 —— 那审核的人没法判")
    return f"发帖→{note}，后台可见实名+举报理由，无 token 401"


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------

@step("⑥", "今日更新 / 资料库 / 数据心跳")
def step_feeds(srv):
    """**为什么加这一步（我的判断，不用等 C）。**

    前五步走的是"单条内容"（列表 → 匹配 → 套磁信 → 论坛）。但演示里
    C 已经有三处界面读的是**分区后的数据**：今日更新、资料库、页头的心跳。
    这三个一旦坏了，前五步全绿也照样翻车 —— 而它们**不在**原来的冒烟里。

    两条不变量：

    1. **分区恒等式**：`updates.total + library.total == 事件总数`，交集 0。
       这条要是断了，某条公告会"今天新增"和"资料库"两边都不出现（或都出现）。
    2. **心跳不是绿的骗人**：`ok` 允许是 `null`（不知道），但**不许在
       `consecutive_ok` 为 0 且 `ok=false` 的时候页面还显示"数据正常"**。
       这里只钉接口层：`last_run.ok` 是 `false` 就算断。
    """
    upd = srv.get("/api/updates?days=7&limit=50")[2]
    lib = srv.get("/api/library?size=20")[2]
    hb = srv.get("/api/health/data")[2]

    need(upd["total"] <= 50, f"今日更新给了 {upd['total']} 条（上限 50）")
    for it in upd["items"]:
        need(it["age_days"] is not None and it["age_days"] <= 7,
             f"7 天窗口里出现了 {it['age_days']} 天前的：{it['event_id']}")
        need(it["is_historical"] is False, f"今日更新里混了历史参考：{it['event_id']}")

    # 分区恒等式：并集 = 全集，交集 = 0
    total_events = hb["events"]
    need(upd["total"] + lib["total"] == total_events,
         f"分区不等于全集：{upd['total']} + {lib['total']} != {total_events}")
    u_ids = {i["event_id"] for i in upd["items"]}
    l_ids = {i["event_id"] for i in lib["items"]}
    need(not (u_ids & l_ids), f"两个接口给了同一条：{list(u_ids & l_ids)[:3]}")

    need(lib["items"], "资料库一条都没有 —— 那 1000 多条历史公告去哪了")
    need(all("is_historical" in i for i in lib["items"]), "资料库条目缺 is_historical")

    # 心跳：不许把"失败"说成正常；`null`（不知道）允许，但要写在 _note 里
    lr = hb["last_run"]
    if lr is not None:
        need(lr["ok"] is not False, f"最近一轮跑失败了，页面却会显示正常：{lr}")
        if lr["ok"] is None:
            need(hb.get("_note"), "ok 是 null 却没说明为什么")

    return (f"更新 {upd['total']} 条 / 资料库 {lib['total']} 条"
            f"（合计 {total_events} = 事件数，无重叠）；"
            f"心跳 {hb['consecutive_ok']} 轮连续成功")


def main():
    print()
    print(LINE)
    print("  推免雷达 · 演示冒烟（按演示 6 步依次打真实接口）")
    print(LINE)

    srv = ServerHandle()
    results, failed = [], None
    t0 = time.time()
    try:
        srv.start()
        print(f"  测试实例已起：{srv.base}")
        print(f"  状态隔离在临时目录（不碰 server/var/state.json）")
        print()
        for no, title, fn in STEPS:
            started = time.time()
            try:
                note = fn(srv)
                ok, err = True, ""
            except Fail as e:
                ok, err = False, str(e)
            except Exception as e:                      # noqa: BLE001
                ok, err = False, f"{type(e).__name__}: {e}"
            dt = time.time() - started
            results.append((no, title, ok, note if ok else err, dt))
            flag = "✓" if ok else "✗"
            print(f"  {no} {title:<10s} {flag}  {results[-1][3][:56]}"
                  f"{'' if ok else ''}  ({dt:.2f}s)")
            if not ok:
                failed = (no, title, err)
    finally:
        srv.stop()

    print()
    print(LINE)
    passed = sum(1 for r in results if r[2])
    print(f"  结果：{passed}/{len(STEPS)} 步通过"
          f"（总耗时 {time.time() - t0:.1f}s）")
    if failed:
        print()
        print(f"  ★ 断在第 {failed[0]} 步「{failed[1]}」：")
        print(f"    {failed[2]}")
        print()
        print("  演示前必须修好这一步 —— 否则现场就断在这里。")
    elif passed == len(STEPS):
        print()
        print("  六步都有东西可看。可以上台了。")
        print("  （提示：健康检查 /api/health 里 store_reloads > 0 说明")
        print("    接口已经跟上了最新数据，不用重启服务）")
    print(LINE)
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
