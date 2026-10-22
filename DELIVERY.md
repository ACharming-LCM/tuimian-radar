# 交付说明（2026-10-20）

> **口径基线**：数据快照 **2026-10-20 16:43** · **2,646 条** · 门禁 **233 通过 / 0 失败**。
> 演示视频 2 分 03 秒。4 所学校本轮抓取失败，数据沿用上一轮，新鲜度页已标出。

> 给队友和评审看的一页：项目现在在哪、做了什么、链接是什么、还剩什么。

## 一、链接

| 用途 | 链接 |
|---|---|
| 在线演示（直接打开） | https://lcmcharming-tuimian-radar.ms.show/ |
| 魔搭创空间 | https://www.modelscope.cn/studios/LCMcharming/tuimian-radar |
| GitHub 仓库（公开） | https://github.com/ACharming-LCM/tuimian-radar |
| BP（PDF） | [BP.pdf](BP.pdf) |
| 演示视频（2 分 03 秒，中文旁白） | [demo/demo.mp4](demo/demo.mp4) |
| **5 分钟路演 HTML（推荐现场用）** | [demo/推免雷达-路演-5分钟.html](demo/推免雷达-路演-5分钟.html) |
| 5 分钟路演打印版 PDF | [demo/推免雷达-路演-5分钟-打印版.pdf](demo/推免雷达-路演-5分钟-打印版.pdf) |
| 截图版演示（浏览器打开） | [demo/demo.html](demo/demo.html) |
| 现场演示脚本 | [DEMO.md](DEMO.md) |
| 部署说明 | [DEPLOY.md](DEPLOY.md) |

线上环境：魔搭创空间，Docker 类型，`platform/2v-cpu-8g-mem`，监听 `0.0.0.0:7860`。
数据：2,646 条公告 / 557 位导师 / 29 所学校 / 37 个站点；快照生成于 **2026-10-20 16:43**。

---

## 二、这次做了什么（2026-10-19 ~ 10-20）

### 1. 安全与公网部署

- 管理口令不再写死 `demo-admin`，`/api/health` 不再返回口令；未设环境变量时每次启动随机生成。
- 新增公网演示模式（`TUIMIAN_PUBLIC=1`）：
  - 演示身份（`u_demo` / `u_num_demo` / `u_demo_sub`）**只读**；
  - 访客自动分配到 `guest_xxx` 身份，互不干扰；
  - 论坛只读（发帖/举报/申诉返回 403）；
  - 实测：改演示身份 403，改访客身份 200，发帖 403。
- 请求体上限 256 KB；画像/帖子长度上限；爬虫默认恢复 TLS 证书校验（临时放宽需显式 `TUIMIAN_TLS_INSECURE=1`）。
- 前端 17 个 JS 文件逐处审计 `innerHTML` / `href`，所有外部数据均经过 `esc()` / `safeUrl()`。

### 2. 数据刷新与质量

- 增量抓取从 2,561 条刷新到 **2,646 条**（新增 114 条 URL，详情复用 1,133 条）。
- 修复「未刷新站点记录被误判删除」的沿用逻辑（按站点比 URL，不再全局比）。
- 修复事件流覆盖口径：`make_id` 依赖 school+unit，换栏目会换 id；现在按 URL 也算覆盖，
  无事件公告从 14 条降到 **0**，2 条悬空事件已记账（`events_orphans.json`）。
- 修复容器 UTC 与数据 +08 时区差导致的「-7.7 小时前」：时区固定 `Asia/Shanghai`，年龄计算下限 0。
- 重新回填派生字段（`record_quality` / `topic` / `notice_kind`），首屏不再混入目录/简章。
- 脱敏脚本加强：829 条名单/人员表正文整段隐去、4,243 行人员表格删除、182 个手机号打码、
  57 个导师电话清空；自检 `left_after_check = 0`。

### 3. 测试与工程

- 契约门禁从 226 条扩到 **233 条**，本地 Windows：**233 通过 / 0 失败**。
- 20 个 collector 测试脚本全部通过。
- 新增 CI（`.github/workflows/ci.yml`）：GitHub Actions 上跑门禁 + 脱敏自检。
  - **最新 run 绿**：Ubuntu runner 233/0；本地 Windows 连跑多次 233/0。
  - 之前那条红是浮点断言写成严格相等（`0.43945` 边界），已改为 `1e-4` 容差。
- 新增 `requirements-dev.txt`（补 `jsonschema` / `httpx2`）、MIT `LICENSE`。
- 从公开仓库全新克隆验证：自动使用 `data/sample/out`，`run.py --check` 与门禁均正常。

### 4. 交付材料

