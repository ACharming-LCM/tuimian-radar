# 010 修了标题之后没有重算 `relevance`：9 条记录的排序键和现算规则不一致

- 提出：B（服务层）2026-10-18 12:05
- 状态：**✅ 已解决**（A13 `7afbc17` 把 `relevance` 纳入 `backfill_derived.py`，
  并在 `run_daily` 收尾加"派生字段刷新"这一步；实测不一致 **9 → 0**）
- 影响：数据侧（`collector/`）；接口层不改代码
- 相关：`collector/fields.py:relevance_of`、`contracts/api.md` §1 排序、门禁 `排序_publish_desc_是两级降序`

## 现象

`data/out/notices.jsonl` 里有 **9 条**记录的 `relevance` 与按**现在**的规则算出来的不一致：

```
存盘 relevance 与现算不一致：9 / 1120
  天津大学 6 条（stored 2 → now 1 或 0）
  北京大学 3 条（stored 2 → now 1）
```

样例：

| 学校 | 标题 | 存盘 | 现算 |
|---|---|---|---|
| 天津大学 | 关于有研究生学籍考生及以同等学力报名考生提供证明材料的通知 | 2 | 1 |
| 天津大学 | 天津大学2027年部分考生需提供学历或学籍认证报告等材料的通知 | 2 | **0** |
| 北京大学 | 地球与空间科学学院 | 2 | 1 |

## 为什么会这样（时序，不是谁写错了）

`relevance` 是**落盘时**算的（`crawl.py::_assemble` 里调 `fields.relevance_of`）。
A6 这轮修的是**标题**（天津那批从站点名换回真标题），修完没有回头重算 `relevance` ——
于是记录里留着一个"当时算的"分数，而规则看到的是新标题。

`retain_unrefreshed` 会把没重抓的记录原样带过来，所以这条旧分数会一直带下去。

## 为什么必须修（它不只是个数字不对）

**`relevance` 是默认排序 `publish_desc` 的第一级键**（`store._sort_key_publish_desc`
排序键是 `(relevance, publish_date)` 两级降序）。所以这 9 条现在**排在错误的位置**：

- 一条现在是 0 分的记录，仍在按 2 分排序 → 它挤在首屏前面；
- 用户看到的是"一条跟招生无关的公告排在很前面"，而我们没有报错。

顺带说：**这正是 B5 那条数据健康门禁想要抓的形状** —— 数据本身不自洽，
而接口形状完全正常。抓到它的是门禁里那条老用例（它拿参考实现现算 `relevance` 再和接口比），
不是我新加的那八条。

## 请 A 做的（两个选择）

1. **重算一遍**（推荐）：跑一次能刷新 `relevance` 的步骤 ——
   对 1120 条重算 `fields.relevance_of(title, board, apply_mode)` 并写回。
   实测只要 9 条会变，改动面极小。
2. **或者在 `run_daily` 的收尾加一步"派生字段刷新"**，把 `relevance` 也纳入
   （`backfill_derived.py` 已经在刷别的派生字段了，看一眼能不能顺手带上）。

修完之后：

```powershell
D:\python.3.10.11\python.exe server\tests\run.py   # 应回到 183/0
```

## 我这一侧没有做的事（说明一下，免得你以为已经兜住了）

- **没有**在接口层现算 `relevance`。那会让"数据层判定"和"接口层判定"各有一份，
  而这个项目已经在同一个坑里栽过四次（`relevance` 排序、`warnings` 措辞、
  `group_kind`、`LIST_FIELDS`）。
- **没有**把这条用例放宽成"允许少量不一致"。它现在红是对的 ——
  放了它，下一次就没人知道首屏为什么乱。

## 复算命令

```powershell
$env:PYTHONIOENCODING="utf-8"; Set-Location D:\tuimian

D:\python.3.10.11\python.exe -c "import sys,json,importlib.util; sys.path.insert(0,'collector'); spec=importlib.util.spec_from_file_location('f','collector/fields.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); recs=[json.loads(l) for l in open('data/out/notices.jsonl',encoding='utf-8') if l.strip()]; d=[r for r in recs if m.relevance_of(r.get('title'),r.get('board'),r.get('apply_mode'))!=r.get('relevance')]; print(len(d),'条不一致'); [print(' ',r['school'],(r.get('title') or '')[:30],r.get('relevance'),'->',m.relevance_of(r.get('title'),r.get('board'),r.get('apply_mode'))) for r in d]"
```
