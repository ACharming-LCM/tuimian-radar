# -*- coding: utf-8 -*-
"""评估"栏目自动发现"的精度与召回，用人工判定当基准。

**为什么需要它**：`discover.py` 里的正/负向词表是很容易"越改越自信"的东西 ——
多丢几个栏目，精度数字就好看，但可能把真正有用的栏目也丢了，而这件事
**在没有基准的情况下看不出来**。

基准文件：`data/out/board-verdicts.md`（人工逐条核对，含 URL 和判定）。
注意这份基准本身**不完美**：实测抽查发现它把"能解析出 7 条党建通知"的栏目
判成了"空栏目"。所以本工具只把它当**方向性**基准，不做绝对结论。

用法（在 collector 目录下）：
    python tools/eval_discovery.py              # 跑对照实验，写报告
    python tools/eval_discovery.py --no-write   # 只打印

归属：A。产出 data/out/discovery-eval.md
"""
import argparse
import io
import json
import os
import re
import sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import common        # noqa: E402
import discover      # noqa: E402

VERDICTS = os.path.join(common.OUT_DIR, "board-verdicts.md")
SITES = os.path.join(os.path.dirname(HERE), "sites.json")


def norm(u):
    """URL 归一化：只比 host+path，忽略协议、端口、查询串、末尾斜杠和大小写。"""
    if not u:
        return ""
    p = urlparse(u.strip())
    host = (p.netloc or "").lower().split(":")[0]
    path = re.sub(r"/+$", "", p.path or "")
    return f"{host}{path}"


def load_verdicts(path=VERDICTS):
    """解析 board-verdicts.md 的表格 -> {归一化URL: 判定}。"""
    if not os.path.exists(path):
        print(f"基准文件不存在：{path}\n先让人工核对产出它。")
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 4 or cells[0] in ("学校", "") or set(cells[0]) <= set("-: "):
                continue
            url, verdict = cells[2], cells[3]
            if not url.startswith("http"):
                continue
            if verdict in ("正确", "错误", "空栏目", "抓不到"):
                out[norm(url)] = verdict
    return out


def evaluate(apply_filter, max_boards=4):
    """跑一遍全部站点，返回发现结果及与基准的比对。"""
    cfg = json.load(open(SITES, encoding="utf-8"))
    gt = load_verdicts()
    found, rejected = [], []
    for site in cfg["schools"]:
        kept, rej = discover.discover_boards(
            site["homepage"], max_boards, with_rejected=True, apply_filter=apply_filter)
        for b in kept:
            found.append({**b, "school": site["school"], "unit": site["unit"]})
        for b in rej:
            rejected.append({**b, "school": site["school"], "unit": site["unit"]})

    judged = {"正确": [], "错误": [], "空栏目": [], "抓不到": [], "未标注": []}
    for b in found:
        judged[gt.get(norm(b["url"]), "未标注")].append(b)
    return {"kept": found, "rejected": rejected, "judged": judged, "gt": gt}


