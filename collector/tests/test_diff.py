# -*- coding: utf-8 -*-
"""变更检测回归测试。不依赖 pytest：

    cd collector && python tests/test_diff.py

为什么要它：增量检测的失败模式很隐蔽 —— 比如"顺序反了导致每次都报全部新增"，
或者"把翻页没抓到的公告报成被删除"。这两种错都会直接误导用户，
而且在小数据量下看不出来。
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diff          # noqa: E402

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}  实际 {got!r}")
        FAILED.append(name)


def rec(rid, **kw):
    body = kw.pop("body", "正文")
    base = {
        "id": rid, "title": "标题", "school": "浙江大学", "unit": "物理学院",
        "publish_date": "2026-09-10", "deadline": "2026-10-01", "body_text": body,
        "content_hash": "sha1:" + hashlib.sha1(body.encode("utf-8")).hexdigest(),
        "apply_mode": "推免", "target_year": 2027, "majors": ["物理学"],
        "attachments": [], "url": "http://x/",
    }
    base.update(kw)
    return base


print("== 基础分类 ==")
old = {"a": rec("a"), "b": rec("b")}
new = [rec("a"),                                  # 未变
       rec("b", deadline="2026-11-20"),           # 只改了**我们抽出来的**字段
       rec("c")]                                  # 新增
d = diff.compare(old, new)
check("新增 1 条", d["counts"]["added"], 1)
# 注意这里：deadline 变了但正文没变 —— 那是**我们的解析结果**变了，不是学校改了通知。
# 这两件事混在一起会让日报说"有 22 条通知被改了"，而实际上公告一个字没动。
# 用户会照着一个假的"变更"去做决定，日报也就没人信了。
check("有变化 0 条（正文没变，不算学校改了）", d["counts"]["changed"], 0)
check("重新解析 1 条", d["counts"]["reparsed"], 1)
check("未变 1 条", d["counts"]["unchanged"], 1)
check("本轮未抓到 0 条", d["counts"]["removed"], 0)
check("变更字段是 deadline",
      list(d["reparsed"][0]["fields"].keys()), ["deadline"])

print("\n== 正文真的被改了 → 才算'有变化' ==")
d2 = diff.compare({"a": rec("a")}, [rec("a", body="学校把这条通知的正文改了")])
check("有变化 1 条", d2["counts"]["changed"], 1)
check("重新解析 0 条", d2["counts"]["reparsed"], 0)
check("body_changed 为真", d2["changed"][0]["body_changed"], True)

print("\n== 两种同时发生 ==")
d3 = diff.compare({"a": rec("a", deadline="2026-10-01")},
                  [rec("a", body="换了正文", deadline="2026-11-30")])
check("正文变了 → 归到'有变化'（不重复计入重新解析）",
      (d3["counts"]["changed"], d3["counts"]["reparsed"]), (1, 0))
check("变更前后的值都对",
      d["reparsed"][0]["fields"]["deadline"], ("2026-10-01", "2026-11-20"))

print("\n== 正文被静默修改（高校公告改截止日、补附件，这是订阅付费的核心）==")
old = {"a": rec("a", body="原来的正文")}
new = [rec("a", body="后来改过的正文", content_hash=None)]   # 故意不给 hash，验回退现算
d = diff.compare(old, new)
check("正文变化被检出", d["changed"][0]["body_changed"], True)
check("没有 content_hash 时能现算", d["counts"]["changed"], 1)

print("\n== 附件变化 ==")
old = {"a": rec("a", attachments=[{"url": "u1", "text_len": 100}])}
new = [rec("a", attachments=[{"url": "u1", "text_len": 100},
                             {"url": "u2", "text_len": 0}])]
d = diff.compare(old, new)
check("新增附件算变化", "attachments" in d["changed"][0]["fields"], True)

print("\n== 抽取器版本参与归因（正文是我们抽的，不是学校发的）==")
# 同一个抽取器版本 → 正文哈希变了就是学校改了内容
d = diff.compare({"a": rec("a", body="旧正文")}, [rec("a", body="新正文")])
check("版本相同 → 算有变化", d["counts"]["changed"], 1)
# 抽取器版本不同 → 正文哈希差异归因于**我们换了抽取方式**
V3 = {"method": "rule", "version": "v3", "confidence": 0.5}
V4 = {"method": "rule", "version": "v4", "confidence": 0.5}
d = diff.compare({"a": rec("a", body="旧抽取结果", extract=V3)},
                 [rec("a", body="新抽取结果", extract=V4)])
check("版本不同 → 算重新解析", (d["counts"]["changed"], d["counts"]["reparsed"]), (0, 1))
# 实测场景：PDF 表格从"一单元格一行"改成"按行拼接"，67 个 PDF 正文全变
d = diff.compare({"a": rec("a", body="序号\n学院\n姓名", extract=V3)},
                 [rec("a", body="序号 | 学院 | 姓名", extract=V4)])
check("PDF 表格拼接不能报成'学校改了通知'",
      (d["counts"]["changed"], d["counts"]["reparsed"]), (0, 1))

print("\n== 附件的 text_len 是抽取结果，URL 才是学校动作 ==")
old = {"a": rec("a", attachments=[{"url": "u1", "text_len": 100}])}
d = diff.compare(old, [rec("a", attachments=[{"url": "u1", "text_len": 180}])])
check("只有 text_len 变 → 重新解析（不是学校加文件）",
      (d["counts"]["changed"], d["counts"]["reparsed"]), (0, 1))

print("\n== '本轮未抓到' 必须单独一类，不能被当成删除 ==")
old = {"a": rec("a"), "gone": rec("gone")}
d = diff.compare(old, [rec("a")])
check("消失的进 removed", len(d["removed"]), 1)
md = diff.render_markdown(d)
check("报告里明确写了不等于删除", "这不等于公告被删除" in md, True)

print("\n== 没有变化时不能瞎报 ==")
d = diff.compare({"a": rec("a")}, [rec("a")])
check("全未变", (d["counts"]["added"], d["counts"]["changed"]), (0, 0))
check("报告里明确说没有新增", "本次没有新增或变化" in diff.render_markdown(d), True)

print("\n== 第一次跑没有基线 ==")
check("文件不存在返回空 dict", diff.load_jsonl("不存在的文件.jsonl"), {})
d = diff.compare({}, [rec("a")])
check("首次全部算新增", d["counts"]["added"], 1)

print("\n== 坏行不能炸掉整个文件 ==")
tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bad.jsonl")
with open(tmp, "w", encoding="utf-8") as f:
    f.write('{"id":"a","title":"好的"}\n')
    f.write("{这不是 json\n")
    f.write("\n")
loaded = diff.load_jsonl(tmp)
os.remove(tmp)
check("坏行被跳过，好行保留", list(loaded.keys()), ["a"])

print("\n== id 变了但公告没变：不能报成『新增 + 未抓到』==")
# 真实事故：我调整了 make_id 的兜底算法（改用归一化 url 算哈希），30 条记录 id 变了。
# 旧逻辑只按 id 比对，于是这 30 条**同时**被报成「新增 30」和「本轮未抓到 30」——
# 而它们一个字都没变。C 在页面上看到 added[0] 和 removed[0] 是同一篇公告，
# 如实标了「不是真的新发或丢失」，然后演示第 1 步就废了。
old = {"OLD_ID_1": rec("OLD_ID_1", title="公告甲"), "OLD_ID_2": rec("OLD_ID_2", title="公告乙")}
new = [rec("NEW_ID_1", title="公告甲"), rec("NEW_ID_2", title="公告乙")]
d = diff.compare(old, new)
check("不报新增", d["counts"]["added"], 0)
check("不报未抓到", d["counts"]["removed"], 0)
check("单独归到 reidentified", d["counts"]["reidentified"], 2)
check("未变计数也对", d["counts"]["unchanged"], 0)
md = diff.render_markdown(d)
check("报告里说明了 id 变更不是新公告", "不是新公告、也不是公告消失" in md, True)

print("\n== 但真的新公告仍要报成新增（别把上面那条修过头）==")
old = {"A": rec("A")}
new = [rec("A"), rec("B", title="完全不同的另一篇")]      # 内容身份也不同
d = diff.compare(old, new)
check("真新公告进 added", d["counts"]["added"], 1)
check("不被误判成 id 变更", d["counts"]["reidentified"], 0)

print("\n== 内容身份相同时不能重复消费同一条老记录 ==")
# 两条新记录内容完全一样（理论上不该有，但去重前可能出现），老记录只有一条
old = {"OLD": rec("OLD")}
new = [rec("N1"), rec("N2")]
d = diff.compare(old, new)
check("老记录只被消费一次", d["counts"]["reidentified"], 1)
check("多出来的那条算新增", d["counts"]["added"], 1)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 个用例失败：{FAILED}")
    sys.exit(1)
print("✅ 全部通过")
