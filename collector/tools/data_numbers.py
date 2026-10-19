# -*- coding: utf-8 -*-
"""生成 `docs/DATA-NUMBERS.md` 里那些数字（只读，不改 `data/`）。

    cd collector && python tools/data_numbers.py

简报要求：「PDF 会引用公告数、站点数、field_state 分布。这些数字必须来自
data/out 的实测，不许凭印象。」—— 这个脚本就是为了让那句话可复现：
**引用前跑一遍，别照抄文档里的旧数**（数据每跑一次日更都会变）。

输出是给人读的分节文本，直接对着抄进 `docs/DATA-NUMBERS.md` 的表。
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common


def load(name):
    p = os.path.join(common.OUT_DIR, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


rs = load("notices.jsonl") or []
fa = load("faculty.jsonl") or []
ev = load("events.jsonl") or []
st = load("stability.jsonl") or []
fr = json.load(open(os.path.join(common.OUT_DIR, "freshness.json"), encoding="utf-8")) \
    if os.path.exists(os.path.join(common.OUT_DIR, "freshness.json")) else {}

print(f"## 公告")
print(f"- 条数：{len(rs)}")
print(f"- 学校：{len(set(r['school'] for r in rs))}")
print(f"- 单位（学校×单位）：{len(set((r['school'], r.get('unit')) for r in rs))}")
print(f"- 正文非空：{sum(1 for r in rs if (r.get('body_text') or '').strip())}/{len(rs)}")
print(f"- 附件总数：{sum(len(r.get('attachments') or []) for r in rs)}")
print(f"- 有截止日：{sum(1 for r in rs if r.get('deadline'))}")
print(f"- 最长正文：{max((len(r.get('body_text') or '') for r in rs), default=0)} 字")
print(f"- 发布日范围：{min((r.get('publish_date') or '9999') for r in rs)} ~ "
      f"{max((r.get('publish_date') or '') for r in rs)}")
print(f"- relevance 分布：{dict(sorted(Counter(r.get('relevance') for r in rs).items()))}")

print(f"\n## 字段可信度三态（field_state）")
for k in ("deadline", "majors", "attachments"):
    c = Counter((r.get("field_state") or {}).get(k) for r in rs)
    print(f"- {k}: found {c.get('found',0)} / absent {c.get('absent',0)} / unknown {c.get('unknown',0)}")
c = Counter((r.get("field_state") or {}).get("body") for r in rs)
print(f"- body: ok {c.get('ok',0)} / generic {c.get('generic',0)} / missing {c.get('missing',0)}")

print(f"\n## 导师")
units = Counter((f.get("school"), f.get("unit")) for f in fa)
print(f"- 人数：{len(fa)}")
print(f"- 院系：{len(units)}")
for (s, u), n in units.most_common():
    cov = sum(1 for f in fa if (f.get("school"), f.get("unit")) == (s, u)
              and f.get("research_areas"))
    print(f"  - {s} / {u}：{n} 人，方向 {cov/n:.0%}")
print(f"- 有研究方向：{sum(1 for f in fa if f.get('research_areas'))}")
print(f"- 有邮箱：{sum(1 for f in fa if f.get('email'))}")

print(f"\n## 站点覆盖")
if fr:
    t = fr.get("totals") or {}
    print(f"- 配置站点：{t.get('schools')}")
    print(f"- 产出 0 条的站点：{sum(1 for s in fr.get('schools', []) if not s.get('records'))}")
    print(f"- 有失败的站点：{t.get('failed_schools')}")

print(f"\n## 事件流")
real = [e for e in ev if not e.get("is_backfill")]
print(f"- 事件总数：{len(ev)}")
print(f"- 补抓（is_backfill）：{len(ev) - len(real)}")
print(f"- 真新增：{len(real)}")
print(f"- 真新增里 relevance=2：{sum(1 for e in real if (e.get('relevance') or 0) == 2)}")

print(f"\n## 日更")
if st:
    print(f"- 运行轮次：{len(st)}")
    print(f"- 成功轮次：{sum(1 for x in st if x.get('ok'))}")
    print(f"- 覆盖天数：{len(set(x.get('day') for x in st))}")
    print(f"- 最长一轮耗时：{max(x.get('seconds', 0) for x in st):.0f}s")
else:
    print("- 还没有 stability.jsonl")
