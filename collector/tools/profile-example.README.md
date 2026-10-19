# 画像文件怎么写

对应 `profile-example.json`（`docs/matching-spec.md` 第 2 节是字段定义，这里是给人看的说明）。

## 为什么说明文字不写在 JSON 里

这个文件原来第一个键是 `_comment`，一段一百多字的说明。问题在于：
**匹配接口会把 `profile` 原样回显**，于是这段说明进了契约样例
（`contracts/fixtures/match.response.json`），后果有两个：

1. 每个 `/api/match` 响应都白扛一遍这段文字；
2. C 照 fixture 写 TypeScript 类型时，`_comment` 会变成画像的正式字段。

所以说明移到这里。**规则：任何 `_` 开头的键都非契约，不进响应**（见 `contracts/api.md`）。
以后再加 `_note` / `_todo` 也不会漏进去。

## 字段怎么填

| 字段 | 说明 |
|---|---|
| `majors` | **请把同义词写全**，例如物理方向写 `["物理学","力学","应用物理","光学"]`。代码里**不维护同义词表** —— 那张表会烂掉，而用户最清楚自己算哪个学科 |
| `interest_keywords` | 导师匹配用（`matching-spec` 第 7 节）。不填会**退化为用专业名粗匹配**，并在结果里明确警告，不静默降级 |
| `wanted_modes` | 硬过滤依据。统考类公告对推免生是纯噪声，写进这里就会被丢掉 |
| `target_entry_year` | 决定"是今年的机会还是明年的" |
| `english` | `cet6` 填了，四级自动视为满足（不再单独提醒"要求四级"） |
| `exclude_expired` | 为 `true` 时丢掉已过截止日的公告 |

## 怎么用

```bash
cd collector
python tools/match_preview.py --profile tools/profile-example.json   # 公告匹配
python tools/match_preview.py --faculty --profile tools/profile-example.json  # 导师匹配
```

**文件必须存成 UTF-8。** Windows 记事本和 PowerShell 的 `Set-Content -Encoding utf8`
会加 BOM —— 读取端已经用 `utf-8-sig` 容错，但别的地方（比如 B 的 server）读到 BOM 会报错。
