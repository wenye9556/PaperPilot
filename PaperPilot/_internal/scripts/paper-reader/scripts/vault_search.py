#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vault_search.py — 在文献库 vault 里搜笔记, 返回排序候选清单 (0 LLM, 0 token)

用法:
    python vault_search.py "无人机 能耗"              # 关键词(空格分隔, 中文/英文混用)
    python vault_search.py "TSP-D approximation" --top 15
    python vault_search.py "风电" --topic 海上风电运维巡检   # 限定主题书架
    python vault_search.py --recent 10                 # 最近 extracted_on 的 10 篇

打分: 每个查询词在 字段/小节 的命中加权——
    文件名×3  标题行×4  tags×3  scenario×2  一句话×2  创新点×1.5  关联×1  正文×0.3
输出: markdown 候选清单(按分排序): [[笔记名]] `类型` — 一句话 · 主题
设计给 agent 的"文献路由"用: 用户说"找文献/vault里有没有…" → 先跑本脚本出清单,
用户点名哪篇再展开全文, 不整篇乱读。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# Windows 下 Python 默认用活动代码页解码 argv, 中文关键词会变乱码;
# 非 UTF-8 模式启动时自我重启为 UTF-8 模式
if os.name == "nt" and sys.flags.utf8_mode != 1:
    os.execv(sys.executable, [sys.executable, "-X", "utf8", os.path.abspath(__file__)]
             + sys.argv[1:])

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
CONFIG_PATH = SKILL_DIR / "config.json"

TYPE_VALUES = ("方法类", "综述类", "实证类", "理论类")

# 同义词表种子: 首次运行写入 vault/_meta/同义词表.txt, 用户可自由增删改
SYN_SEED = """# 中英同义词表: 一行一组, = 分隔, 可中英混写; 命中任一词 = 命中全组
# 改完保存即生效, 无需重启
无人机 = UAV = drone
无人艇 = USV = unmanned surface vessel
母船 = mothership
风电 = wind farm = offshore wind
能耗 = energy consumption = energy model
鲁棒 = robust
不确定性 = uncertainty
时变 = time-varying = time-dependent
配送 = delivery = last-mile
路径 = routing = route = path
调度 = scheduling = schedule
启发式 = heuristic = metaheuristic
精确算法 = exact method = exact
分解 = decomposition
综述 = review = survey
车辆路径 = VRP = vehicle routing problem
旅行商 = TSP = traveling salesman
时窗 = time window = time windows
应急 = emergency = humanitarian = disaster
港口 = port = terminal
轨迹 = trajectory
预测 = prediction = forecast
强化学习 = reinforcement learning = RL = DRL
图神经网络 = graph neural network = GNN
两阶段 = two-stage
选址 = location
海上 = maritime = marine = offshore
巡检 = inspection = maintenance
近似比 = approximation ratio
列生成 = column generation
分支定界 = branch and bound = branch-and-bound
动态规划 = dynamic programming = DP
"""


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for ln in m.group(1).splitlines():
        km = re.match(r"^([a-zA-Z_]+):\s*(.*)$", ln)
        if km:
            fm[km.group(1)] = km.group(2).strip().strip('"').strip("'")
        elif ln.strip().startswith("- ") and fm:
            last = list(fm)[-1]
            fm[last] = (fm[last] + ", " + ln.strip()[2:]).strip(", ")
    return fm


def get_section(body, name):
    m = re.search(rf"^##\s[^#\n]*{re.escape(name)}[^#\n]*$", body, re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^##\s", body[start:], re.MULTILINE)
    return body[start:nxt.start() + start] if nxt else body[start:]


def index_note(md_path):
    text = md_path.read_text(encoding="utf-8", errors="replace")
    fm, body = (parse_frontmatter(text), text.split("---", 2)[-1]
                if text.startswith("---") else text)
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines()
                  if ln.startswith("# ")), md_path.stem)
    one = get_section(body, "一句话")
    one_line = next((ln.strip() for ln in one.splitlines()
                     if ln.strip() and not ln.strip().startswith("#")), "")
    one_line = re.sub(r"^[>*\-]+\s*", "", one_line)[:150]
    if not one_line:
        one_line = fm.get("scenario", "")[:150]
    return {
        "stem": md_path.stem,
        "title": title,
        "tags": fm.get("tags", ""),
        "type": fm.get("type", ""),
        "scenario": fm.get("scenario", ""),
        "one": one_line,
        "date": fm.get("extracted_on", ""),
        # 检索字段(带权重)
        "_fields": {
            "name": (md_path.stem, 3.0),
            "title": (title, 4.0),
            "tags": (fm.get("tags", ""), 3.0),
            "scenario": (fm.get("scenario", ""), 2.0),
            "one": (one_line, 2.0),
            "innov": (get_section(body, "创新点"), 1.5),
            "rel": (get_section(body, "与你课题的关联"), 1.0),
            "body": (body, 0.3),
        },
    }


