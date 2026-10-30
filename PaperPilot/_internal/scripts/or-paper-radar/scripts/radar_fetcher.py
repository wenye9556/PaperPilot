from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEBUG_PORT = 9222
BASE_DEBUG_URL = f"http://127.0.0.1:{DEBUG_PORT}"

# 用哪个用户数据目录：True=借用用户日常浏览器的真实 profile，False=PaperPilot 专用目录。
# 这里默认 True 是为了让本文件**单独当命令行工具跑**时行为不变；
# PaperPilot 应用侧会在 import 之后按 settings.browser.mode 覆写这个模块常量
# （pipeline._adapt_fetcher_paths 的同款做法），默认把它设成 False。
PREFER_REAL_PROFILE = True

# ---------------------------------------------------------------- 浏览器探测
# 2026-10-27 修：原先只写死一条 Edge 的 x86 路径，且 --user-data-dir 指向一个
# **全新的空 profile**。后果是「用户浏览器里明明登录着学校账号、手动能下 PDF，
# 软件却下不到」——因为软件开的那个浏览器实例跟用户的登录态毫无关系。
# 现在改为：
#   1) 依次探测 Edge / Chrome 的多个常见安装路径（含 64 位、x86、用户级安装）；
#   2) 优先复用用户**真实 profile**（里面有校园网/机构订阅的登录 cookie）；
#   3) 真实 profile 被占用（浏览器正开着）时，才退回独立 profile。
_BROWSER_CANDIDATES = [
    # (显示名, exe 候选路径, 默认用户数据目录)
    ("Edge", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    ], r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ("Chrome", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ], r"%LOCALAPPDATA%\Google\Chrome\User Data"),
]

# 兼容旧常量名（外部有 from radar_fetcher import EDGE_BIN 的值拷贝用法）
EDGE_BIN = _BROWSER_CANDIDATES[0][1][0]
PROFILE_DIR = str(Path.home() / "AppData" / "Local" / "edge_debug_profile")


def _expand(p: str) -> str:
    return os.path.expandvars(p) if "%" in p else p


def detect_browser(prefer_real_profile: bool = True) -> dict:
    """探测本机可用的 Chromium 系浏览器及其用户数据目录。

    返回：{"name", "exe", "user_data_dir", "profile_dir", "reuse_real": bool}
    找不到任何浏览器时返回 {}。

    `profile_dir` 是本函数真正要交给 --user-data-dir 的值：
      - 能安全复用真实 profile → 指向用户的 User Data（登录态在里面）
      - 否则 → 指向 PaperPilot 自己的独立目录（不干扰用户浏览器）
    """
    for name, exes, udd in _BROWSER_CANDIDATES:
        exe = next((_expand(c) for c in exes if Path(_expand(c)).exists()), "")
        if not exe:
            continue
        real = _expand(udd)
        reuse = bool(prefer_real_profile) and Path(real).is_dir() and not _profile_locked(real)
        if reuse:
            pdir = real
        else:
            # 独立 profile 仍按浏览器分目录，避免 Edge/Chrome 抢同一个路径
            pdir = str(Path.home() / "AppData" / "Local" / f"paperpilot-{name.lower()}-profile")
        return {"name": name, "exe": exe, "user_data_dir": real,
                "profile_dir": pdir, "reuse_real": reuse}
    return {}


