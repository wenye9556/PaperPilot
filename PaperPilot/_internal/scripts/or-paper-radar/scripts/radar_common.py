#!/usr/bin/env python3
"""Shared helpers for or-paper-radar (profile, session, dedup keys, atomic IO)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import urllib3

SKILL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = SKILL_ROOT / "config" / "radar_profile.json"
HISTORY_PATH = SKILL_ROOT / "data" / "pushed_articles.json"
RUNS_DIR = SKILL_ROOT / "data" / "runs"
PAPER_READER_CONFIG = SKILL_ROOT.parent / "paper-reader" / "config.json"

DEFAULT_PROFILE = {
    "configured": False,
    "window_days": 7,
    "min_score": 70,
    "max_push": 10,
    "review_queue_limit": 60,
    "search_queries": [],
    "journals": [],
    "exclude_types": [],
    "vault": None,
}


def _verify_ssl() -> bool:
    """证书校验开关，默认 **开启**。

    与 core.ssl_verify() 保持同一套语义：settings.json 里
    net.verify_ssl=false 时才关闭（受限网络下的显式退让），否则一律校验。
    """
    env = os.environ.get("PAPERPILOT_NET_VERIFY_SSL", "").strip().lower()
    if env in ("0", "false", "no"):
        return False
    if env in ("1", "true", "yes"):
        return True
    try:
        data = json.loads(PAPER_READER_CONFIG.read_text(encoding="utf-8"))
        return bool(data.get("net", {}).get("verify_ssl", True))
    except Exception:
        return True


_VERIFY_SSL = _verify_ssl()
if not _VERIFY_SSL:
    # 只在真正关闭校验时静音告警，否则让 urllib3 的告警正常浮现
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def session() -> requests.Session:
    """Outbound session that bypasses the local proxy (it blocks HTTPS here)."""
    s = requests.Session()
    s.trust_env = False
    s.verify = _VERIFY_SSL
    s.headers["User-Agent"] = "or-paper-radar/0.1 (mailto:radar@example.com)"
    return s


def load_profile() -> dict:
    profile = dict(DEFAULT_PROFILE)
    if PROFILE_PATH.is_file():
        loaded = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        profile.update({k: v for k, v in loaded.items() if v is not None or k != "vault"})
    return profile


def vault_path(profile: dict) -> Path | None:
    if profile.get("vault"):
        p = Path(profile["vault"])
        return p if p.is_dir() else None
    if PAPER_READER_CONFIG.is_file():
        try:
            cfg = json.loads(PAPER_READER_CONFIG.read_text(encoding="utf-8"))
            p = Path(cfg.get("vault", ""))
            return p if p.is_dir() else None
        except (json.JSONDecodeError, OSError):
            return None
    return None


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + "-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + "-", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_history() -> list[dict]:
    if HISTORY_PATH.is_file():
        try:
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_history(records: list[dict]) -> None:
    atomic_write_json(HISTORY_PATH, records)


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi or None


def norm_title(title: str | None) -> str | None:
    if not title:
        return None
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower()) or None


def article_keys(article: dict) -> list[str]:
    """Identity keys in priority order: DOI > arXiv id > normalized title."""
    keys = []
    doi = norm_doi(article.get("doi"))
    if doi:
        keys.append("doi:" + doi)
    arxiv = article.get("arxiv_id")
    if arxiv:
        keys.append("arxiv:" + str(arxiv))
    title = norm_title(article.get("title"))
    if title:
        keys.append("title:" + title)
    return keys


def prescore(article: dict, queries: list[str]) -> tuple[int, list[str]]:
    """Deterministic keyword hint used ONLY for ordering the review queue."""
    text = ((article.get("title") or "") + " " + (article.get("abstract") or "")).lower()
    hits = [q for q in queries if q.lower() in text]
    return len(hits), hits


def window_from(days: int) -> str:
    return (date.today() - timedelta(days=days - 1)).isoformat()


def today_run_dir() -> Path:
    run = RUNS_DIR / date.today().isoformat()
    run.mkdir(parents=True, exist_ok=True)
    return run