def load_synonyms(vault):
    """读 vault/_meta/同义词表.txt(不存在则写入种子)。返回 [[词, 词, ...], ...]。"""
    p = vault / "_meta" / "同义词表.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SYN_SEED, encoding="utf-8")
        print(f"[OK] 同义词表已初始化: {p}(可自由编辑)")
    groups = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip().lower() for x in ln.split("=") if x.strip()]
        if len(parts) >= 2:
            groups.append(parts)
    return groups


def expand_tokens(tokens, groups):
    """token → 同义变体集合(含自身)。"""
    out = {}
    for t in tokens:
        variants = {t}
        for g in groups:
            if t in g:
                variants.update(g)
        out[t] = sorted(variants)
    return out


def score(note, tokens, expanded=None):
    s = 0.0
    hit_fields = []
    for fname, (text, w) in note["_fields"].items():
        tl = text.lower()
        for tok in tokens:
            variants = (expanded or {}).get(tok, [tok])
            if any(v in tl for v in variants):
                s += w
                if fname not in hit_fields:
                    hit_fields.append(fname)
    return s, hit_fields


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*", help="关键词(空格分隔)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--topic", default=None, help="限定主题(书架名)")
    ap.add_argument("--recent", type=int, default=0, help="按提取日期取最近 N 篇(忽略 query)")
    args = ap.parse_args()

    cfg = load_config()
    vault = Path(cfg["vault"])
    # Obsidian URI 直链配置（vault 根是文献库目录，papers 在 vault/papers/ 下）
    # URI 格式: obsidian://open?vault=文献库&file=vault/papers/<stem>
    vault_root = vault.parent  # 文献库根目录
    vault_name = vault_root.name  # "文献库"
    uri_prefix = f"obsidian://open?vault={vault_name}&file=vault/papers/"

    papers = sorted((vault / "papers").glob("*.md"))
    notes = [index_note(p) for p in papers]

    if args.topic:
        notes = [n for n in notes if args.topic.lower() in n["tags"].lower()]

    if args.recent:
        notes.sort(key=lambda n: n["date"], reverse=True)
        picked = notes[:args.recent]
        print(f"# 最近 {len(picked)} 篇(按 extracted_on)" + (f" · 主题[{args.topic}]" if args.topic else ""))
    else:
        if not args.query:
            eprint("给关键词, 或用 --recent N / --topic")
            return 1
        tokens = [t.lower() for part in args.query for t in part.split() if t.strip()]
        groups = load_synonyms(vault)
        expanded = expand_tokens(tokens, groups)
        n_syn = sum(len(v) - 1 for v in expanded.values())
        if n_syn:
            print(f"(同义词扩展 +{n_syn} 个变体)")
        scored = [(score(n, tokens, expanded), n) for n in notes]
        scored = [x for x in scored if x[0][0] > 0]
        scored.sort(key=lambda x: -x[0][0])
        picked = [n for _, n in scored[:args.top]]
        print(f"# 命中 {len(scored)} 篇, 显示 top {len(picked)}"
              + (f" · 主题[{args.topic}]" if args.topic else ""))

    for n in picked:
        raw = n["tags"]
        raw = "" if raw.lower() in ("null", "none", "~", "") else raw
        tags = [t.strip() for t in raw.split(",") if t.strip() and t.strip() not in TYPE_VALUES]
        tag_s = "·".join(tags[:3]) or "待分类"
        # 双链接输出: wikilink (Obsidian 内) + obsidian:// URI (终端/聊天可点击)
        print(f"- **[[{n['stem']}]]** `{n['type'] or '?'}` — {n['one'] or '(待补一句话)'} 〔{tag_s}〕")
        print(f"  → [{uri_prefix}{n['stem']}]({uri_prefix}{n['stem']})")
    if not picked:
        print("- (无命中; 换关键词, 或用 --recent 看最近入库)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
