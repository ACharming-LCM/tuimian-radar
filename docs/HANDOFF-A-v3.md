# A 的交接（2026-10-18 17:15）—— 漏率压缩进行中

> 一句话：**A1–A17 已完成并提交；A18（分批补抓 + 合并）正在后台跑，现在数据 1444 条，
> 目标是压三校漏率 74% → ≤20%，最后三校复测还没做。**

---

## 一、现在什么在跑

| 项 | 值 |
|---|---|
| 后台任务 | `pwsh-abf224c999c446cc`（`tools/run_batches.py --skip 浙江大学,四川大学`） |
| 日志 | `D:\tuimian\_scratch\a18-rest.log`（实时追加） |
| 各校明细 | `D:\tuimian\_scratch\batch\<学校>.jsonl` 与 `<学校>.log` |
| 已合并 | 浙江大学 212、四川大学 169、上海交通大学 54 + 其余原样 |
| 主文件 | `data/out/notices.jsonl`，此刻 **1444 条**（跑完预计 2000~2800） |

**它每跑完一所学校就自动合并进主文件**（`tools/merge_batch.py`），所以：
- 随时 `Ctrl-C`/杀进程都不会丢已合并的学校；
- 但**我没有每批 commit**，现在工作区里 `data/out/*` 是脏的，需要你收尾时一起提交。

收尾命令（跑完后）：

```powershell
cd D:\tuimian\collector
python tools\make_fixtures.py --as-of 2026-10-18
python tools\make_events.py --daily --write
python tests\test_data_contract.py          # 必须 0 失败
cd ..
git add collector data/out contracts/fixtures
git commit -m "A18: deep-crawl batches, notices 1135 -> N"
```

---

## 二、这一轮到底改了什么（按提交顺序）

| commit | 内容 |
|---|---|
| `9fec5e3` | A16：抓取上限抬到 8 页 / 60 详情，`run_daily` 加 `--max-stale-days 0`（关掉 400 天过滤） |
| `19f87ef` | A17：**崩溃安全**——非法链接不抛异常（`norm_url` 兜住）、单条记录异常不杀整轮、**每站 checkpoint 落盘**、失败主机熔断（同 host 连挂 3 次就跳过）、`run_daily` 改用临时文件捕获（修 64KB 管道死锁） |
| `054f1b8` | A18：分档上限（默认回 4 页/30 详情，只有 6 所深档案学校用 8/60）+ 两校合并验证 |

新增工具：
- `tools/merge_batch.py`——把单校批次按 canonical URL 并进主文件（**批次优先**，其它学校原样保留）
- `tools/run_batches.py`——按学校逐批跑 + 自动合并；`DEEP_SCHOOLS` 控制哪几所开 8/60
- `tests/test_crash_safety.py`——8 条断言，锁住"坏链接/坏记录/中途崩"三种情形

---

## 三、下一步（严格按这个顺序）

### 第 1 步：等分批跑完（预计 40–70 分钟，17:10 起）

```powershell
Get-Content D:\tuimian\_scratch\a18-rest.log -Tail 5 -Encoding utf8
# 跑完后：
(Get-Content D:\tuimian\data\out\notices.jsonl).Count
```

**如果某所学校异常耗时**（单校 >8 分钟）：把 `tools/run_batches.py` 里该校从 `DEEP_SCHOOLS` 拿掉
（就用默认 4 页/30 详情），重跑那一所即可（`--skip` 掉已完成的其他学校）。

### 第 2 步：逐站对账（必须做，别跳）

```powershell
# 与 _scratch/a18-master-before.jsonl（1135 条那份）比
python -c "import json,collections;
before=[json.loads(x) for x in open('_scratch/a18-master-before.jsonl',encoding='utf8')];
after=[json.loads(x) for x in open('data/out/notices.jsonl',encoding='utf8')];
cb=collections.Counter(r['school'] for r in before);ca=collections.Counter(r['school'] for r in after);
print('lost',[(s,cb[s],ca.get(s,0)) for s in cb if ca.get(s,0)<cb[s]])"
```

