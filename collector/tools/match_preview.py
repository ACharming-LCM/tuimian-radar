# -*- coding: utf-8 -*-
"""匹配打分参考实现。规则契约见 docs/matching-spec.md。

**这是给 B 抄的基线，不是生产代码。** 纯标准库，不 import 本项目的任何模块，
就是为了 B 能直接读懂、直接搬进 server/。

用法（在 collector 目录下）：
    python tools/match_preview.py
    python tools/match_preview.py --profile tools/profile-example.json
    python tools/match_preview.py --min-score 0.5 --limit 20
    python tools/match_preview.py --json            # 机器可读，给 B 对接

归属：A（参考实现）。B 若改规则，改 docs/matching-spec.md 并记 contracts/CHANGELOG.md。
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
NOTICES = os.path.join(ROOT, "data", "out", "notices.jsonl")
FACULTY = os.path.join(ROOT, "data", "out", "faculty.jsonl")

# 权重之和必须等于 1。改权重前先想清楚"缺字段给 0.4"这条还成不成立。
WEIGHTS = {
    "major": 0.30,
    "mode": 0.20,
    "level": 0.15,
    "affinity": 0.15,
    "year": 0.10,
    "freshness": 0.10,
}
MISSING = 0.4          # 字段缺失的得分：降权，但不判定为"不相关"
CONF_FLOOR = 0.7       # 置信度降权的下限系数

# 材料缺口检查：只对真正的"硬门槛"词提醒。
# 坑（已踩）：v1 把 论文/科研成果/专家推荐 也放进来，结果几乎每条公告都刷一遍提醒
# （因为 materials 字段本身就是全文关键词扫描的产物，精度不够），
# 真提醒被淹没。这里只保留"没有就很可能不符合条件"的几个，并放软语气。
# 另外：四级由六级推导，不单独提醒 —— 六级 580 的人被提示"要求四级"是纯噪声。
MATERIAL_SIGNALS = {
    "六级": ("cet6", "六级"),
    "托福": ("toefl", "托福"),
    "雅思": ("ielts", "雅思"),
    "推荐信": ("recommenders", "推荐信"),
}


def _hit(a, b):
    """互为子串即算命中：'物理学' vs '物理学类' 要能匹配上。"""
    return bool(a) and bool(b) and (a in b or b in a)


def score_notice(rec, prof, today):
    """返回 (raw_score, reasons, warnings, detail_scores)。"""
    n_majors = rec.get("majors") or []
    p_majors = prof.get("majors") or []

    # ---- major ----
    if not n_majors:
        major, major_note = MISSING, None
    elif any(_hit(m, p) for m in n_majors for p in p_majors):
        hits = [m for m in n_majors for p in p_majors if _hit(m, p)]
        major, major_note = 1.0, f"专业对口：{'、'.join(dict.fromkeys(hits))}"
    else:
        major, major_note = 0.0, f"专业不符（公告是 {'、'.join(n_majors[:3])}）"

    # ---- mode ----
    mode = rec.get("apply_mode")
    wanted = prof.get("wanted_modes") or []
    if mode and mode in wanted:
        mode_s, mode_note = 1.0, mode
    elif not mode:
        mode_s, mode_note = MISSING, None
    else:
        mode_s, mode_note = 0.0, None

    # ---- level ----
    levels = rec.get("degree_types") or []
    targets = prof.get("target_levels") or []
    title = rec.get("title") or ""
    contradiction = None
    mismatch = levels and targets and not any(_hit(l, t) for l in levels for t in targets)
    if mismatch:
        # 坑（已踩）：抽取与标题自相矛盾时，"不匹配"这个判断本身不可信。
        # 真实案例：东南大学2027年**硕士**研究生招生章程被抽出 degree_types=["博士"]，
        # 打分照样给出 0.88 排到第 2 —— 抽取错误就这么悄悄污染了排序。
        # 处理原则：**已知不可信的字段不许参与排序裁判**，降为"缺失"并明确报警。
        if ("硕士" in title and "博士" not in title and "博士" in levels) or \
           ("博士" in title and "硕士" not in title and "硕士" in levels):
            t_word = "硕士" if "硕士" in title else "博士"
            contradiction = (f"标题写「{t_word}」但抽取到「{'、'.join(levels)}」，"
                             f"层次判断可能有误，请核对原文")
            level, level_note = MISSING, None
        else:
            level, level_note = 0.0, f"层次不符（公告是 {'、'.join(levels)}）"
    elif not levels:
        level, level_note = MISSING, None
    else:
        level, level_note = 1.0, "、".join(levels)

    # ---- affinity（学校 / 单位）----
    units = prof.get("preferred_units") or []
    schools = prof.get("preferred_schools") or []
    if any(_hit(rec.get("unit", ""), u) for u in units):
        aff, aff_note = 1.0, f"目标单位：{rec.get('unit')}"
    elif any(_hit(rec.get("school", ""), s) for s in schools):
        aff, aff_note = 0.7, f"目标学校：{rec.get('school')}"
    else:
        aff, aff_note = 0.2, None

    # ---- year ----
    y, want_y = rec.get("target_year"), prof.get("target_entry_year")
    if y is None:
        year, year_note = 0.3, None
    elif want_y is None or y == want_y:
        year, year_note = 1.0, f"{y} 年招生（对应你这一届）"
    elif y > want_y:
        year, year_note = 0.6, f"{y} 年招生（比你晚一届，可作参考）"
    else:
        year, year_note = 0.0, f"{y} 年招生（已过届）"

    # ---- freshness ----
    pub = rec.get("publish_date")
    if pub:
        try:
            d0 = datetime.strptime(pub, "%Y-%m-%d").date()
            age = (today - d0).days
            fresh = 1.0 if age <= 30 else 0.7 if age <= 90 else 0.4 if age <= 365 else 0.1
        except ValueError:
            fresh = 0.2
    else:
        fresh = 0.2
    fresh_note = None

    parts = {"major": major, "mode": mode_s, "level": level,
             "affinity": aff, "year": year, "freshness": fresh}
    raw = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)

    reasons = [n for n in (major_note, mode_note, level_note, aff_note, year_note, fresh_note) if n]
    warnings = []

    # ---- 置信度降权 ----
    conf = (rec.get("extract") or {}).get("confidence")
    conf = 0.5 if conf is None else float(conf)
    final = raw * (CONF_FLOOR + (1 - CONF_FLOOR) * conf)

    # ---- 必配的提醒 ----
    if not rec.get("deadline"):
        warnings.append("未抽到截止日期，务必点开原文确认")
    if conf < 0.5:
        warnings.append(f"该公告字段抽取置信度低（{conf:.2f}），请核对原文")
    if contradiction:
        warnings.append(contradiction)

    mats = rec.get("materials") or []
    eng = prof.get("english") or {}
    have = {"cet6": eng.get("cet6"), "toefl": eng.get("toefl"), "ielts": eng.get("ielts"),
            "recommenders": prof.get("recommenders")}
    for word, (key, label) in MATERIAL_SIGNALS.items():
        if word in mats and not have.get(key):
            warnings.append(f"公告提到「{label}」，若你没有相应材料请先确认是否影响申请")

    return round(final, 4), round(raw, 4), reasons, warnings, parts


def bucket_of(deadline, today, rec=None):
    """按截止日分桶。**`unknown` 不许被当成 `absent`**（契约第 14 节）。

    没有截止日时**要看三态**，不能一律说"需人工确认"：
      · `unknown`（我们没读到）→ `需人工确认` —— 用户必须去看原文
      · `absent`（正文读到了、确实没有）→ `无截止日` —— 这是一个**确定回答**
    两者混成一个桶，就等于把"不知道"说成"知道"；而现在数据层已经分得开了，
    再不分就是把信息扔掉。

    > 上下文（2026-10-17）：这条是**参考实现落后于服务端**。
    > B 在 `server/match.py` 里先按契约第 14 节做了三态分桶，
    > 而当时数据层还没有 `field_state`，所以这个工具一直没被发现没跟上 ——
    > 直到 D 把 `field_state` 落盘，门禁里"与参考实现逐字段一致"那条立刻红了。
    > **门禁红在了两边都没做错的地方**：一边是新的契约，一边是旧的参考。
    > 参考实现跟着契约走，所以这里改成和服务端一致。

    `rec` 省略时按老口径（`unknown`）处理 —— 这样"只有 deadline 一个入参"的老调用
    不会悄悄变成 `absent`（**不知道 ≠ 没有**，宁可保守）。
    """
    if not deadline:
        fs = (rec or {}).get("field_state") or {}
        if fs.get("deadline") == "absent":
            return "无截止日", None
        return "需人工确认", None
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        return "需人工确认", None
    left = (d - today).days
    if left < 0:
        return "已过期", left
    if left <= 7:
        return "立即行动", left
    if left <= 30:
        return "重点关注", left
    return "可规划", left


def match(records, prof, today):
    dropped_expired = dropped_mode = 0
    dropped_nonapply = dropped_kind = dropped_junk = 0
    wanted = prof.get("wanted_modes") or []
    out = []
    for rec in records:
        # 匹配页承诺「推免 / 夏令营 / 预推免公告」，只收招生类。
        if rec.get("record_quality") == "junk":
            dropped_junk += 1
            continue
        topic = rec.get("topic")
        if topic is not None and topic != "apply":
            dropped_nonapply += 1
            continue
        kind = rec.get("notice_kind")
        if kind != "announcement":
            dropped_kind += 1
            continue
        title = rec.get("title") or ""
        if not any(w in title for w in ("推免", "免试", "夏令营", "预推免",
                                        "直博", "优秀应届本科毕业生")):
            dropped_nonapply += 1
            continue
        bucket, left = bucket_of(rec.get("deadline"), today, rec)
        # 硬过滤 ① 已过期
        if prof.get("exclude_expired") and bucket == "已过期":
            dropped_expired += 1
            continue
        # 硬过滤 ② 招生方式不符（null 不丢 —— 67% 的记录抽不到它）
        if rec.get("apply_mode") and wanted and rec["apply_mode"] not in wanted:
            dropped_mode += 1
            continue

        final, raw, reasons, warnings, parts = score_notice(rec, prof, today)
        out.append({
            "id": rec.get("id"), "score": final, "raw_score": raw, "bucket": bucket,
            "days_left": left, "title": rec.get("title"), "school": rec.get("school"),
            "unit": rec.get("unit"), "publish_date": rec.get("publish_date"),
            "deadline": rec.get("deadline"), "url": rec.get("url"),
            "apply_mode": rec.get("apply_mode"), "target_year": rec.get("target_year"),
            "majors": rec.get("majors"), "attachments": len(rec.get("attachments") or []),
            "reasons": reasons, "warnings": warnings, "parts": parts,
        })
    out.sort(key=lambda x: -x["score"])
    stats = {"total": len(records), "dropped_expired": dropped_expired,
             "dropped_mode": dropped_mode,
             "dropped_nonapply": dropped_nonapply,
             "dropped_kind": dropped_kind,
             "dropped_junk": dropped_junk,
             "scored": len(out)}
    return out, stats


def load_profile(path):
    """读画像 JSON。

    坑（已踩）：**必须用 utf-8-sig，不能用 utf-8。**
    画像文件是给人手改的，而 Windows 记事本/PowerShell 的 `Set-Content -Encoding utf8`
    会加一个 BOM，`json.load` 直接报 "Unexpected UTF-8 BOM"。
    utf-8-sig 对带 BOM 和不带 BOM 的文件都能读，所以没有代价。
    我们的数据文件（notices.jsonl / faculty.jsonl）是自己写的，不带 BOM，用普通 utf-8 即可。
    """
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def match_faculty(faculty, prof):
    """把导师和用户兴趣对上。这是"专业契合的实验室"这一层唯一的数据支撑。

    打分刻意简单，因为**可解释比准确更重要**：用户要能一眼看出
    "为什么给我推这个老师"，然后自己去核对。任何说不清理由的排序都不该出现。

    score = 0.70 × 兴趣覆盖率          （命中几个兴趣词 / 一共写了几个）
          + 0.15 × 单位匹配
          + 0.15 × 层次匹配（博导 / 硕导）
    兴趣词为空时退化为用 majors 做子串匹配，并明确告诉用户"没填兴趣词，结果较粗"。
    """
    kws = [k for k in (prof.get("interest_keywords") or []) if k]
    majors = [m for m in (prof.get("majors") or []) if m]
    targets = prof.get("target_levels") or []
    units = prof.get("preferred_units") or []
    schools = prof.get("preferred_schools") or []
    fallback = not kws
    probes = kws or majors

    out = []
    for f in faculty:
        areas = f.get("research_areas") or []
        hits = []
        for p in probes:
            for a in areas:
                if _hit(p, a) and a not in hits:
                    hits.append(a)
        if not probes:
            coverage = 0.3                      # 什么都没填，给个中性分，不排前面
        else:
            coverage = min(1.0, len(hits) / len(probes))

        if any(_hit(f.get("unit", ""), u) for u in units):
            aff, aff_note = 1.0, f"目标单位：{f.get('unit')}"
        elif any(_hit(f.get("school", ""), s) for s in schools):
            aff, aff_note = 0.7, None
        else:
            aff, aff_note = 0.2, None

        wants_doc = any(_hit(t, "博士") or _hit(t, "直博") for t in targets)
        if f.get("is_phd_supervisor") and wants_doc:
            lvl, lvl_note = 1.0, "博士生导师"
        elif f.get("is_phd_supervisor"):
            lvl, lvl_note = 0.8, "博士生导师"
        else:
            lvl, lvl_note = 0.6, None

        score = round(0.70 * coverage + 0.15 * aff + 0.15 * lvl, 4)
        reasons = []
        if hits:
            reasons.append(f"研究方向命中：{'、'.join(hits[:4])}")
        if lvl_note:
            reasons.append(lvl_note)
        if aff_note:
            reasons.append(aff_note)
        warnings = []
        if fallback:
            warnings.append("你没填 interest_keywords，这里只用专业名做了粗匹配")
        if not areas:
            warnings.append("该导师页面没有研究方向，无法判断契合度")
        if not f.get("email"):
            warnings.append("没抽到邮箱，联系前请到个人主页核对")

        out.append({
            "id": f.get("id"), "name": f.get("name"), "title": f.get("title"),
            "school": f.get("school"), "unit": f.get("unit"), "group": f.get("group"),
            "email": f.get("email"), "homepage": f.get("homepage"),
            "research_areas": areas, "hits": hits,
            "score": score, "reasons": reasons, "warnings": warnings,
        })
    out.sort(key=lambda x: (-x["score"], x["name"] or ""))
    return out


def render_faculty(items, prof, limit):
    kws = prof.get("interest_keywords") or []
    print(f"\n导师匹配：{prof.get('name') or '未命名'} | 兴趣词："
          f"{'、'.join(kws) if kws else '（未填，用专业名粗匹配）'}")
    print(f"导师库 {len(items)} 人 → 显示前 {min(limit, len(items))}\n")
    for it in items[:limit]:
        print(f"[{it['score']:.2f}] {it['name']}  {it['title'] or ''}")
        print(f"   {it['school']} / {it['unit']} / {it['group'] or '—'}")
        print(f"   方向：{'、'.join(it['research_areas'][:8]) or '（无）'}")
        if it["reasons"]:
            print(f"   + {' · '.join(it['reasons'])}")
        if it["warnings"]:
            print(f"   ! {' · '.join(it['warnings'])}")
        print(f"   {it['email'] or '—'}   {it['homepage'] or ''}")
        print()


def render(items, stats, prof):
    print(f"\n画像：{prof.get('name') or '未命名'} | {prof.get('school')} · "
          f"{'、'.join(prof.get('majors') or [])} | 目标 {prof.get('target_entry_year')} 年入学")
    print(f"筛选：{stats['total']} 条公告 → 丢弃已过期 {stats['dropped_expired']}、"
          f"招生方式不符 {stats['dropped_mode']}、非招生 {stats['dropped_nonapply']}、"
          f"目录/在职 {stats['dropped_kind']}、脏记录 {stats['dropped_junk']} "
          f"→ 打分 {stats['scored']} 条\n")
    if not items:
        print("  没有匹配结果。检查 profile 的 wanted_modes / exclude_expired。")
        return
    for it in items:
        left = f"剩 {it['days_left']} 天" if it["days_left"] is not None else "无截止日"
        print(f"[{it['score']:.2f}] {it['bucket']:<6} {it['deadline'] or '—':<11} {left}")
        print(f"   {it['school']} / {it['unit']}  ·  {it['title']}")
        if it["reasons"]:
            print(f"   + {' · '.join(it['reasons'])}")
        if it["warnings"]:
            print(f"   ! {' · '.join(it['warnings'])}")
        print(f"   {it['url']}")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=os.path.join(HERE, "profile-example.json"))
    ap.add_argument("--notices", default=NOTICES)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--faculty", action="store_true",
                    help="改用导师匹配（读 data/out/faculty.jsonl）")
    ap.add_argument("--faculty-file", default=FACULTY)
    args = ap.parse_args()

    prof = load_profile(args.profile)

    if args.faculty:
        if not os.path.exists(args.faculty_file):
            print(f"导师库不存在：{args.faculty_file}\n先跑 python crawl_faculty.py")
            sys.exit(1)
        with open(args.faculty_file, encoding="utf-8") as f:
            fac = [json.loads(ln) for ln in f if ln.strip()]
        items = match_faculty(fac, prof)
        items = [i for i in items if i["score"] >= args.min_score]
        if args.json:
            print(json.dumps({"profile": prof, "items": items[: args.limit or None]},
                             ensure_ascii=False, indent=1))
            return
        render_faculty(items, prof, args.limit or 10)
        return

    with open(args.notices, encoding="utf-8") as f:
        records = [json.loads(ln) for ln in f if ln.strip()]

    today = date.today()
    items, stats = match(records, prof, today)
    items = [i for i in items if i["score"] >= args.min_score]

    if args.json:
        print(json.dumps({"profile": prof,
                          "generated_at": datetime.now().isoformat(timespec="seconds"),
                          "stats": stats, "items": items[: args.limit or None]},
                         ensure_ascii=False, indent=1))
        return
    render(items[: args.limit or None], stats, prof)


if __name__ == "__main__":
    main()
