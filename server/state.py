# -*- coding: utf-8 -*-
"""服务端运行时状态：用户、订阅、免费额度、论坛帖子。

**为什么需要一层自己的存储：**
前四步都是"读 A 的产出、算完返回"，纯只读。第 6/7 步不一样 ——
帖子要能发、举报要留痕、订阅要能改、免费额度要能累加，这些都得**写**。
但 `data/out/` 是 A 的产出目录，B 一个字节都不该往里写（会污染 A 的管道产物）。

所以：**运行时状态落在 `server/var/state.json`**，和 `data/` 完全分开。
- 路径可用 `TUIMIAN_STATE` 覆盖（测试就是靠这个做隔离的）
- `server/var/` 在 `.gitignore` 里：用户发的帖子不该进版本库
- 单文件 JSON + 原子替换（写临时文件再 `os.replace`），不做数据库：
  演示级并发量，上数据库是把简单问题复杂化

**边界说明**：这是 demo 的存储，不是生产。没有多进程锁 ——
`uvicorn --workers 4` 下并发写会丢更新。演示用单 worker 起，这条写在
`server/app.py` 的启动注释里。真要上生产，这里得换成 SQLite。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import date, datetime

DEFAULT_STATE = {
    "version": 1,
    "users": {},
    "posts": [],
    "orders": [],
    # 关注规则（契约 api.md 第 11 节）。和帖子/订阅同一个文件、同一套原子替换 ——
    # 不新造一套存储，也不写 data/out/（那是 A 的产出目录）。
    "watches": [],
    # 审核审计日志（契约 api.md 第 13 节）。
    #
    # **单独一份，不混进帖子记录里。** 帖子可能被删，审计不能跟着消失 ——
    # 它是"我说得出依据"的唯一凭据。每条自带**当时的原文快照**，
    # 所以即使 `posts` 整个被清空，审计里仍然能看到当时下架的是什么内容。
    #
    # 只追加。**StateStore 上没有任何修改/删除审计的方法**，
    # 接口层也不提供 —— 这是结构上保证 append-only，不是靠自觉。
    "audit": [],
    # 用户画像（契约 api.md 第 16 节）。**只存"用户填了什么"，不存匹配结果** ——
    # 匹配每次现算，存下来就会和事件源变成两份真相，然后其中一份先烂掉。
    "profiles": {},
    # 推送记录（去重 + 限频）。**只记 push_id / 日期 / event_id，不存推送正文** ——
    # 正文每次从事件现算；存下来会和 events.jsonl 脱节，而脱节之后
    # "这条推送当时说的是什么"就查不清了（审计要能用同一个理由解释它）。
    "pushes": [],
    "counters": {"post": 0, "audit": 0, "appeal": 0},
}


def _today_str():
    return date.today().isoformat()


def _new_state():
    # 深拷贝：不能把 DEFAULT_STATE 本身交出去，多个实例会互相改
    return json.loads(json.dumps(DEFAULT_STATE))


# ---------------------------------------------------------------------------
# 首次运行：自动建一份**干净的演示状态**（B26，2026-10-19）
# ---------------------------------------------------------------------------
#
# 事故（交接时发现的）：`server/var/` 在 `.gitignore` 里，新克隆的仓库没有
# `state.json`。门禁里有几条用例直接 `shutil.copyfile(server/var/state.json, tmp)`，
# 于是**第一次跑门禁就红 5 条**，接手的队友会以为项目是坏的。
#
# 两个决定：
# 1. **缺失时自动建**，不是"报错让人手建" —— 第一次跑就该能用；
# 2. 建出来的是**演示状态**，不是空壳、更不是把开发机上的 118 个测试用户抄过去。
#    空壳也不够：`u_demo` / `u_num_demo` 是演示和门禁性能用例依赖的两个身份。

DEMO_USER_IDS = ("u_demo", "u_num_demo")

# 演示画像：和 `u_num_demo` 现在那份一致（推免 + 直博 + 物理）。
# 改它等于改"演示时画像长什么样"，先跟 C 的演示脚本对一下。
DEMO_PROFILE = {
    "school": "浙江大学",
    "grade": "大三",
    "majors": ["物理学"],
    "preferred_schools": ["中山大学", "浙江大学", "电子科技大学", "北京大学"],
    "interest_keywords": ["物理", "材料"],
    "target_levels": ["直博"],
    "wanted_modes": ["推免"],
    # 目标入学年：推送用它挡掉「2025 级…选拔」这类往届通知。
    "target_entry_year": 2027,
    "gpa_rank": {"rank": 5, "total": 120},
    "english": {"cet6": 580},
    "research": ["CUPT 国赛一等奖"],
}


def _demo_user(user_id, nickname, subscribed=False):
    return {
        "id": user_id,
        "nickname": nickname,
        "subscribed": subscribed,
        # `expires_at=None` + subscribed=True → `is_subscribed()` 判为有效（没有到期日）。
        # 预置一个写死的到期日，过几个月就自己失效了 —— 那样新克隆"过一阵就坏"。
        "expires_at": None,
        "daily_free_quota": 3,
        "daily_used": 0,
        "used_date": _today_str(),
    }


def demo_state():
    """首次运行写进 `server/var/state.json` 的最小可用状态。

    结构九个键一个不少（`users` / `profiles` / `posts` / `watches` / `pushes` /
    `orders` / `audit` / `counters` / `version`），预置两个演示身份：

    - `u_demo`：免费身份（付费墙演示用）；
    - `u_num_demo`：**已订阅 + 完整画像**（推送/匹配/套磁信的演示身份，
      门禁的性能用例也依赖它）。
    """
    st = _new_state()
    st["users"]["u_demo"] = _demo_user("u_demo", "示例同学")
    st["users"]["u_num_demo"] = _demo_user("u_num_demo", "示例同学（完整画像）",
                                           subscribed=True)
    st["profiles"]["u_num_demo"] = {
        "profile": json.loads(json.dumps(DEMO_PROFILE)),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return st


# 顶层键的**期望类型**。状态文件是磁盘上的 JSON，而磁盘上的东西会坏：
# 半截文件、被别的程序改过、手工编辑时少一个引号。类型不对时**不能**硬塞进
# `DEFAULT_STATE` 的补全逻辑 —— 那会让 `state["users"]` 变成字符串，
# 然后每个 `/api/me` 都 500，而错误现场（磁盘上那份文件）已经被覆盖掉了。
_TOP_TYPES = {
    "users": dict, "posts": list, "orders": list,
    "watches": list, "audit": list, "counters": dict,
    "profiles": dict, "pushes": list,
}


class StateStore:
    """JSON 文件状态。所有写操作都走 `save()`，原子替换。

    ## 三件与鲁棒性有关的事（环 4）

    1. **写操作串行化**：`self._lock` 是 RLock，`save()` 和"改状态"的公开方法
       都在它里面。原来只锁了 `save()` 里的 `json.dump`，而**改状态发生在锁外** ——
       于是 A 线程 dump 到一半、B 线程往 `state["users"]` 里塞一个键，
       就会 `RuntimeError: dictionary changed size during iteration`，
       表现成"偶发 500"，而现场只有一个 `.tmp` 文件。
       （我压了 60 个并发写没复现 —— 窗口窄不等于没有，这类 bug 只有
       结构上不成立才算修好。）
    2. **坏了要备份、要响**：文件读不出来/形状不对时**不拒绝启动**（演示现场
       一个字节坏了不该让整个站打不开），但**必须留下三样东西**：
       原文件备份、`self.recovery`（给 `/api/health`）、以及一句人话说明
       "帖子/审计都没有恢复" —— 静默重建一个空 state 等于**审计痕迹全丢**，
       而界面看起来像"新站"。
    3. **结构不对也要拦**：合法 JSON 但不是对象（`[]`）、或者 `users` 是字符串，
       原来分别是 `AttributeError: 'list' object has no attribute 'setdefault'`
       （服务直接起不来）和"每个 /api/me 都 500"。现在都按"损坏"处理。
    """

    def __init__(self, path, state):
        self.path = path
        self.state = state
        # RLock：`save()` 会被已经持锁的公开方法调用（可重入）
        self._lock = threading.RLock()
        # 状态文件的"体检结果"。`None` = 干净；否则见 `load()`。
        # `/api/health` 会把它原样给出去 —— **静默重建是不能接受的**。
        self.recovery = None

    # ---------- 加载 / 保存 ----------

    @classmethod
    def load(cls, path):
        if not path or path == ":memory:" or not os.path.exists(path):
            return cls(path, _new_state())

        state, recovery = cls._read_checked(path)
        if state is None:
            # **损坏**：备份 + 空状态 + 记录发生了什么。不抛异常（不拒绝启动）。
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            broken = f"{path}.broken-{stamp}"
            backup = None
            try:
                os.replace(path, broken)          # 原文件**不改不删**，只改名
                backup = broken
            except OSError as e:
                recovery["detail"] += f"；备份也失败了（{e}）—— 原文件可能已被覆盖"
            recovery["backup"] = backup
            state = _new_state()
            # 老字段保留（有人可能读它），但**它不再是唯一线索**：
            # 真正的体检结果在 `store.recovery`，并且会出现在 `/api/health` 里。
            if backup:
                state["_recovered_from"] = backup
            recovery["message"] = (
                f"状态文件用不了（{recovery['detail']}），已备份到 "
                f"{backup or '（备份失败）'}；**帖子、订阅、审计都没有恢复**，"
                f"从现在开始是空状态。这不是‘新站’，是丢了一次状态")

        # 补上缺失的顶层键（老状态文件遇到新代码时不炸）
        for k, v in DEFAULT_STATE.items():
            state.setdefault(k, json.loads(json.dumps(v)))
        # `counters` 是嵌套的，上面那层 setdefault 不会补它里面的新键
        # （加了 `audit` / `appeal` 之后老状态文件会缺）—— 少了它会 KeyError
        for k, v in DEFAULT_STATE["counters"].items():
            state["counters"].setdefault(k, v)
        obj = cls(path, state)
        obj.recovery = recovery
        return obj

    @classmethod
    def load_or_seed(cls, path):
        """**首次运行**用：文件不存在就写一份演示状态，然后正常加载（B26）。

        和 `load()` 的分工：

        - `load()`：文件不在 → 返回空状态，**不落盘**（单元测试要的语义）；
        - `load_or_seed()`：文件不在 → 建演示状态并落盘（**启动服务**要的语义）。

        为什么不在 `load()` 里直接建：门禁里有几十处 `StateStore.load(临时路径)`，
        它们期望"空状态"；如果 `load()` 自己建演示用户，那些断言会莫名其妙多出
        两个用户。**"启动"和"读一个文件"是两件事，用两个入口表达。**
        """
        if not path or path == ":memory:":
            return cls.load(path)
        if os.path.exists(path):
            return cls.load(path)
        store = cls.load(path)
        store.state = demo_state()
        store.save()
        return store

    @staticmethod
    def _read_checked(path):
        """读 + 形状检查。返回 `(state_or_None, recovery_or_None)`。

        **三态，不是两态**：能读 / 读不出来 / 读出来但形状不对。
        原来的 `except (JSONDecodeError, OSError)` 只覆盖了中间那种，
        另外两种分别是"服务起不来"和"起来了但每个请求都 500"。
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return None, {"reason": "unreadable",
                          "detail": f"{type(e).__name__}: {e}"}
        if not isinstance(state, dict):
            return None, {"reason": "not_an_object",
                          "detail": f"顶层是 {type(state).__name__}，不是 JSON 对象"}
        wrong = [f"`{k}` 应该是 {t.__name__}，实际是 {type(state[k]).__name__}"
                 for k, t in _TOP_TYPES.items()
                 if k in state and not isinstance(state[k], t)]
        if wrong:
            return None, {"reason": "bad_shape", "detail": "；".join(wrong)}
        return state, None

    @contextmanager
    def transaction(self):
        """把"改状态 + save()"包成一段（别的模块直接改 post 字段时用）。

        `with state.transaction(): post["x"] = 1` —— 退出时 save。
        **不要**在别处改 `store.state` 里的东西而不走这里，
        那样就绕开了串行化（`save()` 里的 `json.dump` 会撞上别的线程的修改）。
        """
        with self._lock:
            try:
                yield self.state
            finally:
                self.save()

    def save(self):
        if not self.path or self.path == ":memory:":
            return
        with self._lock:
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)      # 原子替换：不会留下半个文件

    # ---------- 用户 / 订阅 ----------

    def user(self, user_id):
        """取用户，不存在就按 demo 默认值建一个。

        D3/D4 不做真登录（B-KICKOFF 第 87 行："假登录态就够"），
        所以第一个请求就顺手建号 —— 演示时不用先"注册"。

        **整个"查 + 建 + 跨天重置"在锁里**：原来它只在 `save()` 里拿锁，
        于是同一个新用户的两个并发请求会各建一个 dict（后写的覆盖先写的，
        额度重置成 0）。这类"读-改-写"不在锁里，就是丢更新。
        """
        with self._lock:
            u = self.state["users"].get(user_id)
            if u is None:
                u = {
                    "id": user_id,
                    "nickname": "示例同学" if user_id == "u_demo" else user_id,
                    "subscribed": False,
                    "expires_at": None,
                    "daily_free_quota": 3,
                    "daily_used": 0,
                    "used_date": _today_str(),
                }
                self.state["users"][user_id] = u
                self.save()
            # 跨天重置：额度是"每天"的
            if u.get("used_date") != _today_str():
                u["used_date"] = _today_str()
                u["daily_used"] = 0
                self.save()
            return u

    def is_subscribed(self, u):
        """订阅是否**还在有效期**。`expires_at` 过了就要失效 ——
        只看 `subscribed` 布尔值会让过期用户永远免费。"""
        if not u.get("subscribed"):
            return False
        exp = u.get("expires_at")
        if not exp:
            return True
        try:
            return datetime.fromisoformat(exp) > datetime.now()
        except (ValueError, TypeError):
            return False

    def consume_quota(self, u, n=1):
        """扣一次免费额度。返回 (allowed, remaining)。

        订阅用户不扣。额度用尽返回 allowed=False —— 调用方负责返回 402，
        **绝不能"扣不动就放行"**，那等于没有付费墙。

        **读额度 → 判断 → 加一 必须在同一把锁里。** 否则两个并发请求都读到
        `daily_used=2`、都判断"还能扣"、都写 3 —— 用户白拿一条。
        这是"读-改-写"的经典丢更新，而它的表现是**付费墙偶尔漏一条**，
        没有任何日志会告诉你。
        """
        with self._lock:
            if self.is_subscribed(u):
                return True, None
            if u["daily_used"] + n > u["daily_free_quota"]:
                return False, max(0, u["daily_free_quota"] - u["daily_used"])
            u["daily_used"] += n
            self.save()
            return True, u["daily_free_quota"] - u["daily_used"]

    def subscribe(self, user_id, plan, months):
        """模拟支付成功后开通/续期。**不接真实支付通道**（学生拿不到微信商户号）。

        "读到期日 → 算新到期日 → 写回"必须在锁里：两次并发续期各自读到旧日期，
        后写的那个会把前一次买的月份**覆盖掉**（用户付两次、只多一个月）。
        """
        with self._lock:
            u = self.user(user_id)
            now = datetime.now()
            base = now
            exp = u.get("expires_at")
            if self.is_subscribed(u) and exp:
                try:                              # 续期：从原到期日往后加
                    base = max(now, datetime.fromisoformat(exp))
                except (ValueError, TypeError):
                    base = now
            # 按月加：用 30 天近似（演示够用，且不做日历运算就没有月末歧义）
            from datetime import timedelta
            u["subscribed"] = True
            u["expires_at"] = (base + timedelta(days=30 * months)).replace(
                microsecond=0).isoformat(timespec="seconds")
            u["plan_id"] = plan["id"]
            order = {
                "order_id": f"o_{now.strftime('%Y%m%d%H%M%S')}",
                "user_id": user_id,
                "plan_id": plan["id"],
                "plan_name": plan["name"],
                "amount": plan["price"],
                "months": months,
                "paid_at": now.isoformat(timespec="seconds"),
                "status": "paid",
                # 这行必须留着：评委看到"支付成功"要能一眼看出是模拟的
                "_note": "演示用模拟支付，未接真实支付通道",
            }
            self.state["orders"].append(order)
            self.save()
            return order

    def cancel_subscription(self, user_id):
        """**取消订阅**。返回取消之前是不是真的在订阅状态。

        ## 为什么必须有一条正路（这是被 C 卡出来的）

        在这之前，`DELETE /api/subscriptions/{channel}` 只关**渠道**（站内预览），
        而**没有任何接口能取消订阅**。测试时开了一次订阅，就再也退不回去 ——
        C 的原话是"无法还原"。一个演示系统里"开了就退不掉"是致命的：
        演示前想把状态复位，只能手改 `state.json`，而手改会带出更脏的状态
        （这正是 `u_demo` 现在挂着 `subscribed=true` 的原因）。

        ## 两条刻意保留的东西

        1. **`orders` 一条都不删。** 订单是"曾经发生过一次模拟支付"的历史，
           取消订阅不改变这个事实。删了之后 `pricing` 页和 `orders` 会变成
           "没买过"，而用户明明点过 —— **那是抹掉历史，不是取消服务**。
        2. **`plan_id` 保留**（它记录的是"买的是哪个套餐"）。
           真正的开关是 `subscribed` + `expires_at`，`is_subscribed()` 只认这两个。
        """
        with self._lock:
            u = self.user(user_id)
            was = self.is_subscribed(u)
            u["subscribed"] = False
            u["expires_at"] = None
            u["daily_used"] = 0        # 取消后按免费额度重新算，不清零也不影响
            self.save()
            return was

    # ---------- 论坛 ----------

    def next_post_id(self):
        with self._lock:
            self.state["counters"]["post"] += 1
            n = self.state["counters"]["post"]
        return f"p_{date.today().strftime('%Y%m%d')}_{n:04d}"

    def add_post(self, post):
        # **整段在锁里**：`append` + `save()` 原来只锁了 save，
        # 于是并发的第二个 append 可能正好插进第一个线程的 `json.dump` 里。
        with self._lock:
            self.state["posts"].append(post)
            self.save()
            return post

    def find_post(self, post_id):
        for p in self.state["posts"]:
            if p.get("id") == post_id:
                return p
        return None

    def posts(self):
        return self.state["posts"]

    # ---------- 关注规则（契约 api.md 第 11 节） ----------

    def watches(self, user_id):
        """某个用户的关注规则，按 `created_at` 降序、同刻按 `id` 排。

        **降序 + 次级键都是刻意的**：最近关注的在前面；
        同一秒建的规则按 id 排，否则顺序在不同请求之间可能不一样
        （Python 的 sort 稳定，但底层列表顺序会随删除操作变），
        前端画出来的标签会莫名其妙跳位置。
        """
        mine = [w for w in self.state["watches"] if w.get("user_id") == user_id]
        return sorted(mine, key=lambda w: (w.get("created_at") or "", w.get("id") or ""),
                      reverse=True)

    def find_watch(self, user_id, watch_id):
        for w in self.state["watches"]:
            if w.get("id") == watch_id and w.get("user_id") == user_id:
                return w
        return None

    def add_watch(self, watch):
        """加一条规则。返回 `{"watch": …, "created": bool}`。

        **为什么返回字典而不是 `(watch, created)` 元组**：
        我第一版就是元组，然后在调用处按反了顺序写成 `created, watch = ...`，
        结果是 `public_view(True)` → `'bool' object has no attribute 'get'`。
        两个元素、类型还不同（一个是 dict、一个是 bool），顺序全靠记 —— 迟早记反。
        键名自己说明顺序，这类 bug 就不存在了。

        `created=False` 表示这条规则已经在（幂等）。**调用方负责这个判断**，
        但这里仍然防一手：同一 id 的规则不会真的写进去两条，
        否则 digest 的 `matched_by` 会出现两遍"学校=浙江大学"，
        界面上是"为什么提醒我：学校=浙江大学；学校=浙江大学"，看起来像 bug。
        """
        with self._lock:
            existing = self.find_watch(watch["user_id"], watch["id"])
            if existing is not None:
                return {"watch": existing, "created": False}
            self.state["watches"].append(watch)
            self.save()
            return {"watch": watch, "created": True}

    def remove_watch(self, user_id, watch_id):
        """取关。返回是否真的删掉了 —— **调用方要据此返回 404**，
        不能"反正删完它不在了"就一律 200（见契约第 11 节的理由）。"""
        with self._lock:
            for i, w in enumerate(self.state["watches"]):
                if w.get("id") == watch_id and w.get("user_id") == user_id:
                    self.state["watches"].pop(i)
                    self.save()
                    return True
            return False

    # ---------- 审核审计日志（契约 api.md 第 13 节） ----------
    #
    # **这一节故意只有两个方法：一个追加、一个读。**
    # 没有 update_audit / delete_audit，也不会有 —— append-only 靠的是
    # **接口层面根本不提供**，而不是靠调用方自觉不去改。
    # 一条可以被改的审计记录，在法律意义上等于没有审计。

    def next_audit_id(self):
        with self._lock:
            self.state["counters"]["audit"] += 1
            n = self.state["counters"]["audit"]
        return f"a_{date.today().strftime('%Y%m%d')}_{n:05d}"

    def append_audit(self, record):
        """追加一条审计。**只进不出**：没有任何对应的修改或删除方法。

        返回落库后的那条（已分配 `audit_id`）。
        """
        with self._lock:
            if not record.get("audit_id"):
                record["audit_id"] = self.next_audit_id()
            self.state["audit"].append(record)
            self.save()
            return record

    def audit_log(self, post_id=None, actor_id=None, action=None, limit=None):
        """读审计。可按 `post_id` / `actor_id` / `action` 过滤。**新的在前。**

        `limit` 只截返回，**不影响落盘** —— 审计不因为"看得少"而少存。
        """
        out = []
        for r in self.state["audit"]:
            if post_id and r.get("post_id") != post_id:
                continue
            if actor_id and r.get("actor_id") != actor_id:
                continue
            if action and r.get("action") != action:
                continue
            out.append(r)
        out.reverse()
        return out[:limit] if limit else out

    # ---------- 申诉 ----------

    def next_appeal_id(self):
        with self._lock:
            self.state["counters"]["appeal"] += 1
            n = self.state["counters"]["appeal"]
        return f"ap_{date.today().strftime('%Y%m%d')}_{n:04d}"

    # ---------- 用户画像（契约 api.md 第 16 节） ----------

    def profile_of(self, user_id):
        """返回 `(profile, updated_at)`。没填过 → `(None, None)`。

        **没填过不是错误。** 接口层据此返回 200 + `profile: null`，
        而不是 404 —— 404 的意思是"这个用户不存在"，而"新用户还没填画像"
        是一个正常状态。
        """
        rec = (self.state.get("profiles") or {}).get(user_id)
        if not rec:
            return None, None
        return rec.get("profile"), rec.get("updated_at")

    def set_profile(self, user_id, profile):
        """整份替换（不是合并）。返回落库时间。

        **整份替换而不是合并**：画像是一份小对象，读-改-写代价可以忽略，
        而"合并"的语义会让"我把专业删了"变成一次无效操作 ——
        用户删不掉东西，是表单最让人恼火的一种。
        """
        with self._lock:
            now = datetime.now().isoformat(timespec="seconds")
            self.state.setdefault("profiles", {})[user_id] = {
                "profile": profile, "updated_at": now,
            }
            self.save()
            return now

    def delete_profile(self, user_id):
        """删画像。返回删掉之前是否存在。

        **不清推送去重记录。** 删画像的目的是"别再推了"，不是"重置推送历史"——
        清了的话，用户重新填画像会立刻收到一批**已经收过**的旧事件，
        而他刚刚表达的意愿恰恰是"我不想被这些打扰"。
        """
        with self._lock:
            had = (self.state.get("profiles") or {}).pop(user_id, None) is not None
            if had:
                self.save()
            return had

    # ---------- 推送去重 / 限频（契约 api.md 第 16 节） ----------

    def push_seen_ids(self, user_id):
        """该用户**今天**已经推过的 `push_id` 集合。

        为什么只认今天：推送是"每天的结果"。把历史永久压住会让"重新订阅"
        变成"什么都收不到"，而只认今天则"昨天的还会再推一次" —— 后者是噪音，
        前者是功能失效。选噪音。
        """
        today = _today_str()
        return {
            p["push_id"]
            for p in (self.state.get("pushes") or [])
            if p.get("user_id") == user_id and p.get("on") == today
        }

    def push_count_today(self, user_id):
        """今天已经推了几条（算限频用）。"""
        today = _today_str()
        return sum(
            1 for p in (self.state.get("pushes") or [])
            if p.get("user_id") == user_id and p.get("on") == today
        )

    def record_pushes(self, user_id, items):
        """把**这次真的推出去的**条目记下来。

        `items` 是推送项本身，这里只取 `push_id` / `event_id` —— **不存正文**。
        正文每次从 `events.jsonl` 现算；存下来会变成两份真相，
        而两份真相里先烂的那份没人知道。
        """
        if not items:
            return 0
        today = _today_str()
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            for it in items:
                self.state.setdefault("pushes", []).append({
                    "push_id": it.get("push_id"),
                    "event_id": it.get("event_id"),
                    "user_id": user_id,
                    "on": today,
                    "at": now,
                })
            self.save()
        return len(items)
