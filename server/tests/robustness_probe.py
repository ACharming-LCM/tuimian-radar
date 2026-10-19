# -*- coding: utf-8 -*-
"""鲁棒性探针：**给它喂坏的，看它响不响**（契约 `api.md` 第 15 节）。

这不是门禁（门禁是 `server/tests/run.py`，会红会绿）。
这是**给人看的一张表**：五个环各喂一个坏输入，把"它怎么响的"打出来。
演示前想确认"坏了我们看得见吗"，跑这一条就够。

跑法（项目根目录）：

    $env:PYTHONIOENCODING="utf-8"
    D:\\python.3.10.11\\python.exe server\\tests\\robustness_probe.py

**它一个字节都不写进 `data/` 和 `server/var/`**：真数据的坏版本是复制到临时目录里
再造的，状态文件全部用临时路径，公告 store 用临时 JSONL。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                    # noqa: E402

from server import config                                     # noqa: E402
from server import letter as letter_mod                        # noqa: E402
from server.app import create_app                              # noqa: E402
from server.state import StateStore                            # noqa: E402
from server.store import FacultyStore, NoticeStore, load_health  # noqa: E402

TMP = tempfile.mkdtemp(prefix="tuimian_probe_")
FACULTY = FacultyStore.load(config.FACULTY_PATH)


def line(title=""):
    print()
    print("=" * 78)
    if title:
        print(title)
        print("=" * 78)


def app_for(store, state=None):
    """`state` 可以传**已经加载好的** `StateStore`（这样才能看到它的 recovery），
    也可以传路径/None。**必须传实例**，否则重新 load 一次会把"坏文件已改名"
    这件事抹掉 —— 我第一次写这个探针就踩了：health 报 ok=true，而文件明明坏过。"""
    if state is None or isinstance(state, str):
        state = StateStore.load(state or ":memory:")
    return TestClient(create_app(store, FACULTY, state))


def jsonl_of(records, name="notices.jsonl"):
    d = tempfile.mkdtemp(prefix="rb_", dir=TMP)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


# --------------------------------------------------------------------------
line("环 1  数据源里有一行坏了 —— 服务少了公告，它会响吗？")

real = os.path.join(ROOT, "data", "out", "notices.jsonl")
h0 = load_health(NoticeStore.load(real).load_meta)
print(f"真数据              state={h0['state']!r}  loaded={h0['loaded']}  "
      f"skipped={h0['skipped_lines']}")
print(f"                    message={h0['message']}")

broken_dir = tempfile.mkdtemp(prefix="bad_", dir=TMP)
broken = os.path.join(broken_dir, "notices.jsonl")
shutil.copyfile(real, broken)
lines = open(broken, encoding="utf-8").read().splitlines()
lines[5] = "{ 这一行坏了"                       # 第 6 行
open(broken, "w", encoding="utf-8").write("\n".join(lines) + "\n")

st = NoticeStore.load(broken)
h1 = load_health(st.load_meta)
print(f"第 6 行改坏之后     state={h1['state']!r}  loaded={h1['loaded']}  "
      f"skipped={h1['skipped_lines']}")
print(f"                    bad_lines={json.dumps(h1['bad_lines'], ensure_ascii=False)}")
print(f"                    message={h1['message']}")
c = app_for(st)
body = c.get("/api/health").json()
print(f"GET /api/health     data.load_state={body['data']['load_state']!r}  "
      f"data.skipped_lines={body['data']['skipped_lines']}")
print(f"GET /api/notices    total={c.get('/api/notices', params={'size': 50}).json()['total']}"
      f"   ← 能读到的照常给（不拒绝启动）")

# --------------------------------------------------------------------------
line("环 2  默认排序：高 relevance 的早公告 vs 低 relevance 的新公告")

recs = [
    {"id": "p_hi_old", "school": "测试大学", "title": "2027年接收推荐免试研究生工作办法",
     "publish_date": "2026-01-05", "relevance": 2, "url": "https://x.invalid/1",
     "deadline": None, "extract": {"confidence": 0.9}},
    {"id": "p_lo_new", "school": "测试大学", "title": "关于公布本学期课程安排的通知",
     "publish_date": "2026-10-16", "relevance": 0, "url": "https://x.invalid/2",
     "deadline": None, "extract": {"confidence": 0.9}},
]
p2 = jsonl_of(recs, "rel.jsonl")
c2 = app_for(NoticeStore.load(p2))
items = c2.get("/api/notices", params={"size": 50}).json()["items"]
print("排出来的顺序        " + " → ".join(
    f"{i['id']}({i['publish_date']})" for i in items))
print("                    ← 高的在前，说明 relevance 真的进了排序键")
print(f"载荷里有 relevance 吗："
      f"{any('relevance' in i for i in items)}   ← 必须是 False（只排序、不进载荷）")

# --------------------------------------------------------------------------
line("环 3  套磁信引文：表格行 / 残缺标题 / 两个字 —— 会不会混进信里")

cases = [
    ("纯表格正文", {"id": "p_t", "title": "2027年接收推免生工作办法（测试）",
                    "url": "https://x.invalid/t", "deadline": "2026-11-20",
                    "body_text": "序号 | 志愿ID | 姓名 | 专业\n国家专项 | 张一一 | 物理学\n",
                    "materials": [], "extract": {"confidence": 0.9}}),
    ("残缺标题", {"id": "p_e", "title": "关于2027年接收推免生的通知…",
                  "url": "https://x.invalid/e", "deadline": None,
                  "body_text": "申请人须在截止前提交成绩单与推荐信…",
                  "materials": [], "extract": {"confidence": 0.9}}),
    ("两个字正文", {"id": "p_s", "title": "2027年接收推免生工作办法（测试2）",
                    "url": "https://x.invalid/s", "deadline": None,
                    "body_text": "报名材料", "materials": [],
                    "extract": {"confidence": 0.9}}),
]
for name, rec in cases:
    lt = letter_mod.build_letter(rec, {})
    quotes = [c["quote"] for c in lt["citations"]]
    print(f"{name:<8} 引文 {len(quotes)} 条：{quotes}")
    print(f"{'':<8} 丢掉：{lt['quotes_dropped']}")
    print(f"{'':<8} 警告：{[w for w in lt['warnings'] if '判不准' in w or '没有可引用' in w]}")

# 真数据全量扫：一条坏引文都不许有
bad = 0
for r in NoticeStore.load(real).records:
    for c in letter_mod.build_letter(r, {})["citations"]:
        q = c["quote"].strip()
        if (c["source"] == "body_text" and letter_mod._looks_like_table(q)) \
                or q.endswith(letter_mod.ELLIPSIS_TAILS):
            bad += 1
print(f"真数据 196 条扫一遍  坏引文 {bad} 条  ← 必须是 0")

# --------------------------------------------------------------------------
line("环 4  状态文件坏了 —— 帖子/审计还在吗？它会说吗？")

for name, text in (("半截 JSON", '{"version": 1, "posts": [{"id": "p_lost"}]'),
                   ("顶层是 []", "[]"),
                   ("users 是字符串", '{"version": 1, "users": "坏了"}')):
    d = tempfile.mkdtemp(prefix="state_", dir=TMP)
    sp = os.path.join(d, "state.json")
    open(sp, "w", encoding="utf-8").write(text)
    stt = StateStore.load(sp)                     # 不抛
    rec = stt.recovery
    body = app_for(NoticeStore.load(real), stt).get("/api/health").json()
    print(f"{name:<14} reason={rec['reason']!r}  backup={bool(rec.get('backup'))}  "
          f"health.state.ok={body['state']['ok']}")
    print(f"{'':<14} {rec['message']}")

# --------------------------------------------------------------------------
line("环 5  空数据源 ——「没数据」和「筛得太窄」分得开吗？")

d = tempfile.mkdtemp(prefix="empty_", dir=TMP)
ep = os.path.join(d, "notices.jsonl")
open(ep, "w", encoding="utf-8").close()
est = NoticeStore.load(ep)
c5 = app_for(est)
b = c5.get("/api/notices", params={"meta": 1, "size": 50}).json()
print(f"空数据源   total={b['total']}  data_state={json.dumps(b['data_state'], ensure_ascii=False)}")
print(f"           health.data.load_state="
      f"{c5.get('/api/health').json()['data']['load_state']!r}")

c6 = app_for(NoticeStore.load(real))
b2 = c6.get("/api/notices", params={"meta": 1, "school": "这个学校不存在"}).json()
print(f"筛太窄     total={b2['total']}  empty_reason={b2['data_state']['empty_reason']!r}"
      f"  loaded={b2['data_state']['loaded']}")
print(f"           ← 两个 0 条，两个不同的 empty_reason")

shutil.rmtree(TMP, ignore_errors=True)
line("跑完了。门禁（会红会绿的那份）是 server/tests/run.py 第 17 节，见契约 api.md §15.7")