def _profile_locked(user_data_dir: str) -> bool:
    """用户数据目录是否被正在运行的浏览器占用。

    锁文件名是**平台相关**的：Windows 上是 `lockfile`，Linux/macOS 上是
    `SingletonLock`。v1.0.5 及之前只找 `SingletonLock`，而本产品只发 Windows，
    那个文件在这台机器上根本不存在 → `exists()` 恒为假 → 恒判「没人在用」→
    浏览器明明开着，还是拿真实 profile 去启动；新进程把命令转交给已开实例后
    自己退出，调试端口永远起不来（实测 Edge 与 Chrome 表现一致，整批任务因此
    只剩 OA 直下 + arXiv 两条不需要浏览器的路）。

    Windows 上 Chromium 以独占共享模式持有 `lockfile`，第二个进程连以只读方式
    open() 都会撞 sharing violation —— 这本身就是可靠的占用信号。再用
    `msvcrt.locking` 兜一层：个别版本以共享方式打开该文件、真正靠命名对象互斥。
    """
    for name in ("lockfile", "SingletonLock"):
        lock = Path(user_data_dir) / name
        if not lock.exists():
            continue
        try:
            # 只用 rb：既不创建文件，也不截断；Windows 打开时仍会校验既有句柄的共享模式
            fh = open(lock, "rb")
        except OSError:
            return True                      # 打不开 → 浏览器正持有这个目录
        try:
            try:
                import msvcrt
            except ImportError:
                return False                 # 非 Windows：能打开就当作未占用
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                return True                  # 锁不上 → 浏览器正开着
        finally:
            fh.close()
    return False


def clean_str(s: str) -> str:
    return re.sub(r'[\/:*?"<>|]', "", s)


def _is_pdf_bytes(raw: bytes) -> bool:
    """判断字节流是不是 PDF，允许文件头前面有空白/控制字节。

    实测 6 个真实开放获取直链里有 1 个（10.5281/zenodo.18112299）返回的首字节是
    `b'\\n%PDF-1.5'` —— 这是完全合法的 PDF（规范只要求 `%PDF-` 出现在文件头部，
    前面允许空白），但严格的 `startswith(b"%PDF-")` 判它「不是 PDF」，
    结果是**下载明明成功了、文件却被当垃圾丢弃**，用户看到的是一条查不出原因的失败。
    """
    if not raw:
        return False
    return raw.lstrip(b"\x00\r\n \t\x1a").startswith(b"%PDF-")


def browser_user_agent(ws_url: str) -> str:
    """取浏览器自己的 UA（出版商常把 UA 与 cookie 绑定校验，换 UA 可能被判脚本）。"""
    try:
        r = call_cdp(ws_url, "Runtime.evaluate",
                     {"expression": "navigator.userAgent", "returnByValue": True})
        ua = ((r.get("result") or {}).get("result") or {}).get("value")
        if isinstance(ua, str) and ua.strip():
            return ua.strip()
    except Exception:
        pass
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def session_cookies(ws_url: str, urls: list) -> dict:
    """从浏览器会话取指定 URL 会带上的 cookie —— **含 httpOnly**，页内 JS 的
    `document.cookie` 拿不到那些，而机构订阅会话恰恰经常是 httpOnly 的。"""
    try:
        r = call_cdp(ws_url, "Network.getCookies", {"urls": urls})
        return {c["name"]: c["value"] for c in ((r.get("result") or {}).get("cookies") or [])
                if c.get("name")}
    except Exception:
        return {}


def fetch_pdf_via_session(ws_url: str, url: str, out_path, timeout: int = 60,
                          max_bytes: int = 80 * 1024 * 1024) -> bool:
    """浏览器只管维持会话，实际下载交给一次普通 HTTP 请求（带上会话 cookie 与浏览器 UA）。

    为什么把它放在页内 fetch 之前：`Page.navigate` 到 PDF 后，Chromium 会把它渲染成
    查看器文档（`document.contentType == application/pdf`），此时
    `fetch(location.href) + FileReader + awaitPromise` 经常**根本不返回**——
    实测同一目标连试 2 次全失败（一次返回空、一次撞满 45s WS 超时），
    而 cookie 交接一次请求就拿到 2.68MB 合法 PDF。
    只用标准库，不给这个 vendored skill 引新依赖。
    """
    headers = {"User-Agent": browser_user_agent(ws_url), "Accept": "application/pdf,*/*"}
    jar = session_cookies(ws_url, [url])
    if jar:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except Exception:
        return False
    if len(data) < 1024 or not _is_pdf_bytes(data[:64]):
        return False
    try:
        Path(out_path).write_bytes(data)
        return True
    except OSError:
        return False


