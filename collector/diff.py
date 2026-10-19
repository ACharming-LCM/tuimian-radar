# -*- coding: utf-8 -*-
"""增量变更检测：对比两次抓取，回答"今天有什么新东西"。

为什么这个模块是产品的核心，而不是附属工具：
"每天更新、消除信息差"这个卖点，用户真正愿意付钱的部分不是"有公告"，
而是 **"哪一条是今天新出现的"** 和 **"哪一条被悄悄改了"**。
高校公告被静默修改是常态（延期、改截止日、补附件），
`content_hash` 从 D1 就写在契约里，就是为了这一刻。

归属：A。产出 data/out/changes.md 与 data/out/changes.json。
"""
import hashlib
import json
import os
from datetime import datetime

import common

# 会进"变更"展示的字段。改这里等于改"什么算变了"，是产品决策不是技术决策。
WATCH_FIELDS = ("deadline", "publish_date", "title", "apply_mode", "target_year",
                "attachments")


def load_jsonl(path):
    """读成 {id: record}。文件不存在返回 {}（第一次跑没有基线，是正常情况）。"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id"):
                out[rec["id"]] = rec
    return out


def _att_urls(rec):
    """附件的 **URL 集合**。

    只有"学校加了文件 / 删了文件"才算真的变了 —— 那是学校那边的动作。
    """
    return tuple(sorted(a.get("url", "") for a in (rec.get("attachments") or [])))


def _att_sig(rec):
    """含**抽取结果**的附件签名（`text_len` 等）。

    坑（已踩）：原来只用这一个签名判断"附件变了"，而它含 `text_len`。
    我把 PDF 表格从"一个单元格一行"改成"按行拼接"之后，
    附件的提取字符数全变了，于是 **13 条被报成「学校改了通知」** ——
    但学校一个文件都没动，是我们换了抽取方式。
    """
    return [(a.get("url"), a.get("text_len") or 0) for a in (rec.get("attachments") or [])]


def _content_key(rec):
    """**内容身份**：即使 id 变了，同一条公告也应该能认出来。

    坑（已踩，而且毁了演示第 1 步）：我调整了 `make_id` 的兜底算法
    （改用归一化 url 算哈希），于是 30 条记录的 id 变了。
    `diff.py` 只按 id 比对，结果把这 30 条同时报成「新增 30」和「本轮未抓到 30」——
    而它们其实一个字都没变。C 在页面上看到 added[0] 和 removed[0] 是同一篇公告
    （title / url / content_hash 全同，只有 id 不同），如实标了「不是真的新发或丢失」。

    **id 是会给外部用的键，但它不是稳定的身份。** 抓取算法一调整 id 就会动，
    而"今天有没有新公告"这个结论不能因此乱掉。
    """
    return (rec.get("school"), rec.get("unit"), rec.get("title"),
            rec.get("publish_date"))


def _body_hash(rec):
    """优先用记录自带的 content_hash；没有就现算，保证老数据也能比较。"""
    h = rec.get("content_hash")
    if h:
        return h
    return "sha1:" + hashlib.sha1((rec.get("body_text") or "").encode("utf-8")).hexdigest()


def _ext_version(rec):
    """抽取器版本。老记录没有这个字段时返回 None —— 两边都 None 就算相同，
    这样加这个字段之前的归档不会突然全被当成"换了抽取器"。"""
    return (rec.get("extract") or {}).get("version")


def compare(old, new_records):
    """old: {id: rec}（上次），new_records: [rec]（本次）。

    返回 dict。**"消失"不等于"被删除"** —— 很可能是这条公告翻页了、或者我们这轮
    栏目没抓全。所以它在报告里必须单独一类，并且措辞要提醒"可能只是没抓到"，
    不能让用户以为公告撤了。
    """
    new = {r["id"]: r for r in new_records if r.get("id")}
    added, changed, reparsed, reidentified = [], [], [], []

    # 第一遍：按 id 比对（快路径）
    matched_ids = set()
    for rid, nr in new.items():
        orr = old.get(rid)
        if orr is None:
            continue
        matched_ids.add(rid)
        fields = {}
        for k in WATCH_FIELDS:
            ov, nv = orr.get(k), nr.get(k)
            if k == "attachments":
                ov, nv = _att_sig(orr), _att_sig(nr)
            if ov != nv:
                fields[k] = (ov, nv)
        oh, nh = _body_hash(orr), _body_hash(nr)
        # 判据：**我们抓到的"输入"变了吗？**
        #   输入 = 正文 + 附件清单（学校实际发布的东西）
        #   派生 = title / deadline / apply_mode / target_year（都是我们从输入算出来的）
        # 输入变了 → 学校那边真的动了；只有派生变 → 我们改了抽取代码。
        # 只有"学校加了/删了附件"才算学校动作；text_len 这类派生差异不算。
        att_urls_changed = _att_urls(orr) != _att_urls(nr)
        att_derived = _att_sig(orr) != _att_sig(nr)
        # 但"正文变了"这件事本身要归因：正文是我们**抽出来的**，
        # 抽取器版本一变，正文就必然不同 —— 那不是学校改了通知。
        # 实测：把 PDF 表格从"一个单元格一行"改成"按行拼接"之后，
        # 67 个 PDF 的正文全变了，如果不管归因，日报会报"学校改了 67 份通知"。
        # 所以只有**抽取器版本相同**时，正文哈希差异才算学校改的。
        same_extractor = (_ext_version(orr) == _ext_version(nr))
        body_really_changed = (oh != nh) and same_extractor
        if body_really_changed or att_urls_changed:
            changed.append({"record": nr, "fields": fields,
                            "hash": (oh, nh), "body_changed": oh != nh})
        elif fields or oh != nh or att_derived:
            # 走这里都不是学校改了通知：
            #   · 只有 title/deadline 这类派生字段不同（我们改了抽取规则）
            #   · 正文哈希不同但抽取器版本也变了（我们换了抽取方式，比如 PDF 表格拼接）
            #   · 附件只有 text_len 变了（同样是抽取结果）
            why = "抽取器版本变了" if (oh != nh and not same_extractor) \
                else ("附件抽取结果变了" if att_derived and not fields else "派生字段不同")
            reparsed.append({"record": nr, "fields": fields,
                             "hash": (oh, nh), "body_changed": False, "why": why})

    # 第二遍：id 对不上的，用**内容身份**再配一次。
    # 这一遍专门处理"抓取算法调整导致 id 变了，但公告本身没变"——
    # 不这么做的话，30 条老公告会同时被报成"新增"和"未抓到"，把日报彻底毁掉。
    old_free = {k: v for k, v in old.items() if k not in matched_ids}
    old_by_content = {}
    for k, v in old_free.items():
        old_by_content.setdefault(_content_key(v), k)

    consumed = set()
    for rid, nr in new.items():
        if rid in matched_ids:
            continue
        k = old_by_content.get(_content_key(nr))
        if k is not None and k not in consumed:
            consumed.add(k)
            reidentified.append({"record": nr, "old_id": k, "new_id": rid})
        else:
            added.append(nr)

    removed = [old[k] for k in old_free if k not in consumed]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "previous": len(old), "current": len(new),
            "added": len(added), "changed": len(changed),
            "reparsed": len(reparsed),
            "removed": len(removed), "reidentified": len(reidentified),
            "unchanged": (len(new) - len(added) - len(changed)
                          - len(reparsed) - len(reidentified)),
        },
        "added": added,
        "changed": changed,
        "reparsed": reparsed,
        "removed": removed,
        "reidentified": reidentified,
        "_note": "changed = 正文哈希变了（学校真的改了）；"
                 "reparsed = 正文没变但我们抽出的字段变了（我们改了代码）。"
                 "**别把 reparsed 显示成'通知被修改'。**",
    }


def render_markdown(diff, label=None):
    c = diff["counts"]
    L = [f"# 本次更新  {diff['generated_at']}", ""]
    if label:
        # 不要再套一层 ** —— 调用方传来的 label 自己可能已经有加粗，
        # 套两层会渲染成 "> ****回放…**" 这种坏样子。
        L += [f"> {label}", ""]
    L.append(f"上次 {c['previous']} 条 → 本次 {c['current']} 条 ｜ "
             f"**新增 {c['added']}** ｜ 有变化 **{c['changed']}** ｜ "
             f"本轮未抓到 {c['removed']} ｜ 未变 {c['unchanged']}")
    if c.get("reparsed"):
        L.append(f"｜ 重新解析 {c['reparsed']}（**我们改了抽取代码，不是学校改了通知**）")
    if c.get("reidentified"):
        L.append(f"｜ id 变更 {c['reidentified']}（内容完全相同，不计入新增/未抓到）")
    L.append("")

    if c["added"] == 0 and c["changed"] == 0:
        L += ["> 本次没有新增或变化。如果你在等某个学校的通知，说明它还没发。", ""]

    if diff.get("reparsed"):
        L += [f"## 重新解析（{len(diff['reparsed'])} 条，**正文未变**）", "",
              "正文哈希和上次完全一致，只有我们抽出来的字段不同了 —— "
              "**这是我们改了抽取代码，不是学校改了通知。**", ""]
        for x in diff["reparsed"][:20]:
            r = x["record"]
            L.append(f"- [{r.get('school')} · {r.get('unit')}] {r.get('title')}")
            for k, (ov, nv) in (x.get("fields") or {}).items():
                L.append(f"    - `{k}`：`{str(ov)[:60]}` → `{str(nv)[:60]}`")
        L.append("")

    if diff.get("reidentified"):
        L += [f"## id 变更（{len(diff['reidentified'])} 条，内容未变）", "",
              "同一条公告的 `id` 变了，但标题、日期、正文哈希都一致 —— "
              "**这通常来自抓取算法调整，不是新公告、也不是公告消失。**", ""]
        for x in diff["reidentified"][:20]:
            L.append(f"- [{x['record'].get('school')} · {x['record'].get('unit')}] "
                     f"{x['record'].get('title')}  ")
            L.append(f"  `{x['old_id']}` → `{x['new_id']}`")
        if len(diff["reidentified"]) > 20:
            L.append(f"- …另有 {len(diff['reidentified']) - 20} 条")
        L.append("")

    if diff["added"]:
        L += ["## 新增公告", ""]
        for r in sorted(diff["added"], key=lambda x: x.get("publish_date") or "", reverse=True):
            flags = [r.get("apply_mode") or "—"]
            if r.get("deadline"):
                flags.append(f"截止 {r['deadline']}")
            L.append(f"- **[{r.get('school')} · {r.get('unit')}] {r.get('title')}**  ")
            L.append(f"  {r.get('publish_date')} ｜ {' ｜ '.join(flags)}  ")
            L.append(f"  {r.get('url')}")
        L.append("")

    if diff["changed"]:
        L += ["## 有变化的公告（重点看这个）", "",
              "高校公告被静默修改是常态：改截止日、补附件、延期。"
              "**用户订阅付费，买的就是这一节。**", ""]
        for ch in diff["changed"]:
            r = ch["record"]
            L.append(f"### [{r.get('school')} · {r.get('unit')}] {r.get('title')}")
            L.append("")
            for k, (ov, nv) in ch["fields"].items():
                L.append(f"- `{k}`：{ov!r} → **{nv!r}**")
            if ch["body_changed"] and "body_text" not in ch["fields"]:
                L.append(f"- 正文内容变了（{ch['hash'][0][:14]}… → {ch['hash'][1][:14]}…），"
                         f"全文改动需人工核对")
            L.append(f"- {r.get('url')}")
            L.append("")

    if diff.get("site_unrefreshed"):
        L += [f"## 未刷新的站点（{len(diff['site_unrefreshed'])} 个，**沿用上一轮数据**）", "",
              "这些站点本轮没抓成功。它们的记录**没有**被当成「公告消失」——"
              "而是原样沿用了上一轮那份，所以下面「本轮未抓到」不含它们。", ""]
        for u in diff["site_unrefreshed"]:
            L.append(f"- [{u.get('school')} · {u.get('unit')}] "
                     f"`{u.get('reason')}`：沿用 {u.get('kept')}/{u.get('previous')} 条")
        L.append("")

    if diff["removed"]:
        L += ["## 本轮未抓到的公告", "",
              "**这不等于公告被删除**，更可能是翻页、栏目改版或本轮抓取失败。"
              "不要据此判断通知撤销。", ""]
        for r in diff["removed"][:30]:
            L.append(f"- [{r.get('school')} · {r.get('unit')}] {r.get('title')}")
        L.append("")
    return "\n".join(L)


def run(previous, records, out_dir=None, out_md=None, label=None, extra=None):
    """对比并落盘。previous 可以是**上一轮**的 jsonl 路径，也可以直接是已加载的 dict。

    传 dict 的用法是必需的：crawl.py 要先把旧文件读进内存，才能覆盖它。

    out_md / label 是给"回放"用的：演示时需要拿一个**真实的历史快照**当基准，
    把结果写到单独的文件（别覆盖线上那份 changes.md），并标明这是一次回放。

    extra 是调用方补的**附加结论**（目前只有 `site_unrefreshed`：哪些站点本轮
    没刷成功、沿用了多少条）。并进 diff 字典，直接落进 changes.json ——
    "我们没刷新"必须是个能读出来的字段，而不是靠人去数少了几个单位。
    """
    old = previous if isinstance(previous, dict) else load_jsonl(previous)
    diff = compare(old, records)
    if extra:
        diff.update(extra)
    out_dir = out_dir or common.OUT_DIR
    md = render_markdown(diff, label=label)
    with open(out_md or os.path.join(out_dir, "changes.md"), "w", encoding="utf-8") as f:
        f.write(md)
    # 坑（已踩）：这里原来**无条件**写 changes.json。
    # 于是跑一次"回放"（--out 指到 changes.replay.md）就把线上那份结构化数据
    # 覆盖成了回放结果 —— 而 C 的今日更新页读的正是 changes.json，
    # 他会看到一份"指向 22:29 基准"的假数据，而且没有任何报错。
    # 现在：回放模式下 JSON 跟着 md 走（changes.replay.json），不碰线上那份。
    json_path = (out_md.rsplit(".", 1)[0] + ".json") if out_md \
        else os.path.join(out_dir, "changes.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=1, default=str)
    return diff


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="对比两轮抓取，看今天多了什么、什么被改了")
    ap.add_argument("--previous", required=True, help="上一轮的 notices.jsonl 路径")
    ap.add_argument("--current", default=os.path.join(common.OUT_DIR, "notices.jsonl"))
    ap.add_argument("--no-write", action="store_true", help="只打印，不写 changes.md")
    ap.add_argument("--out", default=None, help="写到别的文件（做回放时用，别覆盖线上那份）")
    ap.add_argument("--label", default=None, help="在报告顶部写一行说明（标明这是回放）")
    a = ap.parse_args()

    cur = list(load_jsonl(a.current).values())
    if a.no_write:
        print(render_markdown(compare(load_jsonl(a.previous), cur), label=a.label))
    else:
        # 坑（已踩）：这里原来写的是 print(render_markdown(run(...))) ——
        # run() 返回的是 **diff 字典**不是 markdown，于是又渲染了一遍，
        # 控制台里看不到 --label（文件里其实有，所以很容易误判成 label 没生效）。
        d = run(a.previous, cur, out_md=a.out, label=a.label)
        print(render_markdown(d, label=a.label))
