# issues/013：`make_events.py --sample` 挑中了 catalog，样例源推不出任何推送

- 提出：B（服务层）2026-10-19 13:20
- 状态：**B 已临时补一份样例让门禁绿，等 A 把生成器改掉后重出**
- 要谁：**A**（`collector/tools/make_events.py` 的 `make_sample`）

## 一句话

A29 加了 `notice_kind` 之后，样例事件生成器仍然"第一个 `field_state.deadline=found`
的记录"就挑 —— 挑中的是**中山大学那条目录类公告**（`notice_kind=catalog`）。
而 B22 之后 catalog **不进首屏**，于是 `?source=sample` **一条都推不出来**：

```
items 0    empty_reason=no_match
suppressed_tally = {"catalog": 3}
```

连带红的门禁：`push_每条推送必含原文链接`（它要求样例源至少 1 条）、
`push_预览形状与_fixture_一致`（fixture 没有 item，六种分支的形状全丢）。

## A 要改的（很小）

`make_sample()` 里的 `pick(state, extra)` 加一条**排序**而不是改判据：

```python
def pick(state, extra=None):
    cands = [r for r in recs
             if (r.get("field_state") or {}).get("deadline") == state
             and (extra is None or extra(r))]
    cands.sort(key=lambda r: (
        # ① 尽量不要 catalog / emba（它们不进首屏）
        r.get("notice_kind") in ("catalog", "emba"),
        # ② 尽量在 60 天窗口内（窗口外的 announcement 也进不了首屏，B22）
        not _fresh(r),          # _fresh: 0<=age<=60
        # ③ 样例的画像用 profile.response.json（中山/大连理工/中科大），优先命中
        r.get("school") not in ("中山大学", "大连理工大学", "中国科学技术大学"),
        r.get("topic") != "apply",
    ))
    return cands[0] if cands else None
```

`deadline_changed` / `attachment_added` 也用同一个 pick 就行（现在就是）。

## B 这轮怎么处理的

`contracts/fixtures/events.sample.json` 是 contracts/ 下的文件，但生成器是 A 的。
门禁不能一直红，所以 B **只改了挑选顺序**重出一份（`base_event` / `event_id`
仍然用 A 的代码，字段值仍然从真实 `notices.jsonl` 搬，没有编造），
并在文件 `_note` 里写明"挑选顺序待 A 修，见 issues/013"。

A 改完 `make_sample` 之后，用 `cd collector && python tools/make_events.py --sample`
重出一次即可 —— 那份样例大概率会和 B 临时造的**不一致**（挑选逻辑不同），
这是正常的；重出后 B 跑一遍门禁确认形状没漂就行。

## 为什么值得记

**样例生成器也是"断言绑数据"的一处。** 它按"第一条 found"挑，
数据一换（这次是分类规则换了）就会挑到不该进首屏的类型 ——
而样例是给演示和形状 fixture 用的，它坏了会让**一整套形状断言变成空转**。
挑选顺序应该表达"样例要覆盖哪些分支"，而不是"文件里第一条恰好是谁"。
