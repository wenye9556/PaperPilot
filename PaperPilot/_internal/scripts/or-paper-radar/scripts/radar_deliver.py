#!/usr/bin/env python3
"""Stage 3 of or-paper-radar: delivery (report + vault cards + history + optional push).

Hard gates (daily-push discipline, adapted):
- every article must carry score_status == "evidence_reviewed" with full
  component scores; the weighted recommendation is recomputed server-side and
  a mismatch > 2 points refuses delivery
- only articles >= profile.min_score are recommended; fewer than 5 or even
  zero recommendations are legal — the threshold is never lowered to fill quota
- push history (dedup) is updated ONLY after confirmed delivery; a failed
  WeChat push leaves history untouched so the next run retries
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from radar_common import (  # noqa: E402
    PAPER_READER_CONFIG, article_keys, atomic_write_json, atomic_write_text,
    load_history, load_profile, save_history, session, today_run_dir, vault_path,
)

WEIGHTS = {"relevance": 0.40, "novelty": 0.20, "quality": 0.15, "methodology": 0.15, "inspiration": 0.10}
SERVERCHAN = "https://sctapi.ftqq.com/{key}.send"


def validate(reviewed: dict) -> list[str]:
    errors = []
    for i, art in enumerate(reviewed.get("articles", [])):
        label = f"articles[{i}] {art.get('title', '')[:40]}"
        if art.get("score_status") != "evidence_reviewed":
            errors.append(f"{label}: score_status 不是 evidence_reviewed（禁止未复核直接推荐）")
            continue
        comp = art.get("component_scores") or {}
        missing = [k for k in WEIGHTS if k not in comp]
        if missing:
            errors.append(f"{label}: component_scores 缺 {missing}")
            continue
        bad = [k for k in WEIGHTS if not isinstance(comp[k], (int, float)) or not 0 <= comp[k] <= 100]
        if bad:
            errors.append(f"{label}: component_scores 越界 {bad}")
            continue
        recomputed = sum(comp[k] * w for k, w in WEIGHTS.items())
        stated = art.get("recommendation_score")
        if not isinstance(stated, (int, float)) or abs(stated - recomputed) > 2:
            errors.append(f"{label}: recommendation_score={stated} 与加权重算 {recomputed:.1f} 不符")
        if not art.get("title"):
            errors.append(f"{label}: 缺 title")
    return errors


def card_filename(art: dict, run_date: str) -> str:
    title = art.get("chinese_title") or art.get("title") or "untitled"
    slug = re.sub(r"[\\/:*?\"<>|#^\[\]]", " ", title)
    slug = re.sub(r"\s+", " ", slug).strip()[:40]
    return f"{run_date}-{slug}.md"


def build_card(art: dict, run_date: str) -> str:
    comp = art.get("component_scores", {})
    tags = ["radar"] + (art.get("tags") or [])[:6]
    lines = [
        "---",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        f"doi: {art.get('doi') or ''}",
        f"journal: {art.get('journal') or ''}",
        f"pub_date: {art.get('publication_date') or ''}",
        f"score: {art.get('recommendation_score')}",
        "radar_date: " + run_date,
        "---",
        "",
        f"# {art.get('chinese_title') or art.get('title')}",
        "",
        f"**原题**：{art.get('title')}",
        "",
        f"**出处**：{art.get('journal') or '未知来源'} · {art.get('publication_date') or '未知日期'}"
        + (" · 预印本（未经正式同行评审）" if art.get("is_preprint") else ""),
        f"**链接**：{art.get('url') or art.get('doi') or ''}",
        f"**推荐指数**：{art.get('recommendation_score')}"
        f"（相关 {comp.get('relevance')} / 新颖 {comp.get('novelty')} / 质量 {comp.get('quality')}"
        f" / 方法 {comp.get('methodology')} / 启发 {comp.get('inspiration')}）",
        "",
        f"**为什么值得读**：{art.get('why_worth_reading')}",
        "",
        "**核心发现**：",
    ]
    lines += [f"- {f}" for f in art.get("core_findings", [])]
    insp = art.get("research_inspiration")
    if insp:
        lines += ["", "**研究启发**："]
        if isinstance(insp, dict):
            for k, v in insp.items():
                lines.append(f"- {v}")
        else:
            lines.append(f"- {insp}")
    if art.get("needs_verification"):
        lines += ["", "> [!] 存在需人工核实项（见雷达报告）"]
    lines += [
        "",
        "---",
        f"来源：or-paper-radar {run_date}。要精读：用 ref-downloader 拉 PDF 丢进 literature\\，"
        "再对 paper-reader 说『读这篇』。",
        "",
    ]
    return "\n".join(lines)


def build_report(reviewed: dict, picked: list[dict], skipped: list[dict], run_date: str, window: dict) -> str:
    n_total = len(reviewed.get("articles", []))
    lines = [
        f"# OR 文献雷达 · {run_date}",
        "",
        f"窗口：{window.get('from', '?')} 起 {window.get('days', '?')} 天 · 复核 {n_total} 篇 · "
        f"推荐 {len(picked)} 篇" + (f"（{len(skipped)} 篇低于阈值不推荐）" if skipped else ""),
        "",
    ]
    if not picked:
        lines += ["**本期零推荐**：没有论文同时满足证据充分与评分阈值。不降阈值凑数。", ""]
    if picked:
        lines += ["## 最值得阅读 TOP3", ""]
        for i, art in enumerate(picked[:3], 1):
            lines.append(f"{i}. **{art.get('chinese_title') or art.get('title')}**"
                         f"（{art.get('recommendation_score')} 分）— {art.get('why_worth_reading')}")
        lines += ["", "---", ""]
    for art in picked:
        comp = art.get("component_scores", {})
        lines += [
            f"## {art.get('chinese_title') or art.get('title')}",
            "",
            f"- **英文原题**：{art.get('title')}",
            f"- **出处**：{art.get('journal')} · {art.get('publication_date')} · {art.get('doi') or art.get('url')}"
            + (" · ⚠ 预印本，未经正式同行评审" if art.get("is_preprint") else ""),
            f"- **评分**：{art.get('recommendation_score')} = 相关 {comp.get('relevance')}×0.40 + "
            f"新颖 {comp.get('novelty')}×0.20 + 质量 {comp.get('quality')}×0.15 + "
            f"方法 {comp.get('methodology')}×0.15 + 启发 {comp.get('inspiration')}×0.10",
            f"- **为什么值得读**：{art.get('why_worth_reading')}",
            "**核心发现**：",
        ]
        lines += [f"  - {f}" for f in art.get("core_findings", [])]
        insp = art.get("research_inspiration")
        if insp:
            lines.append("**研究启发**：")
            if isinstance(insp, dict):
                lines += [f"  - {v}" for v in insp.values()]
            else:
                lines.append(f"  - {insp}")
        if art.get("top3_reason"):
            lines.append(f"- **TOP3 理由**：{art.get('top3_reason')}")
        if art.get("needs_verification"):
            lines.append("- **需核实**：是")
        lines.append("")
    ideas = reviewed.get("research_ideas") or []
    if ideas:
        lines += ["## 本期研究思路", ""] + [f"- {idea}" for idea in ideas] + [""]
    if skipped:
        lines += ["## 低于阈值未推荐（存档）", ""] + [
            f"- {a.get('title')}（{a.get('recommendation_score')} 分）" for a in skipped
        ] + [""]
    lines += ["---", "*微信推送：未请求或未配置（本报告为本地交付）。*", ""]
    return "\n".join(lines)


def serverchan_push(key: str, title: str, digest: str) -> tuple[bool, str]:
    try:
        sess = session()
        r = sess.post(SERVERCHAN.format(key=key), data={"title": title, "desp": digest}, timeout=30)
        if r.status_code == 200 and r.json().get("code") == 0:
            return True, "ok"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewed", required=True, type=Path, help="AI 复核后的 reviewed_articles.json")
    ap.add_argument("--report", type=Path, default=None, help="报告输出路径")
    ap.add_argument("--vault", type=Path, default=None, help="覆盖 vault 路径（默认读 paper-reader config）")
    ap.add_argument("--push", action="store_true", help="授权微信推送（Server酱，读 paper-reader 配置）")
    ap.add_argument("--skip-push", action="store_true", help="本地交付：不推送但记录历史（报告已在会话展示）")
    args = ap.parse_args()

    reviewed = json.loads(args.reviewed.read_text(encoding="utf-8"))
    errors = validate(reviewed)
    if errors:
        print(json.dumps({"refused": True, "errors": errors}, ensure_ascii=False, indent=2))
        return 1

    profile = load_profile()
    min_score = float(profile.get("min_score", 70))
    max_push = int(profile.get("max_push", 10))

    articles = reviewed.get("articles", [])
    picked = [a for a in articles if a.get("recommendation_score", 0) >= min_score]
    picked.sort(key=lambda a: -a["recommendation_score"])
    picked = picked[:max_push]
    skipped = [a for a in articles if a not in picked]

    run_date = datetime.now().date().isoformat()
    window = reviewed.get("window") or {}

    report = build_report(reviewed, picked, skipped, run_date, window)
    report_path = args.report or (args.reviewed.parent / "radar-report.md")
    atomic_write_text(report_path, report)

    # ---- vault cards ----
    vault = args.vault or vault_path(profile)
    cards_written = []
    if vault and picked:
        radar_dir = vault / "radar"
        radar_dir.mkdir(parents=True, exist_ok=True)
        for art in picked:
            path = radar_dir / card_filename(art, run_date)
            atomic_write_text(path, build_card(art, run_date))
            cards_written.append(path.name)
        index = radar_dir / "雷达总览.md"
        head = "# 雷达总览\n\n"
        desc = "or-paper-radar 推荐流。精读流程见卡片脚注。\n"
        existing = index.read_text(encoding="utf-8") if index.is_file() else head + desc
        if not existing.startswith(head):
            existing = head + existing
        pos = existing.find(desc)
        pos = pos + len(desc) if pos != -1 else len(head)
        new_lines = "".join(
            f"- [[{name[:-3]}]] {p.get('recommendation_score')}分 {p.get('chinese_title') or (p.get('title') or '')[:40]}\n"
            for name, p in zip(cards_written, picked)
        )
        atomic_write_text(index, existing[:pos] + "\n" + new_lines + existing[pos:])

    # ---- push (optional) ----
    push_status = "not-requested"
    if args.push:
        key = ""
        if PAPER_READER_CONFIG.is_file():
            try:
                key = (json.loads(PAPER_READER_CONFIG.read_text(encoding="utf-8"))
                       .get("push", {}).get("serverchan_key", ""))
            except (json.JSONDecodeError, OSError):
                key = ""
        if not key:
            print(json.dumps({"refused": False, "push": "未配置：paper-reader config.json 缺 serverchan_key"}, ensure_ascii=False))
        else:
            title = f"OR雷达 {run_date}：推荐 {len(picked)} 篇" if picked else f"OR雷达 {run_date}：零推荐"
            digest = "\n".join(
                f"{i}. {a.get('chinese_title') or a.get('title')}（{a['recommendation_score']}）"
                for i, a in enumerate(picked[:5], 1)
            ) or "本期无满足阈值论文"
            ok, detail = serverchan_push(key, title, digest)
            push_status = "ok" if ok else f"failed: {detail}"

    # ---- history: only record on confirmed delivery ----
    history_ok = (push_status == "ok") or args.skip_push or not args.push
    history = load_history()
    n_recorded = 0
    if history_ok:
        for art in picked:
            keys = article_keys(art)
            if keys:
                history.append({
                    "keys": keys,
                    "title": art.get("title"),
                    "doi": art.get("doi"),
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "run_date": run_date,
                    "score": art.get("recommendation_score"),
                })
                n_recorded += 1
        save_history(history)

    print(json.dumps({
        "delivered": True,
        "recommended": len(picked),
        "below_threshold": len(skipped),
        "report": str(report_path),
        "vault_cards": cards_written,
        "push": push_status,
        "history_recorded": n_recorded if history_ok else 0,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