- `BP.md` / `BP.pdf`：商业计划书（问题、产品、技术、市场、竞争、商业模式、合规、路线图、风险）。
- `demo/demo.mp4`：2 分 03 秒，1920×1080，中文旁白 + 字幕；11 页。
- `demo/demo.html`：浏览器直接播放的截图版演示。
- `demo/推免雷达-路演-5分钟.html`：10 页、5 分钟路演版（HTML，双击打开，按 F5 放映；
  每页带讲稿，正文 ≥20px）。打印版 PDF 同目录。结构：需求与付费 → 痛点 → 产品链路 →
  三态 → 为什么推给你 → 导师/套磁信 → 创新点 → 商业 → 收尾；**删掉了“踩坑/边界”章节**。
- `DEMO.md`：七步现场演示脚本、备用话术、常见翻车点。
- `DEPLOY.md`：魔搭 / Docker 部署步骤。
- 线上已部署并通过自检。

---

## 三、关键数字（对外口径）

```
公告入库        2646 条（29 所学校 / 37 个站点 / 34 个院系单位）
默认展示        2493 条（153 条 junk 拒收）
notice_kind     announcement 536 / catalog 293 / emba 56 / other 1761
截止日三态      found 749 / absent 1199 / unknown 698
截止日未过期    40 条
导师            557 位（327 位有邮箱，379 位有研究方向）
事件流          2660 条（无事件 0；悬空 2 条为 id 变更）
契约门禁        233 通过 / 0 失败
```

对外只说三句话：

1. 「2646 条入库 → 2493 条默认展示 → 其中今天真正能行动的是 40 条。」
2. 「536 条是招生通知；293 条目录/简章收进参考资料，不占首屏。」
3. 「1199 条学校确实没写截止日，698 条是我们没读到 —— 两者绝不混用。」

---

## 四、本地怎么跑

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py            # http://127.0.0.1:8000
.venv\Scripts\python.exe run.py --check    # 只体检
```

```powershell
cd server
..\.venv\Scripts\python.exe tests/run.py   # 期望 233 通过 / 0 失败
```

新克隆自带脱敏数据 `data/sample/out`，服务端在 `data/out` 不存在时会自动使用它。
要抓新数据：

```powershell
cd collector
$env:PYTHONIOENCODING='utf-8'
..\.venv\Scripts\python.exe crawl.py --fast
..\.venv\Scripts\python.exe tools/make_events.py --daily --write
..\.venv\Scripts\python.exe tools/backfill_derived.py --write
..\.venv\Scripts\python.exe tools/sanitize_for_deploy.py --src ..\data\out --dst ..\data\sample\out
```

---

## 五、已知边界 / 还没做的

- 本轮有 **4 所学校抓取失败**（北京大学研招网、中科大研究生院、复旦研招网、厦大研究生院），
  数据沿用上一轮，新鲜度页会把它们标成失败；其余 25 个站点正常。
- 其余 26 所学校**尚未做外部漏检对照**，只有北大 / 川大 / 清华物理三校（漏率 74.1% → 17.4%）。
- 上海交大详情页是 JS 渲染；扫描版 PDF 没有 OCR；微信公众号无开放接口。
- 支付是模拟的（开通码）；通知只有站内红点，没有邮件 / 微信。
- 单 worker + JSON 状态文件，暂不支持大规模并发。
- 不预测录取概率、不做选校推荐、没有历年分数线。
- **真实用户试用尚未执行**：`docs/USER-TEST.md` 的 15 分钟流程已写好，缺 3 位真保研生。
  这是当前最需要补的证据，不是功能。
- **校园试点落点**（BP §11.2）：浙大物理 / 材料学院大三下约 200 人；社区渠道约 3–5 位真实用户；
  成功指标是「60 秒内找到截止日 / 主动回访 / 愿意为提醒付费」。
- ¥29/月是**待验证假设**，不是定价结论；模拟开通不算营收。

---

## 六、安全提醒

魔搭部署用的访问令牌只保存在本地 `.ms_token`（不在仓库里，也不在压缩包里）。
部署已经完成，建议到 https://modelscope.cn/my/myaccesstoken 撤销该令牌，然后删除本地文件。

---

## 七、给队友的最短路径

1. 打开在线演示：https://lcmcharming-tuimian-radar.ms.show/
2. 看 BP：`BP.pdf`
3. 看演示视频：`demo/demo.mp4`
4. 要改代码：`git clone https://github.com/ACharming-LCM/tuimian-radar.git`
5. 要重新部署：按 `DEPLOY.md`，记得 Docker 类型、`ENTRYPOINT`、端口 7860。
