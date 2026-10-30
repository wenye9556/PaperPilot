#!/usr/bin/env python3
"""Stage 1 of or-paper-radar: multi-source collection into a review queue.

Supports:
- Default periodic radar (journals + preset queries)
- Ad-hoc scoped topic search (--query / -q "无人机路径规划" or "drone routing")
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from radar_common import (  # noqa: E402
    article_keys, load_history, load_profile, prescore, session,
    today_run_dir, window_from,
)

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/journals/{issn}/works"


def abstract_from_inverted(inv: dict | None) -> str:
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for idx in idxs:
            positions.append((idx, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def from_openalex(work: dict) -> dict:
    loc = work.get("primary_location") or {}
    source = (loc.get("source") or {}) or {}
    best_oa = work.get("best_oa_location") or {}
    arxiv_id = None
    landing = work.get("doi") or (best_oa.get("landing_page_url") if best_oa else None) or loc.get("landing_page_url")
    pdf = best_oa.get("pdf_url") if best_oa else None
    host = source.get("display_name") or ""
    if "arxiv" in host.lower():
        m = re.search(r"arxiv\.org[/\\](?:abs|pdf)/([0-9.]+)", str(landing) + str(pdf or ""))
        arxiv_id = m.group(1) if m else None
    return {
        "doi": work.get("doi"),
        "title": work.get("title"),
        "abstract": abstract_from_inverted(work.get("abstract_inverted_index")),
        "journal": source.get("display_name"),
        "publication_date": work.get("publication_date"),
        "type": work.get("type"),
        "url": landing,
        "pdf_url": pdf,
        "arxiv_id": arxiv_id,
        "is_preprint": work.get("type") == "preprint" or bool(arxiv_id),
        "source": "openalex",
    }


def clean_crossref_title(raw: list | str | None) -> str:
    if not raw:
        return ""
    t = raw[0] if isinstance(raw, list) else str(raw)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def from_crossref(item: dict) -> dict:
    issued = (
        item.get("published-online")
        or item.get("published-print")
        or item.get("issued")
        or {}
    )
    parts = issued.get("date-parts", [[]])[0]
    pub_date = None
    if len(parts) >= 1:
        pub_date = f"{parts[0]:04d}"
        if len(parts) >= 2:
            pub_date += f"-{parts[1]:02d}"
            if len(parts) >= 3:
                pub_date += f"-{parts[2]:02d}"

    links = item.get("link", []) or []
    pdf = None
    for link in links:
        if "pdf" in (link.get("content-type") or ""):
            pdf = link.get("URL")
            break

    journal = (item.get("container-title") or [""])[0]
    return {
        "doi": f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else None,
        "title": clean_crossref_title(item.get("title")),
        "abstract": clean_crossref_title(item.get("abstract")),
        "journal": journal,
        "publication_date": pub_date,
        "type": item.get("type"),
        "url": item.get("URL"),
        "pdf_url": pdf,
        "arxiv_id": None,
        "is_preprint": False,
        "source": "crossref",
    }


def fetch_json(sess, url: str, params: dict, label: str, stats: dict) -> dict | None:
    try:
        r = sess.get(url, params=params, timeout=20)
        stats[f"{label}_requests"] = stats.get(f"{label}_requests", 0) + 1
        if r.status_code == 200:
            return r.json()
        stats[f"{label}_errors"] = stats.get(f"{label}_errors", 0) + 1
    except Exception:
        stats[f"{label}_exceptions"] = stats.get(f"{label}_exceptions", 0) + 1
    return None


def collect(args) -> int:
    profile = load_profile()
    days = args.days if args.days is not None else int(profile.get("window_days", 30))
    window = window_from(days)
    sess = session()

    pool: dict[str, dict] = {}
    stats: dict[str, int] = {}
    exclude = set(profile.get("exclude_types", ["erratum", "paratext", "editorial"]))
    dropped_types: dict[str, int] = {}

    def merge(article: dict):
        atype = (article.get("type") or "").lower()
        if atype in exclude:
            dropped_types[atype] = dropped_types.get(atype, 0) + 1
            return
        if not article.get("title"):
            return
        for key in article_keys(article):
            if key in pool:
                existing = pool[key]
                if len(article.get("abstract") or "") > len(existing.get("abstract") or ""):
                    article["_also_from"] = existing["source"]
                    for k in article_keys(existing):
                        pool.pop(k, None)
                    for k in article_keys(article):
                        pool[k] = article
                else:
                    existing.setdefault("_also_from", article["source"])
                return
        for key in article_keys(article):
            pool[key] = article

    scoped_query = (args.query or "").strip()
    target_queries = [scoped_query] if scoped_query else profile.get("search_queries", [])

    if scoped_query:
        print(f"[*] 启动指定范围定制检索: '{scoped_query}' (时间窗口: {window} 至今)")
        # 1. 在 OpenAlex 全文/摘要中检索指定范围
        data = fetch_json(sess, OPENALEX, {
            "filter": f'from_publication_date:{window},title_and_abstract.search:"{scoped_query}"',
            "per-page": 200, "sort": "cited_by_count:desc",
        }, "openalex", stats)
        if data:
            for work in data.get("results", []):
                merge(from_openalex(work))

        # 2. 同时在核心运筹顶刊中限制该关键词检索
        for journal in profile.get("journals", []):
            data = fetch_json(sess, OPENALEX, {
                "filter": f'from_publication_date:{window},locations.source.issn:{journal["issn"]},title_and_abstract.search:"{scoped_query}"',
                "per-page": 100, "sort": "publication_date:desc",
            }, "openalex", stats)
            if data:
                for work in data.get("results", []):
                    merge(from_openalex(work))
    else:
        # 默认模式：遍历所有配置期刊与常规检索词
        for journal in profile.get("journals", []):
            data = fetch_json(sess, OPENALEX, {
                "filter": f"from_publication_date:{window},locations.source.issn:{journal['issn']},type:article",
                "per-page": 200, "sort": "publication_date:desc",
            }, "openalex", stats)
            if data:
                for work in data.get("results", []):
                    merge(from_openalex(work))

        for query in target_queries:
            data = fetch_json(sess, OPENALEX, {
                "filter": f'from_publication_date:{window},title_and_abstract.search:"{query}"',
                "per-page": 200, "sort": "cited_by_count:desc",
            }, "openalex", stats)
            if data:
                for work in data.get("results", []):
                    merge(from_openalex(work))

        for journal in profile.get("journals", []):
            data = fetch_json(sess, CROSSREF.format(issn=journal["issn"]), {
                "filter": f"from-pub-date:{window}", "rows": 100, "sort": "published", "order": "desc",
            }, "crossref", stats)
            if data:
                for item in (data.get("message") or {}).get("items", []):
                    merge(from_crossref(item))

    history = load_history()
    pushed_keys: set[str] = set()
    for rec in history:
        pushed_keys.update(rec.get("keys", []))

    candidates = []
    skipped_pushed = 0
    seen: set[int] = set()
    for article in pool.values():
        if id(article) in seen:
            continue
        seen.add(id(article))
        keys = article_keys(article)
        if any(k in pushed_keys for k in keys):
            skipped_pushed += 1
            continue
        score, hits = prescore(article, target_queries)
        article["keyword_hits"] = hits
        article["prescore"] = score
        candidates.append(article)

    candidates.sort(key=lambda a: (-a["prescore"], a.get("publication_date") or "", a.get("title") or ""))
    limit = int(profile.get("review_queue_limit", 60))
    queue, overflow = candidates[:limit], len(candidates) - limit

    queue_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"from": window, "days": days},
        "scoped_query": scoped_query if scoped_query else None,
        "stats": {
            "pooled_unique": len(candidates) + skipped_pushed,
            "skipped_already_pushed": skipped_pushed,
            "queue_limit": limit,
            "overflow_beyond_limit": max(overflow, 0),
            "dropped_types": dropped_types,
            **stats,
        },
        "articles": [
            {k: v for k, v in a.items() if k in (
                "doi", "title", "abstract", "journal", "publication_date", "type",
                "url", "pdf_url", "arxiv_id", "is_preprint", "source", "_also_from",
                "keyword_hits", "prescore",
            )}
            for a in queue
        ],
    }

    out = Path(args.out) if args.out else today_run_dir() / "review_queue.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(queue_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "queue": len(queue), "unique_candidates": len(candidates) + skipped_pushed,
        "skipped_pushed": skipped_pushed, "overflow": max(overflow, 0),
        "with_abstract": sum(1 for a in queue if a.get("abstract")),
        "preprints": sum(1 for a in queue if a.get("is_preprint")),
        "output": str(out),
    }, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="回看窗口天数（默认取 profile.window_days）")
    ap.add_argument("--query", "-q", default=None, help="指定文献检索范围/主题关键词 (例如: 'drone routing' 或 '无人机路径规划')")
    ap.add_argument("--out", default=None, help="review_queue.json 输出路径")
    args = ap.parse_args()
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