要求：**没有任何学校条数下降**；每所新增要能对上"翻了第 5–8 页 / 详情从 30 抬到 60 / 关掉 400 天过滤"。

### 第 3 步：三校漏率复测（判据核心）

方法**必须与 A12 完全一致**（脚本还在 `_scratch/a12_final.py`，直接跑）：

```powershell
cd D:\tuimian\collector
python ..\_scratch\a12_final.py          # 输出三校 official_total / have / missing / reasons
```

判据：

- [ ] 三校漏率 **74% → ≤20%**
- [ ] 川大 31 → 接近 300、北大 107 → 接近 212、清华物理 0 → 涨上来
- [ ] **如果仍 >50%：立刻回滚 `sites.json` 的 limits，停下来找决策者**（说明主因不是配额）

### 第 4 步：门禁 + journal + 通知

```powershell
python tests\test_data_contract.py     # 0 失败
```

然后写 `journal/2026-10-18-A18-漏率压缩.md`：单轮耗时（川大 371.6s / 浙大 229.3s）、
逐站对账表、三校漏率前后对比、剩下漏的原因。

再通知 **B**（他等着复跑收口）和 **C**（文档数字要重算：总量、junk、可见数、三校对照）。

---

## 四、必须知道的坑（都踩过，别再踩）

1. **`run_daily.run()` 曾用 `capture_output=True`** → 子进程输出超 64KB 就把自己堵死
   （父等子退出、子等父读管道），表现为"0.5 秒 CPU 挂两小时"。已改临时文件，别再改回去。
2. **`--out` 的测量文件不在主库**：A15 的 169 条在 `_scratch/a15-scu.jsonl`，
   如果找不到"说好的新增"，先看 `--out` 参数。
3. **`--only` 是按 unit 筛的**（"研究生院"），要按学校筛得用 `--school`。
4. **`crawl.py --school X` 写出的文件只含 X**：必须用 `merge_batch.py` 合并，
   否则下一批会把上一批覆盖掉。
5. **`max-stale-days 0` = 不过滤历史公告**；老公告进库后靠 `is_fresh=stale` 排序，不是丢掉。
6. **门禁的 fixtures total** 比的是"可见条数"（`record_quality != junk`），改了数据要重出 fixtures。
7. **events 必须跟着刷**：`make_events.py --daily --write`，日志最后一行会给
   `新增 N / 事件总 M / 无事件 0 / 悬空 X`；**无事件必须为 0**。
8. **GBK 环境**：`run_fast.cmd` / `run_full.cmd` 的 exit 码原来被 `%RC%>>` 吞成文件描述符，
   已改 `%RC% >>`；下次定时任务起 `data/logs/fast.log` 才会有 `exit=0`。

---

## 五、当前数据现状（供参考）

| 指标 | 值 |
|---|---:|
| 总记录 | **1444**（跑批中，还在涨） |
| 前 8 校 | 浙大 212 / 北大 179 / 川大 169 / 南大 80 / 中南 79 / 郑大 71 / 吉大 69 / 上交 54 |
| 基线（本轮开始时） | 1135 |
| junk / ok 分布 | 合并后未重算，收尾时跑一次 `tools/backfill_derived.py --write` 再报 |

字段口径（A1–A14 建立，别改语义）：
`record_quality`(junk/thin/ok) + `quality_hits`、`topic`+`topic_hits`、
`is_fresh`(true/false/"stale"/null) + `age_days` + `admission_year`、
`title_source`(detail/fallback/fallback_body/none)、`publish_date_source`、`deadline_source`(manual)。

---

## 六、我没做完/没做的

- **三校漏率复测没做**（这是判据核心，必须做）。
- 分批只跑了 3 所就交接（浙大、川大、上交），其余 26 所在后台。
- A11 那 17 条扫描版附件只人工读了第一页（4 条标 `not_read`/局限），没有 OCR。
- A12 里"官网应有"仍是"我们能发现的栏目 8 页内"的下限，不等于学校真实全量。
