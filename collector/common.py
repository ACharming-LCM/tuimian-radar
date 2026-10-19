# -*- coding: utf-8 -*-
"""HTTP 取数 + 本地缓存 + 通用 HTML 工具。

归属：A（数据管道）。其他人不要改这个文件，需要新能力先在 journal 里提。
"""
import hashlib
import os
import random
import re
import ssl
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from lxml import html as LH

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
OUT_DIR = os.path.join(ROOT, "data", "out")

# **一次抓取最多允许丢多少条，超过就不许覆盖。**
#
# 为什么是 0.8：一轮抓取里如果有几个站点挂了（DNS/TLS/改版），`crawl.py` 收到的
# 记录自然就少。它原来**不比较上一轮**，抓到多少写多少 —— 于是"今天网络不好"
# 会静默把 196 条覆盖成 120 条，而所有门禁在**第二天**才发现，那时昨天的数据已经没了，
# 而 `data/out/history/` 里归档的也是那份残缺数据。
#
# 0.8 和 `tests/test_data_contract.py` 的"条数突变"阈值**是同一个数**，
# 所以它定义在这里、两边都读它 —— 不这么办就是"同一个策略写在两个地方，
# 迟早分叉"（这个项目栽过 4 次）。
#
# 真要合法地减少条数（比如清理过期公告），传 `--allow-drop` 显式说明。
MIN_RETAIN_RATIO = 0.8

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# TLS：默认**校验**证书。以前是全局 `CERT_NONE`，抓到的内容可被中间人篡改而无感。
# 个别学校用过期/自签证书时，设 `TUIMIAN_TLS_INSECURE=1` 临时放宽（只应在本地排查时用，
# 并在日志里明确说出来）—— 不要为了"能抓"而默认关掉校验。
_CTX = ssl.create_default_context()
if os.environ.get("TUIMIAN_TLS_INSECURE", "").strip() in ("1", "true", "yes"):
    _CTX.check_hostname = False
    _CTX.verify_mode = ssl.CERT_NONE

os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def cache_path(url):
    return os.path.join(RAW_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest() + ".html")


BIN_DIR = os.path.join(RAW_DIR, "files")
os.makedirs(BIN_DIR, exist_ok=True)


def bin_cache_path(url):
    ext = os.path.splitext(url.split("?")[0])[1][:8] or ".bin"
    return os.path.join(BIN_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest() + ext)


def _ascii_url(url):
    """把 URL 里的非 ASCII 字符按 UTF-8 百分号转义。

    坑（已踩，四川大学「国优计划」那几条）：附件的 href 里直接带中文文件名，
    `urllib.request.Request` 只吃 ASCII，于是抛 InvalidURL / UnicodeEncodeError，
    在 pdf.enrich 里表现为"下载失败"——看起来像对方服务器坏了，其实是**我们没发出去**。
    只转义 `ord>=128` 的字符；已经是 `%XX` 的部分原样保留，不会二次转义。
    缓存键仍用原始 URL，所以历史缓存不会失效。
    """
    if not url:
        return url
    safe = set(string.ascii_letters + string.digits + "-._~:/?#[]@!$&'()*+,;=%")
    if all(ch in safe for ch in url):
        return url
    out = []
    for ch in url:
        if ch in safe:
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