def summarize(res, tag, max_boards):
    j, gt = res["judged"], res["gt"]
    labeled = len(j["正确"]) + len(j["错误"]) + len(j["空栏目"]) + len(j["抓不到"])
    prec = (len(j["正确"]) / labeled) if labeled else 0.0
    total_pos = sum(1 for v in gt.values() if v == "正确")
    recall = (len(j["正确"]) / total_pos) if total_pos else 0.0
    return {
        "tag": tag, "max_boards": max_boards, "kept": len(res["kept"]), "labeled": labeled,
        "正确": len(j["正确"]), "错误": len(j["错误"]),
        "空栏目": len(j["空栏目"]), "抓不到": len(j["抓不到"]),
        "未标注": len(j["未标注"]),
        "精度": prec, "召回": recall, "总正确数": total_pos,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    gt = load_verdicts()
    if not gt:
        sys.exit(1)

    print(f"基准：{len(gt)} 个已判定栏目，其中「正确」{sum(1 for v in gt.values() if v == '正确')} 个\n")
    print("跑对照实验：关闭过滤器@4 / 开启过滤器@4 / 开启过滤器@8 ...")
    runs = [("关闭过滤器（改之前）", evaluate(False, 4), 4),
            ("开启过滤器，top-4", evaluate(True, 4), 4),
            ("开启过滤器，top-8", evaluate(True, 8), 8)]
    res = [(tag, summarize(r, tag, mb), r) for tag, r, mb in runs]
    s0, s1, s2 = res[0][1], res[1][1], res[2][1]
    after = res[1][2]

    buf = io.StringIO()
    p = lambda *a: print(*a, file=buf)          # noqa: E731
    p("# 栏目自动发现：精度与召回评估\n")
    p(f"基准来源 `data/out/board-verdicts.md`（人工逐条核对，**本身不完美**，"
      f"见文件末尾的『基准的可信度』一节）。共 {len(gt)} 个已判定栏目，"
      f"其中「正确」{s0['总正确数']} 个。\n")
    p("## 对照结果\n")
    p("| 指标 | 关闭过滤器 | 开启过滤器 top-4 | 开启过滤器 top-8 |")
    p("|---|---|---|---|")
    for k in ("kept", "labeled", "正确", "错误", "空栏目", "抓不到", "未标注"):
        p(f"| {k} | {s0[k]} | {s1[k]} | {s2[k]} |")
    p(f"| **精度**（保留的里有多少是对的） | {s0['精度']:.0%} | **{s1['精度']:.0%}** | {s2['精度']:.0%} |")
    p(f"| **召回**（{s0['总正确数']} 个正确栏目保住了多少） | {s0['召回']:.0%} | {s1['召回']:.0%} | **{s2['召回']:.0%}** |")
    p("")
    p("> **精度和召回必须一起看。** 只报精度的话，"
      "把所有栏目都丢掉就能拿到 100% —— 那是自欺欺人。\n")
    p("> **top-N 截断会和评分互相干扰**（这是评估工具抓出来的第二个隐蔽问题）："
      "评分一变，站内排名就变，挤进 top-4 的栏目就换了一批 ——"
      "所以「召回下降」其实是「top-4 召回下降」。既然过滤器已经把噪声清掉了，"
      f"就应该放宽 top-N 把召回换回来：top-8 的召回是 {s2['召回']:.0%}。\n")

    lost = [b for b in after["rejected"] if after["gt"].get(norm(b["url"])) == "正确"]
    if lost:
        p(f"## ⚠️ 过滤器误杀了 {len(lost)} 个「正确」栏目（需要修词表）\n")
        for b in lost:
            p(f"- [{b['school']} · {b['unit']}] `{b['label']}` — {b['reason']}")
            p(f"  {b['url']}")
        p("")
    else:
        p("## ✅ 没有误杀任何「正确」栏目\n")

    p("## 被丢掉的栏目（含原因）\n")
    p("| 学校 | 单位 | 栏目名 | 分数 | 丢弃原因 | 基准判定 |")
    p("|---|---|---|---|---|---|")
    for b in sorted(after["rejected"], key=lambda x: -x["score"]):
        v = after["gt"].get(norm(b["url"]), "未标注")
        p(f"| {b['school']} | {b['unit']} | {b['label'][:26]} | {b['score']} | "
          f"{b['reason']} | {v} |")
    p("")

    p("## 还保留着但基准判为「错误」的栏目（过滤器没覆盖到的）\n")
    for b in after["judged"]["错误"]:
        p(f"- [{b['school']} · {b['unit']}] `{b['label']}`  ")
        p(f"  {b['url']}")
    p("")

    p("## 基准的可信度（必读）\n")
    p("抽查三条独立复核的结果：")
    p("")
    p("| 基准判定 | 我的复核 | 结论 |")
    p("|---|---|---|")
    p("| 浙大研招 `/tz_62881` 错误 | 确认是「在职攻读硕士学位」，条目止于 2020 | ✅ 一致 |")
    p("| 华科 4 个 `list.jsp` 抓不到 | 确认 HTTP 302 → `clogin.jsp`，是登录墙 | ✅ 一致 |")
    p("| 浙大材料 `/50959` **空栏目** | **实际解析出 7 条**（党建类通知） | ❌ **判错** |")
    p("")
    p("所以：**基准的『正确/错误』方向可信，『空栏目』要打折**。")
    p("第三条的正确说法是「内容不相关」，不是「解析失败」—— 若照字面信了它，")
    p("就会去查一个根本不存在的选择器 bug。")
    p("**这也是『不拿 agent 的结论当结论』的原因：它的底线可能对，标签可能是错的。**\n")

    report = os.path.join(common.OUT_DIR, "discovery-eval.md")
    if not args.no_write:
        with open(report, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
    print(buf.getvalue())
    if not args.no_write:
        print(f"\n报告 -> {report}")


if __name__ == "__main__":
    main()