def call_cdp(ws_url: str, method: str, params: dict | None = None, msg_id: int = 1):
    import websocket

    ws = websocket.create_connection(ws_url, timeout=45)
    payload = {"id": msg_id, "method": method, "params": params or {}}
    ws.send(json.dumps(payload))
    resp = json.loads(ws.recv())
    ws.close()
    return resp


def close_tab_safely(tab_id: str):
    try:
        urllib.request.urlopen(f"{BASE_DEBUG_URL}/json/close/{tab_id}", timeout=5)
    except Exception:
        pass


def ensure_edge_running(want_notes: bool = False):
    """确保有一个带调试端口的浏览器在跑。

    默认返回 bool（保持与历史调用点的兼容）；`want_notes=True` 时返回
    (ok, notes)，notes 里逐条说明用了哪个浏览器、哪个 profile、是否复用了
    登录态——调用方应当把它们打进日志，否则用户完全看不出
    「为什么手动能下、软件下不到」。
    """
    notes = []
    try:
        with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json/version", timeout=3) as resp:
            notes.append("调试端口已在应答，复用现有会话")
            return (True, notes) if want_notes else True
    except Exception:
        pass

    info = detect_browser(prefer_real_profile=PREFER_REAL_PROFILE)
    if not info:
        notes.append("未找到 Edge / Chrome，浏览器通道不可用")
        return (False, notes) if want_notes else False

    notes.append(f"浏览器 -> {info['name']} ({info['exe']})")
    notes.append(f"profile -> {info['profile_dir']}")
    if info["reuse_real"]:
        notes.append("已复用你的浏览器登录态（含校园网/机构订阅 cookie）")
    else:
        notes.append("使用 PaperPilot 专用 profile（机构登录只需在里面做一次，cookie 长期保留）")
        if not PREFER_REAL_PROFILE and _profile_locked(info.get("user_data_dir", "")):
            notes.append("你的日常浏览器此刻正开着 —— 与本次下载无关，不用再关掉它")

    cmd = [
        info["exe"],
        f"--remote-debugging-port={DEBUG_PORT}",
        # 不加 --remote-allow-origins=*。实测（Edge 156）：
        #   · 本模块的客户端用 websocket-client，握手时**不发 Origin**，不加这个标志照样连得上；
        #   · 加了它，任何本机网页都能带上自己的 Origin 连进这个 WS，在**持有机构订阅会话**
        #     的浏览器里执行任意 JS（Runtime.evaluate）——等于把学校权限开放给本机所有页面。
        #   · 不加时伪造的跨站 Origin 被 403 拒绝，而我们这条链路完全不受影响。
        # 独立 profile 成为默认之后，这个目录里会长期存着机构登录 cookie，更要收紧。
        # 不加这两个开关时，独立 profile 的第一次启动会被首启流程接管：
        # 实测 Chrome 唯一的页面标签变成 chrome://intro/（about:blank 被顶掉），
        # 于是取件逻辑只能退到「第一个标签」，而页内 fetch 在内部页上直接
        # TypeError: Failed to fetch —— 整条浏览器通道在零缓存机器上必挂。
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={info['profile_dir']}",
    ]
    if info["reuse_real"]:
        # 复用真实 profile 时开一个独立窗口，避免影响用户已开的标签页
        cmd.append("--new-window")
    else:
        # 专用 profile 最小化启动：下载是后台活，不该弹窗口抢前台焦点。
        # （v1.4.0 前每个下载批次都会把一个 Edge 窗口弹到用户脸上。）
        cmd.append("--start-minimized")
    cmd.append("about:blank")

    try:
        subprocess.Popen(cmd)
    except Exception as e:
        notes.append(f"浏览器启动失败：{type(e).__name__}: {e}")
        return (False, notes) if want_notes else False

    # 复用真实 profile 时启动更慢（要读用户完整数据），多等一会儿
    deadline = time.time() + (12 if info["reuse_real"] else 6)
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json/version", timeout=3) as resp:
                notes.append("调试端口就绪")
                return (True, notes) if want_notes else True
        except Exception:
            continue
    notes.append("调试端口迟迟不应答（浏览器可能未真正启动）")
    return (False, notes) if want_notes else False