def fetch_binary(url, refresh=False, retries=2, timeout=45, max_bytes=30 * 1024 * 1024,
                 err=None):
    """下载附件（PDF/DOCX）。与 fetch 分开，因为附件有独立缓存目录和更大的超时。

    PDF 比 HTML 大得多，学校服务器也慢，所以 timeout 给到 45 秒、重试 2 次。
    返回 (bytes, from_cache)；失败返回 (None, False)。
    """
    p = bin_cache_path(url)
    if not refresh and os.path.exists(p):
        with open(p, "rb") as f:
            return f.read(), True

    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(_ascii_url(url), headers={"User-Agent": UA,
                                                       "Accept-Language": "zh-CN,zh;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                print(f"    [附件过大，跳过] {url[-50:]}")
                return None, False
            with open(p, "wb") as f:
                f.write(body)
            time.sleep(random.uniform(0.5, 1.2))
            return body, False
        except Exception as e:                    # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    print(f"    [附件下载失败] {url[-60:]} -> {type(last).__name__}")
    return None, False


def classify_error(exc):
    """把网络异常分成可解释的类别。

    为什么重要：一个"解析失败"如果不说原因，就只能靠猜；
    而猜错的代价是去修一个不存在的问题。实测剩下来的失败只有两类，
    都是**静态抓取真的够不着**，而不是我们的 bug：
      · tls_handshake —— 站点服务端 SSL 握手失败（北大光华 gsm.pku.edu.cn）
      · js_app        —— 内容靠 AJAX 渲染（东南大学专业目录 gsas.seu.edu.cn）
    能归类，就能对用户诚实地说"这一条我们抓不到，请点原文"。
    """
    s = str(getattr(exc, "reason", exc))
    low = s.lower()
    if "截断" in s or "truncat" in low or "incompleteread" in low:
        return "truncated"
    if "sslv3_alert" in low or "handshake" in low or "certificate" in low:
        return "tls_handshake"
    if "getaddrinfo" in low or "name or service not known" in low:
        return "dns"
    if "refused" in low:
        return "refused"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "http error" in low or "http" == low[:4]:
        return "http_error"
    return type(exc).__name__


def fetch(url, refresh=False, retries=3, timeout=25, sleep=(0.6, 1.4), err=None):
    """取页面。默认走本地缓存（同一 url 不重复打扰对方服务器），refresh=True 强制重取。

    返回 (bytes, from_cache)。失败返回 (None, False)。
    传 err 这个 dict 进来可以把失败原因写在 err["reason"] 里（见 classify_error）。

    **`retries` 是"最多尝试几次"，不是"失败后重试几次"。**
    坑（已踩，而且差点让我把一所学校判成"网络不可达"）：
        `for i in range(retries)` 在 `retries=0` 时**一次请求都不发**，
        然后落到失败分支，报出 `NoneType: None` —— 因为 `last` 还是 None。
        我传 `retries=0` 时想的是"别重试"，实际是"别试"。
        更坏的是：命中了缓存的 URL 会在循环**之前**就返回，
        于是同一批探测里"有缓存的显示 OK、没缓存的显示不通"，
        看起来像"这几个站点宕了"，其实是**我根本没发请求**。
        现在 `max(1, ...)` 保证至少试一次，且没发过请求时给出的原因说得清楚。
    """
    p = cache_path(url)
    if not refresh and os.path.exists(p):
        with open(p, "rb") as f:
            return f.read(), True

    last = None
    for i in range(max(1, retries)):
        try:
            req = urllib.request.Request(_ascii_url(url), headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                # **读之前先记下服务端声明的长度，读完对一下。**
                #
                # 坑（已踩，而且是这一轮才发现的）：连接在中途被 RST、或者代理提前
                # 结束响应时，`r.read()` **可能正常返回一段半截的正文**，不抛异常。
                # 后果比"抓取失败"危险得多 —— 半截正文会被当成完整正文存下来，
                # 而判据一样通过（选择器命中、长度也够），于是我们**敢对用户说
                # "该公告未公布截止日"**，可那句话是建立在一段残页上的。
                # 实测：把一个 1400 字的正文截到 232 字，`body_state` 照样是 `ok`。
                #
                # 有 `Content-Length` 时这是**确凿的**证据，所以在这里拦、当场重试，
                # 而不是留给下游去猜"这条正文怎么这么短"。
                # 没有该头（分块传输）时无法判断 —— 这部分残余风险是真实的，
                # 记在 crawl.py 的 `_assemble` 注释里，不假装解决了。
                declared = r.headers.get("Content-Length")
                body = r.read()
            if declared and declared.strip().isdigit() and len(body) != int(declared):
                raise IOError(f"响应被截断：Content-Length={declared} 实际={len(body)} 字节")
            with open(p, "wb") as f:
                f.write(body)
            # 礼貌间隔，别把学校站点打挂。
            # `sleep` 既收 (最小, 最大) 元组，也收单个数字 ——
            # 参数名叫 sleep 却只收元组是个陷阱：我传过一次 `sleep=0.5`，
            # 报出来的是 `random.Random.uniform() argument after * must be an
            # iterable` —— 错在调用方，但这句话完全指不到现场。
            lo, hi = (sleep, sleep) if isinstance(sleep, (int, float)) else sleep
            time.sleep(random.uniform(lo, hi))
            return body, False
        except Exception as e:                    # noqa: BLE001 网络异常一律重试
            last = e
            time.sleep(1.2 * (i + 1))
    # 没发过请求时（attempts 走到 0）别说成网络错误 —— 那会把原因指向错的地方
    reason = classify_error(last) if last is not None else "没有发起请求"
    if err is not None:
        err["reason"] = reason
    print(f"    [fetch fail] {url} -> {reason}: {str(last)[:60]}")
    return None, False


def parse(raw):
    """bytes -> lxml 树。

    坑（已踩）：不要把 bytes 直接交给 LH.fromstring —— 它会按页面的 meta charset
    自行猜编码，遇到"声明 gb2312 实际 utf-8"或声明缺失的页面就会整页乱码
    （表现为 'ææå¦é¢' 这种 UTF-8 被当 latin-1 读的形态）。
    自己解码，且**先试 utf-8**：GBK 的字节序列几乎不可能同时是合法 UTF-8，
    所以这个顺序对两种页面都正确。
    """
    if not raw:
        return None
    try:
        text = decode(raw)
        # lxml 不接受「已经解码成 Unicode、却仍带 encoding 声明」的字符串。
        # 四川大学研招网正是 `<?xml version="1.0" encoding="utf-8"?>` + HTML：
        # 页面有完整的 15 条公告，但这里会 ValueError 后静默返回 None，整站产出 0。
        # 我们已经在 decode() 里完成编码判断，所以声明只需去掉，不能再让 lxml 二次猜编码。
        text = re.sub(r"^\s*<\?xml[^>]*encoding\s*=\s*['\"][^'\"]+['\"][^>]*\?>",
                      "", text, count=1, flags=re.I)
        return LH.fromstring(text)
    except Exception:                             # noqa: BLE001
        return None


CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([\w-]+)", re.I)


def decode(raw):
    if isinstance(raw, str):
        return raw
    declared = None
    m = CHARSET_RE.search(raw[:4096])
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()
        if declared in ("gb2312", "gbk"):
            declared = "gb18030"
    for enc in ("utf-8", declared, "gb18030"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def text(el):
    return " ".join(el.text_content().split()) if el is not None else ""


# 私用区字符（U+E000–U+F8FF）。高校 CMS 大量用图标字体（FontAwesome/alicdn），
# 图标在 HTML 里就是一个私用区码位，混进栏目名/标题时看着是空白，实际是毒：
#   · 计划任务的环境是 GBK（Windows 默认），`print()` 遇到它直接抛
#     `UnicodeEncodeError: 'gbk' codec can't encode character '\ue603'` —— 一个 print 废掉整轮抓取
#   · 落盘进 JSONL 之后，前端/搜索/去重都会拿它当"正文的一部分"
# 实测北大研招网的栏目名是 `\ue603 硕士招生 \ue61e 博士招生 …`（7 个图标）。
# 处理方式：**直接删掉**（不是替换成 '?'）。图标没有语义，删掉才是干净文本。
PUA_RE = re.compile("[\ue000-\uf8ff]")


def clean_text(s):
    """删掉私用区图标字符。**任何要落盘或打印的页面文本都该过一遍。**"""
    if not s:
        return s
    return PUA_RE.sub("", s)


def safe_stdout():
    """把 stdout/stderr 的编码错误策略改成 replace。

    为什么必须有：Windows 计划任务里 `PYTHONIOENCODING` 可能是 GBK，
    任何页面里没清干净的怪字符都会让 `print()` 抛异常，而异常发生在抓取中途 ——
    整轮数据就没了。**打印失败不该等于抓取失败。**
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# 被丢掉的非法链接计数（页面里 `http：` 全角冒号、空格、控制字符…）。
# 这个数必须在日志里打出来 —— 静默丢链接和静默丢数据是一回事。
URL_SKIP_COUNT = 0
URL_SKIP_SAMPLES = []
URL_REPAIRED_COUNT = 0


def norm_url(base, href):
    """把 href 拼成绝对 URL。**任何非法输入都不许抛异常。**

    坑（已踩，代价是 20 分钟白跑一整轮）：某个公告页里有个链接写成 `http：//…`
    （全角冒号），`urllib.parse.urljoin` 在 Python 3.10 里会抛
    `ValueError: netloc 'http：' contains invalid characters under NFKC normalization`。
    而 `norm_url` 是在 `detail.parse_detail` 的**循环体里**被调的 —— 一个坏链接
    直接把整轮抓取炸掉，`crawl` 最后才落盘，于是 20 分钟的数据一条没留。

    所以这里兜住一切异常：返回 None，并记到 `URL_SKIP_COUNT`。
    「这条链接用不了」是数据里的事实，不该升级成「整轮失败」。
    """
    global URL_SKIP_COUNT, URL_REPAIRED_COUNT
    if not href:
        return None
    try:
        href = href.strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            return None
        # 全角冒号/空格/控制字符：先归一化再看能不能拼
        fixed = href
        if href[:6].lower().startswith("http："):
            fixed = href.replace("：", ":", 1)
            URL_REPAIRED_COUNT += 1
        fixed = "".join(ch for ch in fixed if ch >= " " or ch in "\t")
        out = urllib.parse.urljoin(base, fixed)
        urllib.parse.urlsplit(out)          # 触发 netloc 校验（3.10 的校验在 urljoin 内部）
        return out
    except (ValueError, TypeError, UnicodeError) as e:
        URL_SKIP_COUNT += 1
        if len(URL_SKIP_SAMPLES) < 5:
            URL_SKIP_SAMPLES.append(f"{href[:60]!r}: {type(e).__name__}")
        return None


def canonical_url(u):
    """把 http/https 变体归一成一个**身份键**，用来判重。

    坑（已踩）：浙大物理学院的同一篇文章 http 和 https 都能打开，
    于是同一条公告进了两条记录，`id` 一模一样、只有 `url` 协议不同。
    crawl 原来按 `url` 去重，两条都留下了 —— 列表页就会出现成对的一模一样的卡片。

    **只用于判重，不要拿它替换展示用的 url**：
    浙大研究生招生网（grs）只有 http 能通，https 是拒绝连接的。
    """
    if not u:
        return ""
    p = urllib.parse.urlsplit(u.strip())
    host = (p.netloc or "").lower()
    path = urllib.parse.unquote(p.path or "").rstrip("/")
    return host + path + (f"?{p.query}" if p.query else "")


DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def iso_date(s):
    """从任意文本里抠出第一个 YYYY-MM-DD。"""
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"
