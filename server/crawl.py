# -*- coding: utf-8 -*-
"""抓取调度与运行记录。契约见 `contracts/api.md` 第 12 节。

## 为什么这里是"提交 + 轮询"而不是同步接口

真抓一轮 37 所学校是分钟级，还会**打对方学校的服务器**。
放在请求里同步跑，HTTP 会超时、演示界面会卡死。

## 单飞（single-flight）不是性能优化，是责任

并发跑两轮不只是浪费：**是对别人不礼貌**（同一批站点被我们打两遍），
而且 `crawl.py` 是"读旧文件 → 覆盖写"的，两轮并发会互相踩。
所以同时触发只有一次真的跑，其余拿到**同一个 run_id**。

调度器 + 手动同时触发是**正常情况**（演示时你会手动点一下），
所以 join 返回 202 而不是 409 —— 报错会让调用方以为出了问题。

## 不解析 A 的 md 报告

`data/out/runs/<日期>.md` 是**对人写的文档**，格式说改就改。
解析它等于把接口绑在别人的排版上。所以这里只报**子进程自己的事实**：
退出码、起止时间、输出尾部。每步明细（`steps`）恒为 `null`，
要的话需要 A 产一份机器可读的运行记录（形状见契约）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTOR_DIR = os.path.join(ROOT, "collector")

# 抓取的默认命令。跑 `collector/tools/run_daily.py`（**cwd 必须是 collector/**，
# 它内部用相对路径调 crawl.py 等）。可用 `TUIMIAN_CRAWL_CMD` 覆盖 ——
# 测试就是靠这个注入一个假的命令，绝不在门禁里真去抓 37 所学校。
DEFAULT_CRAWL_ARGS = ["tools/run_daily.py"]

# 一次抓取最多允许多久（run_daily 内部每步自己也有 timeout=3600）
CRAWL_TIMEOUT_S = int(os.environ.get("TUIMIAN_CRAWL_TIMEOUT_S", "7200"))

# 运行日志留几条
MAX_RUNS = 20

# 定时器：默认一天一轮（和管道节拍一致）
DEFAULT_INTERVAL_HOURS = float(os.environ.get("TUIMIAN_CRAWL_INTERVAL_HOURS", "24"))
# **启动时不抓**：第一次检查延后这么久。
# 演示时服务器一起来就狂抓很难看，而且刚跑完的数据根本不需要再抓。
DEFAULT_FIRST_DELAY_MIN = float(os.environ.get("TUIMIAN_CRAWL_FIRST_DELAY_MIN", "5"))
# 调度开关。测试和"不想有惊喜抓取"的演示都靠它关掉。
SCHEDULE_ENABLED = os.environ.get("TUIMIAN_CRAWL_SCHEDULE", "1") not in ("0", "false", "")


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _parse_dt(s):
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


class CrawlRunner:
    """单飞 + 子进程 + 运行日志。

    `command` 可注入（测试用假命令）。`state_path` 是运行日志的落盘位置，
    和帖子/订阅同一层（`server/var/`），**不写 `data/out/`** —— 那是 A 的产出目录。
    """

    def __init__(self, command=None, cwd=None, state_path=None, timeout_s=None,
                 freshness_path=None, schedule_enabled=None,
                 interval_hours=None, first_delay_min=None):
        self.command = list(command or [sys.executable, *DEFAULT_CRAWL_ARGS])
        self.cwd = cwd or COLLECTOR_DIR
        self.state_path = state_path
        self.timeout_s = timeout_s or CRAWL_TIMEOUT_S
        self.freshness_path = freshness_path
        self.interval_hours = (DEFAULT_INTERVAL_HOURS if interval_hours is None
                               else interval_hours)
        self.first_delay_min = (DEFAULT_FIRST_DELAY_MIN if first_delay_min is None
                                else first_delay_min)
        self.schedule_enabled = (SCHEDULE_ENABLED if schedule_enabled is None
                                 else schedule_enabled)

        self._lock = threading.Lock()
        self._current = None            # 正在跑的那个 run dict
        self._runs = []                 # 最近几次（新的在前）
        self._thread = None
        self._sched = None
        self._sched_stop = threading.Event()
        self._load()

    # ---------- 运行日志（落盘） ----------

    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            runs = d.get("runs") or []
        except (json.JSONDecodeError, OSError):
            # 坏了不该把服务打挂。备份一份，从头开始 —— 别静默丢数据
            broken = f"{self.state_path}.broken-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            try:
                os.replace(self.state_path, broken)
            except OSError:
                pass
            return
        # **上一次进程留下的 `running` 一定是假的** —— 进程没了，那个 run 不可能还在跑。
        # 不修正的话，界面会永远显示"正在抓取"，而实际上什么都没在跑。
        for r in runs:
            if r.get("status") == "running":
                r["status"] = "interrupted"
                r["error"] = "服务在抓取过程中重启了，这次运行的结果未知"
                r["finished_at"] = r.get("finished_at") or _now_iso()
        self._runs = runs[:MAX_RUNS]

    def _save(self):
        if not self.state_path:
            return
        d = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(d, exist_ok=True)
        tmp = f"{self.state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"runs": self._runs[:MAX_RUNS]}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)          # 原子替换，不留半个文件

    def _record(self, run):
        with self._lock:
            self._runs = [r for r in self._runs if r.get("run_id") != run["run_id"]]
            self._runs.insert(0, run)
            del self._runs[MAX_RUNS:]
            self._save()

    # ---------- 对外 ----------

    @staticmethod
    def public_view(run):
        """**对外只给这些键**（白名单）。加字段要显式改这里。"""
        if run is None:
            return None
        return {k: run.get(k) for k in (
            "run_id", "status", "trigger", "joined_existing",
            "started_at", "finished_at", "duration_s", "exit_code",
            "command", "tail", "steps", "error",
            "data_generated_before", "data_generated_after", "data_moved",
            "_note")}

    def runs(self, limit=MAX_RUNS):
        with self._lock:
            return [self.public_view(r) for r in self._runs[:limit]]

    def get(self, run_id):
        with self._lock:
            for r in self._runs:
                if r.get("run_id") == run_id:
                    return self.public_view(r)
            if self._current and self._current.get("run_id") == run_id:
                return self.public_view(self._current)
        return None

    @property
    def current(self):
        with self._lock:
            return self.public_view(self._current)

    # ---------- 触发 ----------

    def trigger(self, trigger="manual"):
        """单飞入口。返回 `(run_view, joined_existing)`。

        **已经在跑就把那个 run 交出去** —— 不排队、不报错、不起第二个进程。
        """
        with self._lock:
            if self._current is not None and self._current.get("status") == "running":
                return self.public_view(self._current), True

        run = {
            "run_id": "run_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_"
                      + uuid.uuid4().hex[:4],
            "status": "running",
            "trigger": trigger,
            "joined_existing": False,
            "started_at": _now_iso(),
            "finished_at": None,
            "duration_s": None,
            "exit_code": None,
            "command": self.command,
            "tail": [],
            # **恒为 None。** 每步明细在 A 的 md 报告里，B 不解析它（见模块 docstring）。
            "steps": None,
            "error": None,
            # 跑完后填：数据里的 `generated_at` 有没有动（见 `_run`）
            "data_generated_before": None,
            "data_generated_after": None,
            "data_moved": None,
            "_note": "真实抓取在子进程里跑。steps 恒为 null —— 接口层不解析 "
                     "A 的 md 运行报告；要每步明细需要机器可读的运行记录（见 api.md 第 12 节）。",
        }
        with self._lock:
            self._current = run
        self._record(run)

        self._thread = threading.Thread(target=self._run, args=(run,), daemon=True)
        self._thread.start()
        return self.public_view(run), False

    def _run(self, run):
        """在子进程里真跑。这段在后台线程里，不阻塞接口。"""
        t0 = time.time()
        before = self._data_generated_at()
        exit_code, tail, err = None, [], None
        try:
            r = subprocess.run(self.command, cwd=self.cwd, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=self.timeout_s)
            exit_code = r.returncode
            # 只留输出尾部几行：**报 failed 却不给原因等于没报**，
            # 但也不能把几分钟的日志全塞进响应
            tail = [ln for ln in (r.stdout or "").strip().splitlines()[-8:]]
            if r.returncode != 0 and (r.stderr or "").strip():
                tail += ["--- stderr ---"] + [
                    ln for ln in (r.stderr or "").strip().splitlines()[-6:]]
        except subprocess.TimeoutExpired:
            exit_code, err = -1, f"抓取超过 {self.timeout_s}s 没结束，已中止"
        except FileNotFoundError as e:
            exit_code, err = -2, f"找不到抓取入口：{e}"
        except Exception as e:                          # noqa: BLE001
            exit_code, err = -3, f"{type(e).__name__}: {e}"

        after = self._data_generated_at()
        # 拿不到时间戳时是 **None（不知道）**，不是 False（没动）。
        # `bool(None and ...)` 会算成 False —— 那等于把"不知道"说成"没动"，
        # 和本项目"不许把未知说成已知"的原则冲突。门禁里有一条专门盯它。
        moved = None if not (before and after) else (before != after)
        run.update({
            "status": "ok" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "finished_at": _now_iso(),
            "duration_s": round(time.time() - t0, 1),
            "tail": tail,
            # ---- 统计：**数据到底动没动** ----
            # 退出码 0 但数据没变，是另一种失败（子进程"成功"了什么也没产出）。
            # 不能直接把它判成 failed（中途失败时 freshness.json 本来就不会重写），
            # 但**两个时间戳都报出来**，看的人一眼就知道这次抓取有没有用。
            "data_generated_before": before,
            "data_generated_after": after,
            "data_moved": moved,
        })
        if err:
            run["error"] = err
        elif exit_code != 0:
            # 退出码非 0 但拿不到 stderr 时，至少说清"子进程以 N 退出"
            run["error"] = f"run_daily.py 以退出码 {exit_code} 结束（原因见 tail）"
        elif exit_code == 0 and moved is False:
            run["error"] = ("子进程退出码 0，但 freshness.json 的 generated_at 没变 —— "
                            "这一轮可能什么都没抓到，建议看一眼 tail")
        with self._lock:
            self._current = None
        self._record(run)

    def _data_generated_at(self):
        """读 freshness.json 的 `generated_at`。读不到返回 None（不猜）。"""
        if not self.freshness_path or not os.path.exists(self.freshness_path):
            return None
        try:
            with open(self.freshness_path, encoding="utf-8") as f:
                return json.load(f).get("generated_at")
        except (json.JSONDecodeError, OSError):
            return None

    # ---------- 定时 ----------

    def start_scheduler(self):
        """起进程内定时器。**不在启动时立刻抓。**"""
        if not self.schedule_enabled:
            return False
        if self._sched is not None and self._sched.is_alive():
            return False
        self._sched_stop.clear()
        self._sched = threading.Thread(target=self._schedule_loop, daemon=True)
        self._sched.start()
        return True

    def stop_scheduler(self):
        self._sched_stop.set()
        if self._sched is not None:
            self._sched.join(timeout=3)
        self._sched = None

    def _latest_last_ok(self):
        """从新鲜度数据里取最新的 `last_ok`。取不到返回 None。"""
        if not self.freshness_path or not os.path.exists(self.freshness_path):
            return None
        try:
            with open(self.freshness_path, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        stamps = [_parse_dt(s.get("last_ok")) for s in (d.get("schools") or [])
                  if isinstance(s, dict)]
        stamps = [s for s in stamps if s is not None]
        return max(stamps) if stamps else None

    def due(self, now=None):
        """该不该跑。返回 `(should_run, reason)`。

        **判断依据永远是数据本身，不是计时器** —— 定时器只负责"提醒检查"。
        一个频繁重启的服务，计时器永远攒不到点，但数据真的旧了就该跑。
        """
        now = now or datetime.now()
        latest = self._latest_last_ok()
        if latest is None:
            # 从没跑过 → 该跑。但**不是启动后立刻**（第一次检查本来就有延迟）
            return True, "还没有新鲜度数据（从没跑过）"
        age = (now - latest).total_seconds() / 3600.0
        if age >= self.interval_hours:
            return True, f"最新一次抓取在 {age:.1f} 小时前（节拍 {self.interval_hours}h）"
        return False, f"最新一次抓取在 {age:.1f} 小时前，还没到节拍（{self.interval_hours}h）"

    def _schedule_loop(self):
        # 第一次检查延后 —— **启动时不抓**
        if self._sched_stop.wait(self.first_delay_min * 60):
            return
        while not self._sched_stop.is_set():
            try:
                should, reason = self.due()
                if should:
                    self.trigger(trigger="schedule")
                else:
                    # 跳过也要留痕 —— 不然看不出"定时器是活的但判断为还新"
                    self._record({
                        "run_id": "skip_" + datetime.now().strftime("%Y%m%d_%H%M%S"),
                        "status": "skipped_fresh", "trigger": "schedule",
                        "joined_existing": False, "started_at": _now_iso(),
                        "finished_at": _now_iso(), "duration_s": 0.0,
                        "exit_code": None, "command": None, "tail": [reason],
                        "steps": None, "error": None,
                        "_note": "定时器到点了，但数据还新，没抓。",
                    })
            except Exception:                              # noqa: BLE001
                pass          # 调度线程绝不能因为一次异常就死掉
            if self._sched_stop.wait(self.interval_hours * 3600):
                return