def _search_sciencedirect_by_title(title: str) -> str | None:
    """当 DOI 变更或失效时，使用文章全名在 ScienceDirect 搜索并定位最新有效 PII。"""
    search_tab_id = None
    try:
        q = urllib.parse.quote(title.strip())
        search_url = f"https://www.sciencedirect.com/search?qs={q}"
        req = urllib.request.Request(f"{BASE_DEBUG_URL}/json/new?{search_url}", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            tab = json.loads(r.read().decode("utf-8"))
        search_tab_id = tab["id"]
        time.sleep(4.0)

        with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json", timeout=5) as r:
            tabs = json.loads(r.read().decode("utf-8"))
        t = next((x for x in tabs if x.get("id") == search_tab_id), None)
        if not t:
            return None

        res = call_cdp(t["webSocketDebuggerUrl"], "Runtime.evaluate", {"expression": "document.documentElement.outerHTML"})
        html = res.get("result", {}).get("result", {}).get("value", "")

        m = re.search(r'href="(/science/article/pii/([^"]+))"', html)
        if m:
            pii = m.group(2).split("?")[0].split("/")[0]
            print(f"    [🔍 标题检索命中] PII: {pii}")
            return pii
    except Exception as e:
        print(f"    [-] 标题检索异常: {e}")
    finally:
        if search_tab_id:
            close_tab_safely(search_tab_id)
    return None


def _extract_sd_pdf_token(html_content: str):
    """从 ScienceDirect 页面 HTML 里抽取 pdfft 下载签名令牌。

    返回 (md5, pid, pii)；抽不到返回 None。

    v1.9.9 修：旧实现只做一次字面子串匹配 `'"pdfDownload":{'`，
    对下面这些**真实存在**的写法全都视而不见，于是「明明有权限却报未取到」：
      · `"pdfDownload": {`         —— 冒号后带空格（JSON 美化/压缩差异）
      · `&quot;pdfDownload&quot;:{` —— outerHTML 把内联 JSON 转义了
      · 字段整体缺失，但页面上有 `<a href=".../pdfft?md5=..&pid=..">` 直链
    现在先做容错子串判定，再退一步直接正则抓 pdfft 直链。
    """
    if not html_content:
        return None

    # 先归一化转义，再把全部空白压掉——这样「冒号后带不带空格」「换行缩进」
    # 都不再影响匹配（HTTP 压缩后与美化后的 HTML 差异就在于此）。
    probe = (html_content.replace("&quot;", '"')
                         .replace("&amp;", "&")
                         .replace("\\u0022", '"'))
    probe = re.sub(r"\s+", "", probe)

    # A. 标准的 pdfDownload 内联 JSON（此时已无空白，统一为 "pdfDownload":{）
    if '"pdfDownload":{' in probe:
        m = re.search(r'"pdfDownload":\{"md5":"([^"]+)"', probe)
        m2 = re.search(r'"pdfDownload":\{[^}]*?"pid":"([^"]+)"', probe)
        m3 = re.search(r'"pdfDownload":\{[^}]*?"pii":"([^"]+)"', probe)
        if m and m2 and m3:
            return (m.group(1), m2.group(1), m3.group(1))

    # B. 页面直接给了 pdfft 直链（含全部签名参数）——优先从直链取
    m_link = re.search(r"/science/article/pii/([A-Za-z0-9]+)/pdfft\?([^\"'<>]+)", probe)
    if m_link:
        pii = m_link.group(1)
        qs = dict(re.findall(r"([^=&]+)=([^=&]*)", m_link.group(2)))
        if qs.get("md5") and qs.get("pid"):
            return (qs["md5"], qs["pid"], pii)

    # C. 最后兜底：md5/pid/pii 三个字段散落在页面任意位置（无 pdfDownload 包裹）
    m_md5 = re.search(r'"?md5"?[:=]"?([A-Za-z0-9]{6,})', probe)
    m_pid = re.search(r'"?pid"?[:=]"?([^",}<]{3,})', probe)
    m_pii = re.search(r'"?pii"?[:=]"?([A-Za-z0-9]{8,})', probe)
    if m_md5 and m_pid and m_pii:
        return (m_md5.group(1), m_pid.group(1), m_pii.group(1))
    return None


def fetch_pdf_via_edge(doi: str, expected_title: str, out_dir: Path) -> Path | None:
    """使用 Edge DevTools 会话下载一篇 Elsevier / ScienceDirect 官方正版 PDF。
    支持双模定位：DOI 直连模式优先，失败或 404 时自动回退至 Title 标题精准检索模式。
    """
    clean_doi = (
        doi.replace("https://doi.org/", "")
        .replace("http://doi.org/", "")
        .strip()
    )
    article_tab_id = None
    pdf_tab_id = None

    try:
        # 1. 尝试打开 DOI 页面
        target_url = f"https://doi.org/{clean_doi}"
        req = urllib.request.Request(
            f"{BASE_DEBUG_URL}/json/new?{target_url}", method="PUT"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            article_tab = json.loads(r.read().decode("utf-8"))

        article_tab_id = article_tab["id"]
        ws_url = article_tab["webSocketDebuggerUrl"]

        # 2. 等待页面加载，检测是否有效
        html_content = ""
        m_pdf = None
        needs_title_fallback = False

        for attempt in range(10):
            time.sleep(2.0)
            try:
                with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json", timeout=5) as r:
                    tabs = json.loads(r.read().decode("utf-8"))
                for t in tabs:
                    if t.get("id") == article_tab_id:
                        ws_url = t.get("webSocketDebuggerUrl", ws_url)
                        if "Error: DOI Not Found" in t.get("title", ""):
                            needs_title_fallback = True
                        break

                if needs_title_fallback:
                    break

                res = call_cdp(
                    ws_url,
                    "Runtime.evaluate",
                    {"expression": "document.documentElement.outerHTML"},
                )
                html_content = (
                    res.get("result", {}).get("result", {}).get("value", "")
                )

                if '"pdfDownload":{' in html_content:
                    m_md5 = re.search(r'"md5":"([^"]+)"', html_content)
                    m_pid = re.search(r'"pid":"([^"]+)"', html_content)
                    m_pii = re.search(r'"pii":"([^"]+)"', html_content)
                    if m_md5 and m_pid and m_pii:
                        m_pdf = (m_md5.group(1), m_pid.group(1), m_pii.group(1))
                        break
                # 容错解析：冒号空格 / HTML 转义 / pdfft 直链三种写法都能认
                tok = _extract_sd_pdf_token(html_content)
                if tok:
                    m_pdf = tok
                    break
            except Exception:
                pass

        # 3. 如果 DOI 失效/未找到，触发标题检索模式
        if needs_title_fallback or not m_pdf:
            print(f"    [!] DOI 直连无效 ({clean_doi})，启动 ScienceDirect 标题精准回退检索...")
            close_tab_safely(article_tab_id)
            article_tab_id = None

            pii = _search_sciencedirect_by_title(expected_title)
            if not pii:
                print(f"    [-] 标题检索未定位到有效文章: {expected_title[:40]}")
                return None

            article_url = f"https://www.sciencedirect.com/science/article/pii/{pii}"
            req = urllib.request.Request(f"{BASE_DEBUG_URL}/json/new?{article_url}", method="PUT")
            with urllib.request.urlopen(req, timeout=10) as r:
                article_tab = json.loads(r.read().decode("utf-8"))
            article_tab_id = article_tab["id"]
            ws_url = article_tab["webSocketDebuggerUrl"]

            for _ in range(8):
                time.sleep(2.0)
                try:
                    res = call_cdp(
                        ws_url,
                        "Runtime.evaluate",
                        {"expression": "document.documentElement.outerHTML"},
                    )
                    html_content = res.get("result", {}).get("result", {}).get("value", "")
                    if '"pdfDownload":{' in html_content:
                        m_md5 = re.search(r'"md5":"([^"]+)"', html_content)
                        m_pid = re.search(r'"pid":"([^"]+)"', html_content)
                        m_pii = re.search(r'"pii":"([^"]+)"', html_content)
                        if m_md5 and m_pid and m_pii:
                            m_pdf = (m_md5.group(1), m_pid.group(1), m_pii.group(1))
                            break
                    tok = _extract_sd_pdf_token(html_content)
                    if tok:
                        m_pdf = tok
                        break
                except Exception:
                    pass

        if not m_pdf:
            print(f"    [-] 未能提取到下载签名令牌")
            return None

        md5, pid, pii = m_pdf
        pdfft_url = f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?md5={md5}&pid={pid}"

        # 4. 打开 PDF 导航标签页
        req_pdf = urllib.request.Request(
            f"{BASE_DEBUG_URL}/json/new?{pdfft_url}", method="PUT"
        )
        with urllib.request.urlopen(req_pdf, timeout=10) as r:
            pdf_tab = json.loads(r.read().decode("utf-8"))
        pdf_tab_id = pdf_tab["id"]
        pdf_ws_url = pdf_tab["webSocketDebuggerUrl"]

        # 5. 等待重定向至签名下载节点（最多 25 秒）
        signed_ready = False
        signed_url = pdfft_url
        for _ in range(16):
            time.sleep(1.5)
            with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json", timeout=5) as r:
                tabs = json.loads(r.read().decode("utf-8"))
            current_tab = next(
                (t for t in tabs if t.get("id") == pdf_tab_id), None
            )
            if current_tab and "pdf.sciencedirectassets.com" in current_tab.get(
                "url", ""
            ):
                pdf_ws_url = current_tab["webSocketDebuggerUrl"]
                signed_url = current_tab["url"]
                signed_ready = True
                break

        if not signed_ready:
            print(f"    [-] PDF 标签页重定向超时")
            return None

        safe_name = clean_str(expected_title)[:75].strip() + ".pdf"
        target_path = out_dir / safe_name

        # 6a. 主路：浏览器只管会话，下载交给一次带 cookie + 浏览器 UA 的 HTTP 请求。
        #     绕开「PDF 被渲染成查看器文档后页内 fetch 不返回」这一整类失败。
        if fetch_pdf_via_session(pdf_ws_url, signed_url, target_path):
            print(f"    [+] 成功落盘(会话直下): {target_path.name} "
                  f"({round(target_path.stat().st_size/1024/1024, 2)} MB)")
            return target_path

        # 6b. 退路：在页面原生上下文里 fetch + FileReader 取字节
        print("      [·] 会话直下没拿到 PDF，退回页内 fetch")
        extract_js = """
        fetch(window.location.href)
          .then(resp => resp.blob())
          .then(blob => new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onloadend = () => resolve(reader.result.split(',')[1]);
              reader.onerror = reject;
              reader.readAsDataURL(blob);
          }))
        """
        res_bytes = call_cdp(
            pdf_ws_url,
            "Runtime.evaluate",
            {"expression": extract_js, "awaitPromise": True, "returnByValue": True},
        )
        b64_val = (
            res_bytes.get("result", {}).get("result", {}).get("value", "")
        )

        if b64_val:
            pdf_data = base64.b64decode(b64_val)
            if _is_pdf_bytes(pdf_data):
                safe_name = clean_str(expected_title)[:75].strip() + ".pdf"
                target_path = out_dir / safe_name
                target_path.write_bytes(pdf_data)
                print(
                    f"    [+] 成功落盘: {target_path.name} ({round(len(pdf_data)/1024/1024, 2)} MB)"
                )
                return target_path

        print(f"    [-] 提取到的数据非标准 PDF")
        return None

    except Exception as e:
        print(f"    [×] 抓取异常: {e}")
        return None

    finally:
        if article_tab_id:
            close_tab_safely(article_tab_id)
        if pdf_tab_id:
            close_tab_safely(pdf_tab_id)
