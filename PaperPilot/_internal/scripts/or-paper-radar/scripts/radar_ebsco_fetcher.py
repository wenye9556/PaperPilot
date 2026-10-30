"""radar_ebsco_fetcher.py — EBSCO 机构通道 PDF 下载器（UTD 五刊专用）

管线（全部在 Edge 调试会话内以纯 API 方式完成，无 UI 点击）：
  1. 页面上下文 fetch EBSCO 搜索 API（TI 精确标题查询）→ record_id
  2. 页面上下文 fetch fulltext API → 签名直链（content.ebscohost.com）
  3. urllib 直接下载签名直链 → 验证 %PDF- 文件头 → 统一入库 <文献库>/literature/

设计红线（见 references/ebsco-pipeline-notes.md 踩坑手册）：
  - 只对 UTD 五刊（ISSN 白名单，config.ebsco_channel）启用；Elsevier/IEEE 走原通道
  - 下载前强制标题相似度门禁（token 重叠 ≥ threshold，默认 0.55），杜绝错配
  - 下载后验证文件头 %PDF- 与最小大小
  - 失败五级如实报告：NO_RESULT / NO_PDF / MISMATCH / BLOCKED / ERROR，绝不静默
  - 签名直链一次性使用，不缓存
  - 遇 CAPTCHA / 反爬一律停止并转人工，不自动重试
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from radar_fetcher import BASE_DEBUG_URL, EDGE_BIN, PROFILE_DIR, call_cdp, close_tab_safely, ensure_edge_running, _is_pdf_bytes

SKILL_DIR = Path(__file__).resolve().parent.parent
PROFILE_PATH = SKILL_DIR / "config" / "radar_profile.json"
# 命令行单跑时的默认入库目录（应用内调用一律显式传 out_dir，走不到这里）。
# 取环境变量或用户「文档」目录，避免把某台机器的盘符写死在源码里。
LITERATURE_DIR = (Path(os.environ.get("PAPERPILOT_LIBRARY")
                       or (Path.home() / "Documents" / "PaperPilotLibrary")) / "literature")

SEARCH_API = "https://research.ebsco.com/api/search/v1/search?applyAllLimiters=true&includeSavedItems=false&excludeLinkValidation=true&includeHbrRestrictedLinks=true&useV3CoverArt=true"
FULLTEXT_API_TPL = ("https://research.ebsco.com/api/researcher-edge-aggregator/v1/records/{rid}/fulltext/pdf"
                    "?sourceRecordId={rid}&opid={opid}&intent=view&lang=en-US")
EBSCO_HOME = "https://research.ebsco.com/c/{opid}/search"


# ---------------------------------------------------------------- config ----
def load_ebsco_config() -> dict:
    cfg = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return cfg.get("ebsco_channel") or {}


def is_utd_paper(issn: str | None, cfg: dict | None = None) -> bool:
    """路由判据：容器期刊 ISSN ∈ UTD 白名单 → 走 EBSCO 通道。"""
    if not issn:
        return False
    cfg = cfg or load_ebsco_config()
    issn_norm = re.sub(r"[^0-9Xx]", "", issn or "").upper()
    for j in cfg.get("utd_issn_whitelist", []):
        if re.sub(r"[^0-9Xx]", "", j.get("issn", "")).upper() == issn_norm:
            return True
    return False


# ------------------------------------------------------------ 标题匹配门禁 ----
def _norm_title(s: str) -> str:
    """规范化：小写、去 <mark> 标签、去标点（含 en/em-dash）、压缩空白。"""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def title_similarity(query: str, candidate: str) -> float:
    """token 重叠率：命中词数 / min(两侧词数)。错配拦截实测阈值 0.55。"""
    A = set(_norm_title(query).split())
    B = set(_norm_title(candidate).split())
    if not A or not B:
        return 0.0
    hit = len(A & B)
    return hit / min(len(A), len(B))


# ------------------------------------------------------------- 管线核心 ----
def _eval_in_page(ws_url: str, js: str, await_promise: bool = True):
    return call_cdp(
        ws_url,
        "Runtime.evaluate",
        {"expression": js, "awaitPromise": await_promise, "returnByValue": True},
    )


def _eval_json(ws_url: str, js: str):
    """执行页面内 JS（通常是 async fetch），返回解析后的 dict。"""
    resp = _eval_in_page(ws_url, js)
    inner = resp.get("result", {}).get("result", {})
    if inner.get("type") == "string":
        return json.loads(inner["value"])
    if "value" in inner:
        return inner["value"]
    # 异常详情透传（如 CORS / 404 抛错）
    exc = resp.get("result", {}).get("exceptionDetails")
    if exc:
        raise RuntimeError(str(exc.get("text", "page exception"))[:200])
    return None


_JS_SEARCH_TPL = r"""
(async () => {
  const body = __PAYLOAD__;
  const resp = await fetch(__SEARCH_URL__, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const j = await resp.json();
  const items = (j.search && j.search.items) || [];
  return JSON.stringify(items.slice(0, 5).map(r => ({
    id: r.id,
    title: (r.title && (r.title.value || r.title)) || ''
  })));
})()
"""

_JS_FULLTEXT_TPL = r"""
(async () => {
  const resp = await fetch(__FT_URL__, { credentials: 'include' });
  const t = await resp.text();
  let j = null; try { j = JSON.parse(t); } catch (e) {}
  if (j && j.url) return JSON.stringify({ ok: true, url: j.url });
  return JSON.stringify({ ok: false, status: resp.status, head: t.slice(0, 150) });
})()
"""


def _open_ebsco_tab(opid: str) -> str | None:
    """打开（或复用）一个 EBSCO 页面标签，返回 tab id。机构 IP 即认证，无需登录。"""
    with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json", timeout=5) as r:
        tabs = json.loads(r.read().decode())
    for t in tabs:
        if "research.ebsco.com" in t.get("url", "") and t.get("type") == "page":
            return t["id"]
    home = EBSCO_HOME.format(opid=opid)
    req = urllib.request.Request(f"{BASE_DEBUG_URL}/json/new?{home}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        tab = json.loads(r.read().decode())
    time.sleep(4)
    return tab["id"]


def _tab_ws(tab_id: str) -> str:
    with urllib.request.urlopen(f"{BASE_DEBUG_URL}/json", timeout=5) as r:
        tabs = json.loads(r.read().decode())
    for t in tabs:
        if t.get("id") == tab_id:
            return t.get("webSocketDebuggerUrl", "")
    return ""


def fetch_pdf_via_ebsco(title: str, out_dir: Path | None = None,
                        issn: str | None = None, cfg: dict | None = None,
                        doi: str | None = None) -> dict:
    """下载一篇 UTD 论文的官方 PDF（EBSCO 机构通道）。

    doi 可选：给定时检索优先走 DOI（最精准，且不受 EBSCO 长标题 TI 零命中的影响）。

    返回结构化结果：
      {status: OK|NO_RESULT|NO_PDF|MISMATCH|BLOCKED|ERROR,
       rec_title, record_id, file, score, detail}
      OK 时 file 为已入库的绝对路径（<文献库>/literature/，统一去重）。
    """
    out_dir = out_dir or LITERATURE_DIR
    cfg = cfg or load_ebsco_config()
    opid = cfg.get("opid", "")   # 不内置任何学校的门户 id：没配就走 NO_OPID 分支
    dbs = cfg.get("databases", [])
    threshold = float(cfg.get("title_match_threshold", 0.55))
    min_bytes = int(cfg.get("min_pdf_bytes", 50000))

    if issn is not None and not is_utd_paper(issn, cfg):
        return {"status": "ERROR", "detail": f"non-UTD journal issn={issn}; route to legacy channel"}

    # 上面那句注释承诺的 NO_OPID 分支此前并不存在：opid 为空时会一路走到
    # _open_ebsco_tab()，用 `research.ebsco.com/c//search` 这个畸形 URL 去开标签，
    # 白等一次超时后返回一个看不出原因的 ERROR。
    if not str(opid or "").strip():
        return {"status": "NO_OPID", "detail": "机构门户 opid 未配置（设置里填你自己学校的）"}

    result = {"status": "ERROR", "rec_title": None, "record_id": None, "file": None, "score": 0.0}
    ensure_edge_running()
    tab_id = _open_ebsco_tab(opid)
    if not tab_id:
        result["detail"] = "cannot open EBSCO tab (Edge debug session down?)"
        return result

    try:
        ws = _tab_ws(tab_id)
        if not ws:
            result["detail"] = "tab websocket url lost"
            return result

        # ---- Step 1: 三级检索（DOI → TI 全名 → TI 短语，命中即止）----
        # 实测（2026-10-22，IJOC 10.1287/ijoc.2024.1010）：EBSCO 布尔 TI 对长标题
        # （100+ 字符、多连字符复合词）会 0 命中，而同一篇论文用 DOI 查询、
        # TI 前 9 词短语都能命中。三级依次尝试；命中后仍过 Step 2 相似度门禁，
        # 宽松查询不会带来错配下载。
        def _search_once(query: str):
            payload = {
                "advancedSearchStrategy": "SMART_TEXT_FAILOVER",
                "query": query,
                "autoCorrect": False,
                "profileIdentifier": opid,
                "expanders": [],
                "filters": [{"id": "databases", "values": dbs, "type": "LIMITER"}],
                "searchMode": "boolean",
                "sort": "relevance",
                "isNovelistEnabled": False,
                "includePlacards": False,
                "offset": 0,
                "count": 5,
                "highlightTag": "mark",
                "userDirectAction": True,
            }
            js_search = (_JS_SEARCH_TPL
                         .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
                         .replace("__SEARCH_URL__", f"'{SEARCH_API}'"))
            return _eval_json(ws, js_search)

        queries = []
        if doi:
            queries.append(str(doi).strip())
        queries.append(f'TI("{title}")')
        queries.append(f'TI("{" ".join(title.split()[:9])}")')
        items, used_q = [], ""
        for query in queries:
            if not query.strip('TI()" '):
                continue
            items = _search_once(query) or []
            used_q = query
            if items:
                break
        if not items:
            result["status"] = "NO_RESULT"
            result["detail"] = f'{used_q[:70]} no record（DOI/TI 全名/TI 短语三级均未命中）'
            return result

        # ---- Step 2: 标题相似度门禁（防"相似但错误"的错配下载）----
        best, best_score = None, 0.0
        for it in items:
            s = title_similarity(title, str(it.get("title", "")))
            if s > best_score:
                best, best_score = it, s
        result["record_id"] = best.get("id")
        result["rec_title"] = re.sub(r"<[^>]+>", "", str(best.get("title", ""))).strip()
        result["score"] = round(best_score, 3)
        if best_score < threshold:
            result["status"] = "MISMATCH"
            result["detail"] = f"best match {best_score:.2f} < {threshold}; refuse to download"
            return result

        # ---- Step 3: fulltext API → 签名直链 ----
        ft_url = FULLTEXT_API_TPL.format(rid=best["id"], opid=opid)
        js_ft = _JS_FULLTEXT_TPL.replace("__FT_URL__", json.dumps(ft_url))
        ft = _eval_json(ws, js_ft)
        if not ft or not ft.get("ok"):
            # 404 = 机构订阅未覆盖该 PDF（有记录≠有全文，必须分级报告）
            result["status"] = "NO_PDF"
            result["detail"] = f"fulltext api: {str(ft)[:120]}"
            return result

        signed_url = ft["url"]  # 一次性使用，不缓存

        # ---- Step 4: 下载 + 验证 PDF 文件头（防假 PDF 挑战页）----
        # 判据与 radar_fetcher._is_pdf_bytes 同源：`%PDF-` 前允许空白/控制字节。
        # 严格 startswith 会把**已经拿到手**的合法 PDF 当挑战页丢掉，实测合法样本
        # 首字节就是 b'\n%PDF-1.5'。
        req = urllib.request.Request(signed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if not _is_pdf_bytes(data) or len(data) < min_bytes:
            result["status"] = "BLOCKED"
            result["detail"] = f"not a valid PDF (head={data[:8]!r}, {len(data)} bytes)"
            return result

        # ---- Step 5: 统一入库 <文献库>/literature/（与库内现有文件 md5 去重）----
        safe = re.sub(r'[\/:*?"<>|]', "", title)[:75].strip() or best["id"]
        target = out_dir / f"{safe}.pdf"
        if target.exists():  # 同名先比内容
            import hashlib
            if hashlib.md5(target.read_bytes()).hexdigest() == hashlib.md5(data).hexdigest():
                result["status"] = "OK"
                result["file"] = str(target)
                result["detail"] = "duplicate skipped (md5 identical)"
                return result
            target = out_dir / f"{safe}_{best['id']}.pdf"
        target.write_bytes(data)
        result["status"] = "OK"
        result["file"] = str(target)
        result["detail"] = f"{len(data)} bytes"
        return result

    except Exception as e:
        result["status"] = "ERROR"
        result["detail"] = f"{type(e).__name__}: {str(e)[:150]}"
        return result


# ---------------------------------------------------------------- CLI ----
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="EBSCO UTD paper PDF fetcher")
    ap.add_argument("--title", required=True, help="exact paper title")
    ap.add_argument("--issn", default=None, help="container journal ISSN (routing gate)")
    ap.add_argument("--out", default=None, help="output dir (default: <library>/literature)")
    args = ap.parse_args()

    res = fetch_pdf_via_ebsco(args.title, out_dir=Path(args.out) if args.out else None, issn=args.issn)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    raise SystemExit(0 if res.get("status") == "OK" else 1)
