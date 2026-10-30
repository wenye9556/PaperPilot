#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
note_writer.py — 按 _精读模板.md 自动生成 / 回炉 / 体检 / 监听 vault 笔记（v2）

用法:
  # 1) 新论文精读(全精读): PDF → deep 提取 → LLM → 规范化 → R1-R12 校验 → 自动修复一轮 → 落 vault
  python note_writer.py --pdf "D:/papers/foo.pdf"

  # 2) 回炉: 旧 .md + PDF → 按模板重写
  python note_writer.py --pdf "..." --existing "vault/papers/Old.md" --rewrite

  # 3) 体检(0 token): 全库扫描, 产出 _meta/笔记体检报告.md（含内容级规则 R1-R12）
  python note_writer.py --audit-only

  # 4) 校验单篇(agent 手写笔记后的自检, 0 token):
  python note_writer.py --validate --note "vault/papers/Xxx.md"

  # 5) 扫描哪些 PDF 还没有笔记(0 token):
  python note_writer.py --scan

  # 6) 批量精读扫描到的新 PDF:
  python note_writer.py --auto [--yes] [--limit N]

  # 7) 监听模式(轮询新 PDF 并自动精读, Ctrl+C 停):
  python note_writer.py --watch [--interval 300]

LLM 配置: config.json 的 llm 节(base_url/model/api_key/...)，
          或环境变量 PAPER_READER_API_KEY(优先)。OpenAI 兼容 chat/completions。
综述自动识别: meta 文本含 review/survey/综述 等 → 自动 --with-litreview。
              可用 --with-litreview / --no-litreview 强制。

退出码: 0 成功; 1 参数错; 2 提取失败; 5 LLM 未配置; 6 LLM 调用失败;
        7 校验存在 FAIL(仅 --validate 模式)
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Windows 下 Python 默认用活动代码页解码 argv, 中文路径会变乱码;
# 非 UTF-8 模式启动时自我重启为 UTF-8 模式
if os.name == "nt" and sys.flags.utf8_mode != 1:
    os.execv(sys.executable, [sys.executable, "-X", "utf8", os.path.abspath(__file__)]
             + sys.argv[1:])

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
CONFIG_PATH = SKILL_DIR / "config.json"
PAPER_TOOL = HERE / "paper_tool.py"

DEFAULT_LLM = {
    "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "model": "glm-4.6",
    "api_key": "",
    "temperature": 0.3,
    "max_tokens": 12000,
    "timeout": 600,
}

# frontmatter 字段规范顺序(模板 v2); doi/aliases 可选, 其余必填
CANONICAL_ORDER = ["tags", "scenario", "type", "status", "data_open", "code_open",
                   "aliases", "date", "doi", "pdf", "extracted_on", "extracted_by"]
# 允许写"未提及"的字段(data_open/code_open 按模板是合法值)
MAY_SAY_UNMENTIONED = {"data_open", "code_open"}
# 任何字段出现这些值 = 占位符, 整行删
PLACEHOLDER_VALUES = {"null", "none", "~", "", "无", "nan", "未找到", "见原文", "缺失", "待补充"}

# ---------- 工具 ----------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def eprint(*a):
    print(*a, file=sys.stderr)


# ---------- PDF 提取 ----------

def call_paper_tool(pdf, outdir, mode="deep", with_litreview=False):
    """调 paper_tool.py 提取, 返回 transcript 路径。失败抛异常。"""
    cmd = [sys.executable, "-X", "utf8", str(PAPER_TOOL), pdf, "-o", outdir, "--mode", mode]
    if with_litreview:
        cmd.append("--with-litreview")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"paper_tool 失败 rc={p.returncode}\nSTDERR:\n{p.stderr}")
    for line in p.stdout.splitlines():
        if line.startswith("transcript="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"paper_tool 输出无 transcript 行\nSTDOUT:\n{p.stdout}")


SURVEY_PAT = re.compile(r"\b(review|survey|state[- ]of[- ]the[- ]art|taxonomy|systematic"
                        r" literature|bibliometric)\b|综述|研究综述", re.I)


def looks_like_survey(meta_text):
    """从标题+摘要(meta 前 800 字符)判断是否综述类。"""
    return bool(SURVEY_PAT.search(meta_text[:800]))


# ---------- LLM ----------

def llm_settings():
    cfg = load_config().get("llm", {}) or {}
    s = dict(DEFAULT_LLM)
    s.update({k: v for k, v in cfg.items() if v not in (None, "")})
    s["api_key"] = os.environ.get("PAPER_READER_API_KEY") or cfg.get("api_key") or ""
    return s


def call_llm(prompt, system=None, max_tokens=None):
    """OpenAI 兼容 chat/completions。429/5xx 退避重试 2 次。"""
    import requests  # 延迟导入, 体检/扫描模式不需要

    s = llm_settings()
    if not s["api_key"]:
        eprint("[LLM 未配置] 两种方式任选:")
        eprint("  1) 编辑 %s -> llm.api_key 填你的 key" % CONFIG_PATH)
        eprint("  2) 环境变量 PAPER_READER_API_KEY=<key>")
        eprint("  base_url 当前=%s model=%s" % (s["base_url"], s["model"]))
        sys.exit(5)

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": s["model"], "messages": msgs, "temperature": s["temperature"],
            "max_tokens": max_tokens or s["max_tokens"]}
    headers = {"Authorization": "Bearer %s" % s["api_key"],
               "Content-Type": "application/json"}

    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(s["base_url"], json=body, headers=headers,
                              timeout=s["timeout"], proxies={"http": None, "https": None})
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = "HTTP %d: %s" % (r.status_code, r.text[:300])
                wait = 15 * (attempt + 1)
                eprint("[..] %s, %ds 后重试(%d/2)" % (last_err.split(':')[0], wait, attempt + 1))
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not content:
                raise RuntimeError("LLM 返回空 content: %s" % json.dumps(data, ensure_ascii=False)[:400])
            return strip_code_fence(content)
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001 — 网络类异常统一重试
            last_err = str(e)
            if attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue
    raise RuntimeError("LLM 调用失败(重试耗尽): %s" % last_err)


def strip_code_fence(text):
    """模型偶尔用 ```markdown 包裹, 剥掉。"""
    t = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", t, re.DOTALL)
    return m.group(1).strip() if m else t


# ---------- prompt 组装 ----------

SYSTEM_PROMPT = """你是学术论文精读助手,严格按用户提供的模板输出 markdown 笔记。

铁律:
1. 不编造: DOI、链接、数字、页码、公式号——原文没有就**整行省略该字段**,绝不凑数、绝不写"未提及"(data_open/code_open 除外)。
2. 引用具体发现时, 标注 §X.Y / Table N / Fig M (p.P) 形式。
3. 输出语言: 中文为主, 术语/模型名/数据集/指标用英文反引号。
4. 所有数学符号/变量/公式用行内 LaTeX `$...$` 包裹(模板有对照表); 禁用块级 `$$`。
5. "与你课题的关联"必须锚定用户提供的"课题画像", 画像里没提的不猜。
6. 只输出笔记全文, 不要解释、不要道歉、不要 markdown 代码块包裹。
"""


def read_template(vault):
    p = vault / "_精读模板.md"
    if not p.exists():
        raise FileNotFoundError(f"模板不存在: {p}")
    return p.read_text(encoding="utf-8")


def read_profile(vault, cfg):
    p = vault / cfg.get("profile_file", "_我的课题.md")
    return p.read_text(encoding="utf-8") if p.exists() else "(用户尚未创建课题画像, 关联节按主题列表合理推断并标注待确认)"


def read_topics(vault, cfg, topics_file=None):
    p = Path(topics_file) if topics_file else vault / cfg.get("topics_file", "_研究主题.md")
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    return [ln[2:].strip() for ln in lines if ln.strip().startswith("- ")]


def build_prompt_for_new_paper(template_text, transcript, meta_text, topics, profile):
    return f"""# 任务
按下面的"模板"格式, 把论文**全精读**成 Obsidian 笔记。模板末尾的 R1-R12 是机器自动检查规则, 违反会被打回重写, 务必逐条对照。

# 模板(单一真相源)
{template_text}

# 我的课题画像(写"与你课题的关联"/"用途四问"必须锚定此文件)
{profile}

# 当前研究主题列表(分类标签从这里选, 不在列表归"待分类")
{json.dumps(topics, ensure_ascii=False, indent=2)}

# 论文首页元信息(标题/作者/摘要前段)
{meta_text}

# 论文正文精读稿
{transcript}

# 输出要求(与 R 规则对应)
1. frontmatter 字段顺序严格按模板; 没有值的字段整行省略
2. scenario 30-80 字, 塞数字/对比/边界
3. "一句话"独立成段, 跟标题不重复
4. Model / Experiments 节内每个发现带 [§X·Table/Fig Y] 出处
5. 创新点 3-5 条编号, 每条独立成句
6. 不足分"作者自述"和"我观察的"两类
7. "与你课题的关联" ≥ 2 段, 至少 1 段含 💎, 锚定课题画像
8. 证据锚索引表 11-13 条, 覆盖动机/模型/算法/关键数字/局限
9. 全部数学用 `$...$` 行内包裹; 禁块级 `$$`
10. 没编造, 原文没有就省略

直接输出笔记全文。"""


def build_prompt_for_rewrite(template_text, old_md, transcript, meta_text, profile):
    return f"""# 任务
用户已有旧版笔记, 请按模板回炉重写, 补全缺失字段, 修正格式, 升级证据。

# 模板(单一真相源, 末尾 R1-R12 为机器检查规则)
{template_text}

# 我的课题画像(写"与你课题的关联"必须锚定此文件)
{profile}

# 旧笔记(待回炉)
{old_md}

# 论文元信息
{meta_text}

# 论文正文精读稿
{transcript}

# 回炉要点
1. frontmatter: 补 scenario / aliases / pdf / extracted_by / date 等缺失字段, 顺序按模板
2. doi: 没找到就**整行省略**, 不写占位符
3. 正文: 把泛述替换为带 §X·Table/Fig 的具体发现
4. 创新点: 拆成 3-5 条编号独立句
5. 不足: 分作者自述 vs 我观察
6. **补强"与你课题的关联"节**(锚定课题画像, ≥2 段, ≥1 💎)
7. **补全"证据锚索引"表** 11-13 条
8. 数学全部 `$...$` 行内包裹, 禁 `$$`
9. PDF 链接: 同 vault 用 [[文件名.pdf]], 跨库用 file:///
10. **核心模型与算法严禁写'见原文'、'详见原文'或泛泛套话**：必须从正文提取出具体的决策变量、目标函数公式、关键约束公式($...$ 行内渲染)及算法流程步骤。

输出完整新笔记全文。"""


def build_repair_prompt(note_md, failures):
    lines = "\n".join("- [%s] %s" % (f["rule"], f["msg"]) for f in failures)
    return f"""# 任务
你刚生成的笔记未通过机器校验。逐条修复下面的问题, 输出**修复后的完整笔记全文**。
只改问题相关内容, 其余原样保留; 继续遵守模板全部铁律(不编造/数学`$...$`/出处标注)。

# 未通过的规则
{lines}

# 待修复笔记
{note_md}

直接输出修复后的完整笔记。"""


# ---------- frontmatter 解析 / 规范化 ----------

def parse_frontmatter_and_body(md_text):
    """分离 frontmatter 和正文, 返回 (fm_dict, body_str)。值可能是 str 或 list。"""
    if not md_text.startswith("---"):
        return {}, md_text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", md_text, re.DOTALL)
    if not m:
        return {}, md_text
    fm_raw, body = m.group(1), m.group(2)
    fm = {}
    lines = fm_raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        km = re.match(r"^([a-zA-Z_]+):\s*(.*)$", line)
        if not km:
            i += 1
            continue
        key, val = km.group(1), km.group(2).strip()
        if val == "" and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("- "):
            items = []
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(lines[i].lstrip()[2:].strip().strip('"').strip("'"))
                i += 1
            fm[key] = items
            continue
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip('"').strip("'") for x in val[1:-1].split(",") if x.strip()]
            fm[key] = items
            i += 1
            continue
        fm[key] = val.strip('"').strip("'")
        i += 1
    return fm, body


def is_empty_value(field, val):
    """null 字符串 / 空串 / 占位词(按字段策略)。"""
    if val is None:
        return True
    if isinstance(val, list):
        return len([v for v in val if str(v).strip()]) == 0
    s = str(val).strip()
    if s.lower() in PLACEHOLDER_VALUES:
        return True
    if field not in MAY_SAY_UNMENTIONED and s.startswith("未提及"):
        return True  # 未提及/未提及（原因）——data_open/code_open 白名单外字段同规
    return False


def yaml_emit_value(val):
    """按模板风格输出 YAML 值。"""
    if isinstance(val, list):
        return val
    s = str(val)
    if re.match(r"^\[\[.*\]\]$", s) or ": " in s or s.startswith("[") or "#" in s:
        return '"%s"' % s.replace('"', "'")
    if s.startswith("file:///"):
        return '"%s"' % s
    return s


def normalize_note(md_text):
    """机械修复(不经 LLM): 字段顺序重排 + 占位符删行 + tags 字符串转列表。
    返回 (fixed_text, fixes:list[str])。"""
    fm, body = parse_frontmatter_and_body(md_text)
    if not fm:
        return md_text, []
    fixes = []
    new_fm = {}
    for k, v in fm.items():
        if is_empty_value(k, v):
            fixes.append("删占位字段 %s: %r" % (k, v))
            continue
        if isinstance(v, str) and k in ("tags", "aliases") and ("," in v or "/" in v) \
                and not v.startswith("["):
            parts = [x.strip() for x in v.split(",") if x.strip()]
            if len(parts) > 1:
                fixes.append("%s 字符串拆列表" % k)
                v = parts
        new_fm[k] = v

    def order_key(k):
        return CANONICAL_ORDER.index(k) if k in CANONICAL_ORDER else len(CANONICAL_ORDER)

    ordered = sorted(new_fm.items(), key=lambda kv: order_key(kv[0]))
    if [k for k, _ in ordered] != [k for k in new_fm]:
        fixes.append("字段顺序重排为模板序")

    out = ["---"]
    for k, v in ordered:
        if isinstance(v, list):
            out.append("%s:" % k)
            for item in v:
                out.append('  - "%s"' % item)
        else:
            out.append("%s: %s" % (k, yaml_emit_value(v)))
    out.append("---")
    fixed = "\n".join(out) + "\n" + body
    return fixed, fixes


# ---------- 校验(R1-R12) ----------

TYPE_VALUES = ("方法类", "综述类", "实证类", "理论类")

SECTIONS_BY_TYPE = {
    "方法类": ["一句话", "用途四问", "Model", "Experiments", "创新点", "不足",
             "证据锚"],   # v2.1: "与你课题的关联"改为可选(用户点名才写; 写了由 R10 查质量)
    "综述类": ["一句话", "原文摘要翻译", "文献综述要点", "创新点", "不足",
             "证据锚"],
    "实证类": ["一句话", "用途四问", "数据来源", "主要发现", "创新点", "不足",
             "证据锚"],
    "理论类": ["一句话", "用途四问", "核心定理", "创新点", "不足",
             "证据锚"],
}

SECTION_ALIASES = {
    "一句话": ["一句话"],
    "用途四问": ["用途四问"],
    "Model": ["Model / Algorithm", "Model/Algorithm", "Model", "模型", "Method", "方法"],
    "Experiments": ["Experiments", "实验", "实验结果"],
    "原文摘要翻译": ["原文摘要翻译", "摘要翻译"],
    "文献综述要点": ["文献综述要点", "文献综述"],
    "数据来源": ["数据来源", "数据"],
    "主要发现": ["主要发现"],
    "核心定理": ["核心定理"],
    "创新点": ["创新点"],
    "不足": ["不足", "不足与局限", "局限"],
    "与你课题的关联": ["与你课题的关联", "对你课题的关联", "与你的研究关联", "与课题的关联"],
    "证据锚": ["证据锚"],
}

GREEK_CHARS = "αβγδεζηθικλμνξπρστυφχψω"
BARE_OPS = "≥≤∈⊂⊆∪∩∑∏∫≠"
SUBSCRIPT_PAT = re.compile(r"\b[a-zA-Z]_\{?[a-zA-Z0-9]{1,4}\}?\b")
CITE_PAT = re.compile(r"§\d|\[§|Table\s*\d|Fig(?:ure)?\s*\d|\bp\.\s*\d")


def get_section(body, name):
    aliases = SECTION_ALIASES.get(name, [name])
    for a in aliases:
        m = re.search(rf"^##\s[^#\n]*{re.escape(a)}[^#\n]*$", body, re.MULTILINE)
        if m:
            start = m.end()
            nxt = re.search(r"^##\s", body[start:], re.MULTILINE)
            return body[start:nxt.start() + start] if nxt else body[start:]
    return ""


def math_violations(body):
    """剥离 $...$、`...`、[[wikilink]]、URL 后, 找裸数学。返回问题列表(最多 6 条)。

    v2.2 修误报(2026-10-28 实证, Santini2026 笔记): 笔记里货币/票价常写成
    `[\\$]`——LaTeX 转义美元。旧实现按裸 `$` 左右配对剥离, `\\$` 被当成数学定界符,
    它之后的所有 `$...$` 全部错位: 已经规范包裹的 `$N_a$` 反被整段暴露出来,
    判成「裸下标变量」R6 FAIL(该笔记一行 15 组数学全部正确包裹, 仅因两个
    `[\\$]` 被误报 4 处)。于是出现「md 明明写了、面板却报精读·校验未过」。
    修法: ① 先摘掉转义美元再配对; ② 逐行剥离, 剥完仍残留 `$` 的行说明它本身
    写坏了、剥离结果不可信, 该行的数学检查跳过——宁可放过(还有 LLM 修复轮兜底),
    不可错杀出假 FAIL。
    """
    cleaned = body.replace("\\$", "")
    cleaned = re.sub(r"`[^`\n]*`", "", cleaned)
    cleaned = re.sub(r"\[\[[^\]]*\]\]", "", cleaned)
    cleaned = re.sub(r"\(https?://[^)]*\)", "()", cleaned)
    safe_lines = []
    for ln in cleaned.splitlines():
        out = re.sub(r"\$[^$\n]+\$", "", ln)
        if "$" in out:            # 该行 $ 没配对完, 剥离不可信, 跳过
            continue
        safe_lines.append(out)
    stripped = "\n".join(safe_lines)
    issues = []
    greek = sorted({ch for ch in GREEK_CHARS if ch in stripped})
    if greek:
        issues.append("裸希腊字母 %s(应 $\\alpha$ 式包裹)" % "".join(greek[:8]))
    ops = sorted({ch for ch in BARE_OPS if ch in stripped})
    if ops:
        issues.append("裸运算/关系符 %s" % "".join(ops[:8]))
    subs = SUBSCRIPT_PAT.findall(stripped)
    if subs:
        issues.append("裸下标变量 %s 等(应 $q_j$ 式包裹)" % ", ".join(sorted(set(subs))[:5]))
    return issues[:6]


def validate_note_text(md_text, name=""):
    """对一篇笔记文本跑 R1-R12。返回 {'fails':[{rule,msg}], 'warns':[...]}。"""
    fm, body = parse_frontmatter_and_body(md_text)
    fails, warns = [], []

    def fail(rule, msg):
        fails.append({"rule": rule, "msg": msg})

    def warn(rule, msg):
        warns.append({"rule": rule, "msg": msg})

    # R1 必填字段(值为空也算缺)
    for f in ["tags", "scenario", "type", "status", "data_open", "code_open",
              "date", "pdf", "extracted_on", "extracted_by"]:
        if f not in fm or is_empty_value(f, fm.get(f)):
            fail("R1", "frontmatter 缺 %s(或值为空/占位)" % f)

    # R2 字段顺序
    present = [k for k in fm if k in CANONICAL_ORDER]
    idx = [CANONICAL_ORDER.index(k) for k in present]
    if idx != sorted(idx):
        warn("R2", "字段顺序与模板不一致: %s" % ", ".join(present))

    # R3 占位符(doi 出现"未提及"等 = 违规; 其他字段 null 值)
    if "doi" in fm and is_empty_value("doi", fm["doi"]):
        fail("R3", "doi 是占位值 %r, 应整行省略" % fm["doi"])
    for k, v in fm.items():
        if isinstance(v, str) and v.strip().lower() in ("null", "none", "~"):
            fail("R3", "%s: %s 占位值应整行省略" % (k, v))

    # R4 scenario 30-80 字
    sc = str(fm.get("scenario", "")).strip()
    if sc and not (30 <= len(sc) <= 80):
        warn("R4", "scenario %d 字(要求 30-80)" % len(sc))

    # R5 必填小节(按 type 差异化; 未知 type 按方法类并 WARN)
    ntype = str(fm.get("type", "")).strip()
    if ntype not in TYPE_VALUES:
        warn("R5", "type=%r 不在四类中, 按方法类检查" % ntype)
        ntype = "方法类"
    for sec in SECTIONS_BY_TYPE[ntype]:
        aliases = SECTION_ALIASES.get(sec, [sec])
        if not any(re.search(rf"^##\s[^#\n]*{re.escape(a)}", body, re.MULTILINE)
                   for a in aliases):
            fail("R5", "缺小节 '## %s'(%s)" % (sec, ntype))

    # R6 裸数学
    for issue in math_violations(body):
        fail("R6", issue)

    # R7 块级 $$
    if "$$" in body:
        fail("R7", "存在块级 $$ 公式(模板禁用, 全部改行内)")

    # R8 一句话必须为中文
    yijuhua_sec = get_section(body, "一句话") or get_section(body, "一句话总结")
    if yijuhua_sec:
        first_line = next((ln.strip() for ln in yijuhua_sec.splitlines() if ln.strip()), "")
        first_line_clean = re.sub(r"^[#>*\-\s]+", "", first_line)
        if first_line_clean and not re.search(r"[\u4e00-\u9fff]", first_line_clean):
            fail("R8", "## 一句话 必须为中文（当前为纯英文，请提炼中文要点）")

    # R8b 创新点 3-5 条（按模板规范，仅统计顶层 3-5 条独立成句，不统计下方 ### 算法创新机理解构 内的展开子项）
    innov = get_section(body, "创新点")
    innov_main = innov.split("###")[0] if "###" in innov else innov
    n_innov = len(re.findall(r"^\s*(?:\d+\.|-)\s+", innov_main, re.MULTILINE))
    if innov and not (3 <= n_innov <= 5):
        fail("R8", "创新点 %d 条(要求 3-5, 编号列出)" % n_innov)
    if not innov and "创新点" not in SECTIONS_BY_TYPE.get(ntype, []):
        pass  # R5 已覆盖

    # R9 不足 ≥2 条
    lack = get_section(body, "不足")
    n_lack = len(re.findall(r"^\s*(?:\d+\.|-)\s+", lack, re.MULTILINE))
    if lack and n_lack < 2:
        warn("R9", "不足仅 %d 条(建议 ≥2, 分作者自述/我观察)" % n_lack)

    # R10 与你课题的关联(v2.1 起可选——用户项目多、关联暂缓; 要求时才写)
    rel = get_section(body, "与你课题的关联")
    if rel:
        paras = [p for p in re.split(r"\n\s*\n", rel) if p.strip()]
        if len(paras) < 2:
            warn("R10", "关联节仅 %d 段(建议 ≥2)" % len(paras))
        if "💎" not in rel:
            warn("R10", "关联节缺 💎 高价值平移点标记")
        if len(re.sub(r"\s", "", rel)) < 200:
            warn("R10", "关联节 %d 字(建议 ≥200)" % len(re.sub(r"\s", "", rel)))
    # 没写关联节: 不再报错(用户明确暂缓)

    # R11 证据锚：下限随笔记体量走
    anchors = get_section(body, "证据锚")
    if anchors:
        rows = [ln for ln in anchors.splitlines()
                if ln.strip().startswith("|") and ln.strip().count("|") >= 3
                and not re.match(r"^\|\s*#\s*\|", ln.strip())
                and not re.match(r"^\|[\s\-:|]+\|$", ln.strip())]
        # 固定「≥11 条」会逼模型给短文或纯理论论文造出原文里不存在的页码/表号
        # ——模板自己都承认「机器查不了,靠抽查」。改成按实质小节数推导需求：
        # 一个实质小节平均能给 2 条锚，再加 2 条（一句话/创新点各一处），下限 4、封顶 11。
        secs = sum(1 for s in ("Model", "Experiments", "数据来源", "主要发现", "核心定理",
                               "文献综述要点", "用途四问") if get_section(body, s))
        need = max(4, min(11, secs * 2 + 2))
        if len(rows) < need:
            fail("R11", "证据锚表 %d 条(本笔记 %d 个实质小节，要求 ≥%d)"
                 % (len(rows), secs, need))
        elif len(rows) > 16:
            warn("R11", "证据锚表 %d 条偏多，核对是否有为凑数而造的锚点" % len(rows))

    # R12 出处密度: Model/Experiments 类小节内带出处的条目占比
    cite_secs = []
    for sec in ("Model", "Experiments", "数据来源", "主要发现", "核心定理"):
        s = get_section(body, sec)
        if s:
            cite_secs.append(s)
    if cite_secs:
        items = []
        for s in cite_secs:
            items += [ln for ln in s.splitlines()
                      if ln.strip().startswith(("-", "*", "•")) and len(ln.strip()) > 15]
        if items:
            with_cite = [ln for ln in items if CITE_PAT.search(ln)]
            ratio = len(with_cite) / len(items)
            if ratio < 0.6:
                warn("R12", "出处密度 %d/%d(<60%%): 发现条目缺 [§X·Table/Fig] 标注"
                     % (len(with_cite), len(items)))

    return {"fails": fails, "warns": warns}


# ---------- 体检 ----------

def run_audit(vault, papers_dir):
    papers = sorted(papers_dir.glob("*.md"))
    results = []
    for p in papers:
        text = p.read_text(encoding="utf-8")
        v = validate_note_text(text)
        results.append({"path": p, "name": p.stem, **v,
                        "n": len(v["fails"]) + len(v["warns"])})
    results.sort(key=lambda r: (-len(r["fails"]), -len(r["warns"])))

    n_fail = sum(1 for r in results if r["fails"])
    n_warn_only = sum(1 for r in results if not r["fails"] and r["warns"])
    n_clean = sum(1 for r in results if not r["fails"] and not r["warns"])

    out = ["# 笔记体检报告(v2, 含内容级规则 R1-R12)\n",
           f"> 生成时间: {dt.datetime.now():%Y-%m-%d %H:%M} · 扫描: {len(results)} 篇 · 模板: v2\n",
           "## 总览\n",
           f"- 总篇数: **{len(results)}**",
           f"- 完全干净(0 FAIL 0 WARN): **{n_clean}**",
           f"- 仅 WARN: **{n_warn_only}**",
           f"- 有 FAIL(建议回炉): **{n_fail}**\n",
           "## 规则命中分布\n"]
    rule_hits = {}
    for r in results:
        for f in r["fails"]:
            rule_hits.setdefault(f["rule"], [0, 0])[0] += 1
        for w in r["warns"]:
            rule_hits.setdefault(w["rule"], [0, 0])[1] += 1
    RULE_DESC = {
        "R1": "frontmatter 必填字段", "R2": "字段顺序", "R3": "占位符未省略",
        "R4": "scenario 长度", "R5": "必填小节缺失", "R6": "裸数学未包 $..$",
        "R7": "块级 $$", "R8": "一句话中文要求", "R8b": "创新点条数", "R9": "不足条数",
        "R10": "关联节质量", "R11": "证据锚条数", "R12": "出处密度",   # 原列的 R13「书架规范」无任何实现，已撤
    }
    for rule in sorted(rule_hits):
        nf, nw = rule_hits[rule]
        out.append(f"- **{rule}** {RULE_DESC.get(rule, '')}: FAIL × {nf} · WARN × {nw}")

    out.append("\n## 待回炉 Top 30(按 FAIL 数降序)\n")
    out.append("| FAIL | WARN | 笔记 | 问题 |")
    out.append("|---|---|---|---|")
    for r in results[:30]:
        if not r["fails"] and not r["warns"]:
            continue
        msgs = [f"[{x['rule']}] {x['msg']}" for x in r["fails"]] + \
               [f"[{x['rule']}] {x['msg']}" for x in r["warns"][:3]]
        out.append(f"| {len(r['fails'])} | {len(r['warns'])} | `[[{r['name']}]]` |"
                   f" {'<br>'.join(msgs[:6])} |")

    out.append("\n## 完整清单(所有非零篇)\n")
    for r in results:
        if r["fails"] or r["warns"]:
            out.append(f"- `[[{r['name']}]]` F{len(r['fails'])}/W{len(r['warns'])} — "
                       + "; ".join(f"[{x['rule']}]" for x in r["fails"] + r["warns"]))

    out_path = vault / "_meta" / "笔记体检报告.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    print(f"[OK] 体检报告: {out_path}")
    print(f"  总 {len(results)} 篇 · 干净 {n_clean} · 仅WARN {n_warn_only} · 有FAIL {n_fail}")
    for rule in sorted(rule_hits):
        nf, nw = rule_hits[rule]
        print(f"    {rule} {RULE_DESC.get(rule, '')}: FAIL×{nf} WARN×{nw}")


# ---------- 书架页 / 新 PDF 检测 ----------

def one_liner_of(md_text):
    fm, body = parse_frontmatter_and_body(md_text)
    sec = get_section(body, "一句话") or get_section(body, "一句话总结")
    if sec:
        line = next((ln.strip() for ln in sec.splitlines() if ln.strip()), "")
        line = re.sub(r"^[#>*\-\s]+", "", line)
        if line:
            return line[:120]
    sc = str(fm.get("scenario", "")).strip()
    return sc[:120] if sc else "(待补一句话)"


TOPIC_ALIASES = {
    "运筹优化": "运筹优化算法与建模",
    "运筹算法": "运筹优化算法与建模",
    "末端配送": "城市末端配送",
    "城市配送": "城市末端配送",
    "无人机配送": "无人机物流配送优化",
    "无人机": "无人机物流配送优化",
    "应急": "应急物流",
    "海运": "海上运输调度与不确定性优化",
    "航运": "海上运输调度与不确定性优化",
    "港口航运": "港口",
    "供应链": "供应链与物流系统",
}


def topic_tags_of(fm, known_topics):
    """tags 里的主题标签(过滤类型值与未知项→待分类, 自动映射别名)。"""
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    known = {t.lower() for t in known_topics}
    out = []
    for t in tags:
        t = str(t).strip()
        if not t or t in TYPE_VALUES or t.startswith("papers/") or t.lower() in ("待分类",):
            continue
        mapped = TOPIC_ALIASES.get(t, t)
        if not known or mapped.lower() in known:
            out.append(mapped)
        elif t.lower() in known:
            out.append(t)
    return out or ["待分类"]


def find_shelf_path(vault, topic):
    """递归查找已存在的书架页；不存在则默认归入 00-分类/{topic}.md"""
    shelf_dir = vault / "00-分类"
    for p in shelf_dir.glob(f"**/{topic}.md"):
        return p
    return shelf_dir / f"{topic}.md"


def shelve_note(vault, note_stem, md_text, known_topics):
    """把笔记链接行追加进对应主题书架页(已存在则跳过), 并更新计数。"""
    fm, _ = parse_frontmatter_and_body(md_text)
    ntype = str(fm.get("type", "")).strip() or "方法类"
    line = f"- **[[{note_stem}]]** `{ntype}` — {one_liner_of(md_text)}"
    added = []
    for topic in topic_tags_of(fm, known_topics):
        shelf = find_shelf_path(vault, topic)
        shelf.parent.mkdir(parents=True, exist_ok=True)
        if shelf.exists():
            text = shelf.read_text(encoding="utf-8")
            if f"[[{note_stem}]]" in text:
                continue
            m = re.search(r"共\s*\*\*(\d+)\*\*\s*篇", text)
            if m:
                text = text.replace(m.group(0), "共 **%d** 篇" % (int(m.group(1)) + 1), 1)
            text = text.rstrip("\n") + "\n" + line + "\n"
            shelf.write_text(text, encoding="utf-8")
        else:
            shelf.write_text(
                "---\ntags:\n  - 书架\n  - \"%s\"\n---\n\n# 📚 %s\n\n> 共 **1** 篇。返回：[[文献总索引]]\n\n%s\n"
                % (topic, topic, line), encoding="utf-8")
        added.append(topic)
    return added


def reindex_shelves(vault, known_topics):
    """0-token 全量重建 00-分类 所有书架索引页，消除死链、重复、乱序与旧格式。"""
    papers_dir = vault / "papers"
    shelf_dir = vault / "00-分类"
    if not papers_dir.exists():
        eprint(f"ERROR: papers 目录不存在: {papers_dir}")
        return 1

    topic_map = {}
    notes = list(papers_dir.glob("*.md"))
    print(f"正在扫描 {len(notes)} 篇文献笔记...")

    for note_path in sorted(notes):
        text = note_path.read_text(encoding="utf-8", errors="ignore")
        fm, _ = parse_frontmatter_and_body(text)
        ntype = str(fm.get("type", "")).strip() or "方法类"
        date_str = str(fm.get("date", "")).strip()
        ol = one_liner_of(text)
        topics = topic_tags_of(fm, known_topics)
        entry = {
            "stem": note_path.stem,
            "type": ntype,
            "summary": ol,
            "date": date_str
        }
        for t in topics:
            topic_map.setdefault(t, []).append(entry)

    ignore_files = {"学术领域与方法总览.md", "学习资源与方法指南.md"}
    all_shelves = [p for p in shelf_dir.glob("**/*.md") if p.name not in ignore_files]
    shelf_stems = {p.stem: p for p in all_shelves}

    for t in topic_map:
        if t not in shelf_stems:
            shelf_stems[t] = find_shelf_path(vault, t)

    updated = 0
    for topic, shelf_path in sorted(shelf_stems.items(), key=lambda x: x[0]):
        entries = topic_map.get(topic, [])
        # 年份倒序 + stem 排序
        entries.sort(key=lambda x: (x.get("date") or "0000", x["stem"]), reverse=True)

        lines = [
            "---",
            "tags:",
            "  - 书架",
            f'  - "{topic}"',
            "---",
            "",
            f"# 📚 {topic}",
            "",
            f"> 共 **{len(entries)}** 篇。返回：[[文献总索引]]",
            ""
        ]
        for e in entries:
            lines.append(f"- **[[{e['stem']}]]** `{e['type']}` — {e['summary']}")
        lines.append("")

        shelf_path.parent.mkdir(parents=True, exist_ok=True)
        shelf_path.write_text("\n".join(lines), encoding="utf-8")
        rel_p = shelf_path.relative_to(shelf_dir)
        print(f"  ✓ [{rel_p}] -> 共 {len(entries)} 篇")
        updated += 1

    print(f"\n[OK] 全量书架重建完成：共更新 {updated} 个书架，覆盖 {len(notes)} 篇文献。")
    return 0


def _norm_key(s):
    """规范化比对键: 小写 + 只留字母数字中文。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s.lower())


def _unquote(text):
    """URL 解码(file:/// 形态的 %XX 转中文)。"""
    try:
        from urllib.parse import unquote
        return unquote(text)
    except Exception:  # noqa: BLE001
        return text


def noted_pdf_blobs(vault):
    """每篇笔记 frontmatter 的规范化文本块(截断 wikilink / URL 编码都能兜住)。"""
    blobs = []
    for md in (vault / "papers").glob("*.md"):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        blob = _norm_key(_unquote(m.group(1) if m else text[:3000]))
        if blob:
            blobs.append(blob)
    return blobs


def load_ignore_list(vault):
    p = vault / "_meta" / "忽略清单.txt"
    if not p.exists():
        return set()
    out = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#", 1)[0].strip()        # 剥行内注释
        if ln:
            out.add(ln.lower())
    return out


def find_new_pdfs(pdf_root, vault):
    """还没有笔记的 PDF。匹配 = PDF 文件名规范化前 60 字符出现在任一笔记 frontmatter 中。"""
    blobs = noted_pdf_blobs(vault)
    ignore = load_ignore_list(vault)
    # 短名(规范化<8字符)无法安全前缀匹配 → 先查精确 wikilink "[[name.pdf]]"
    exact = set()
    for md in (vault / "papers").glob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\[\[([^\]|]+?\.pdf)\]\]", text):
            exact.add(m.group(1).strip().lower())
    out = []
    for p in sorted(pdf_root.glob("*.pdf"), key=lambda x: x.name.lower()):
        if p.name.lower() in ignore:
            continue
        key = _norm_key(p.stem)
        if len(key) < 8:
            if p.name.lower() not in exact:
                out.append(p)  # 短名且无精确 wikilink → 报未笔记化
            continue
        if not any(key[:60] in b for b in blobs):
            out.append(p)
    return out


# ---------- 生成主流程 ----------

def slugify_first_author_year(pdf_path, meta_text):
    """直接采用 PDF 文件名 stem（真实论文标题或规范简写），保持 .md 与 .pdf 命名同名对齐。"""
    base = Path(pdf_path).stem.strip()
    return base or "untitled-note"


def unique_path(path):
    if not path.exists():
        return path
    for i in range(2, 99):
        cand = path.with_name(re.sub(r"\.md$", "", path.name) + f"-{i}.md")
        if not cand.exists():
            return cand
    return path


def process_pdf(pdf_path, args, cfg, vault, topics, profile, template):
    """单篇全流程: 提取→综述识别→LLM→规范化→校验→修复一轮→写盘→上架。返回笔记路径。"""
    tmp_dir = Path(cfg["pdf_root"]) / ".tmp_extract"
    tmp_dir.mkdir(exist_ok=True)

    meta_path = Path(call_paper_tool(str(pdf_path), str(tmp_dir), mode="meta"))
    meta_text = meta_path.read_text(encoding="utf-8")

    if args.with_litreview:
        keep_lit = True
    elif args.no_litreview:
        keep_lit = False
    else:
        keep_lit = looks_like_survey(meta_text)
    print(f"[..] {pdf_path.name} 综述识别={keep_lit} → deep 提取")
    transcript_path = Path(call_paper_tool(str(pdf_path), str(tmp_dir),
                                           mode="deep", with_litreview=keep_lit))
    transcript = transcript_path.read_text(encoding="utf-8")

    if args.rewrite and args.existing:
        old = Path(args.existing).read_text(encoding="utf-8")
        prompt = build_prompt_for_rewrite(template, old, transcript, meta_text, profile)
    else:
        prompt = build_prompt_for_new_paper(template, transcript, meta_text, topics, profile)

    print(f"[..] 调 LLM(prompt {len(prompt)} chars)")
    new_md = call_llm(prompt, system=SYSTEM_PROMPT)

    new_md, fixes = normalize_note(new_md)
    if fixes:
        print("[OK] 规范化修复: " + "; ".join(fixes))
    v = validate_note_text(new_md)
    if v["fails"] and not args.no_repair:
        print(f"[..] 校验 {len(v['fails'])} FAIL, 自动修复一轮")
        try:
            md2 = call_llm(build_repair_prompt(new_md, v["fails"]), system=SYSTEM_PROMPT)
            md2, fixes2 = normalize_note(md2)
            v2 = validate_note_text(md2)
            if len(v2["fails"]) <= len(v["fails"]):
                new_md, v = md2, v2
                if fixes2:
                    print("[OK] 二次规范化: " + "; ".join(fixes2))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 — 修复轮失败不阻塞交付
            eprint(f"[WARN] 修复轮失败(交付未修复版): {e}")

    # 保证 pdf 字段精准对齐磁盘上的实际 PDF 文件名
    new_md = re.sub(r'^(pdf:\s*).*$', f'pdf: "[[{pdf_path.name}]]"', new_md, flags=re.MULTILINE)

    if args.out:
        out_path = Path(args.out)
    elif args.rewrite and args.existing:
        out_path = Path(args.existing)
    else:
        slug = slugify_first_author_year(pdf_path, meta_text)
        out_path = unique_path(vault / "papers" / f"{slug}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(new_md, encoding="utf-8")
    print(f"[OK] 写盘: {out_path}")

    if v["fails"]:
        eprint("  [!] 仍有 FAIL(建议人工过目或再跑一次 --rewrite):")
        for f in v["fails"]:
            eprint("      [%s] %s" % (f["rule"], f["msg"]))
    if v["warns"]:
        for w in v["warns"][:5]:
            eprint("      [%s·W] %s" % (w["rule"], w["msg"]))

    if not args.no_shelve:
        topics_added = shelve_note(vault, out_path.stem, new_md, topics)
        if topics_added:
            print("[OK] 上架: " + ", ".join(topics_added))
    return out_path


# ---------- 全库机械修复 ----------

def shelf_map(vault):
    """从 00-分类/*.md 书架页提取 {笔记名: {topics, types}}（确定性，0 LLM）。"""
    out = {}
    for shelf in (vault / "00-分类").glob("*.md"):
        topic = shelf.stem
        text = shelf.read_text(encoding="utf-8")
        for m in re.finditer(r"\[\[([^\]|]+)\]\]\*{0,2}\s*`([^`]*)`", text):
            stem, ntype = m.group(1).strip(), m.group(2).strip()
            d = out.setdefault(stem, {"topics": [], "types": []})
            if topic != "待分类" and topic not in d["topics"]:
                d["topics"].append(topic)
            if ntype and ntype not in d["types"]:
                d["types"].append(ntype)
    return out


def fix_mechanical(vault):
    """全库机械修复（0 LLM）: frontmatter 规范化(字段序/占位符删行/status 统一)
    + 从书架页回填缺失的 tags/type。"""
    smap = shelf_map(vault)
    stats = {"files": 0, "tags_filled": 0, "type_filled": 0, "status_unified": 0}
    for md in sorted((vault / "papers").glob("*.md")):
        orig = md.read_text(encoding="utf-8")
        fm, body = parse_frontmatter_and_body(orig)
        if not fm:
            continue
        changed = False
        info = smap.get(md.stem)
        if info:
            if ("tags" not in fm or is_empty_value("tags", fm.get("tags"))) and info["topics"]:
                fm["tags"] = info["topics"]
                stats["tags_filled"] += 1
                changed = True
            if ("type" not in fm or is_empty_value("type", fm.get("type"))) and info["types"]:
                fm["type"] = info["types"][0]
                stats["type_filled"] += 1
                changed = True
        if str(fm.get("status", "")).strip('"\u2705') in ("精读",) and "✅" not in str(fm.get("status")):
            fm["status"] = "✅精读"
            stats["status_unified"] += 1
            changed = True
        # 重拼 frontmatter 再走 normalize(字段排序/占位符删除)
        lines = ["---"]
        for k, v in fm.items():
            if isinstance(v, list):
                lines.append("%s:" % k)
                lines.extend('  - "%s"' % i for i in v)
            else:
                lines.append("%s: %s" % (k, yaml_emit_value(v)))
        lines.append("---")
        text = "\n".join(lines) + "\n" + body
        fixed, _ = normalize_note(text)
        if fixed != orig:
            md.write_text(fixed, encoding="utf-8")
            stats["files"] += 1
    print("[OK] 机械修复 %d 篇: tags 回填 %d · type 回填 %d · status 统一 %d"
          % (stats["files"], stats["tags_filled"], stats["type_filled"],
             stats["status_unified"]))


# ---------- main ----------

def confirm(question):
    if getattr(confirm, "_yes", False):
        return True
    ans = input(f"{question} (y/N) ").strip().lower()
    return ans in ("y", "yes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="PDF 路径(单篇生成/回炉)")
    ap.add_argument("--existing", help="已存在 .md(回炉模式, 配 --rewrite)")
    ap.add_argument("--rewrite", action="store_true", help="回炉模式开关")
    ap.add_argument("--audit-only", action="store_true", help="全库体检, 0 token")
    ap.add_argument("--reindex-shelves", action="store_true",
                    help="0-token 全量重建 00-分类 所有书架索引页(消除死链/旧格式/孤立文件)")
    ap.add_argument("--fix-mechanical", action="store_true",
                    help="全库机械修复(0 LLM): 字段序/占位符/status 统一 + 书架回填 tags/type")
    ap.add_argument("--validate", action="store_true", help="校验单篇(配 --note)")
    ap.add_argument("--note", help="待校验的 .md 路径")
    ap.add_argument("--scan", action="store_true", help="列出还没有笔记的 PDF, 0 token")
    ap.add_argument("--auto", action="store_true", help="批量精读所有未笔记化 PDF")
    ap.add_argument("--watch", action="store_true", help="监听模式: 轮询新 PDF 自动精读")
    ap.add_argument("--interval", type=int, default=300, help="watch 轮询秒数(默认 300)")
    ap.add_argument("--limit", type=int, default=0, help="auto 最多处理 N 篇(0=不限)")
    ap.add_argument("--yes", action="store_true", help="auto/watch 跳过确认")
    ap.add_argument("--with-litreview", action="store_true", help="强制保留文献综述章")
    ap.add_argument("--no-litreview", action="store_true", help="强制裁掉文献综述章")
    ap.add_argument("--no-repair", action="store_true", help="跳过自动修复轮")
    ap.add_argument("--no-shelve", action="store_true", help="不自动更新 00-分类 书架页")
    ap.add_argument("--topics-file", help="覆盖 _研究主题.md 路径")
    ap.add_argument("--out", help="输出 .md 路径(默认自动命名到 vault/papers/)")
    args = ap.parse_args()

    cfg = load_config()
    vault = Path(cfg["vault"])
    pdf_root = Path(cfg["pdf_root"])
    topics = read_topics(vault, cfg, args.topics_file)
    confirm._yes = args.yes

    if args.audit_only:
        run_audit(vault, vault / "papers")
        return 0

    if args.reindex_shelves:
        return reindex_shelves(vault, topics)

    if args.fix_mechanical:
        fix_mechanical(vault)
        run_audit(vault, vault / "papers")
        return 0

    if args.validate:
        if not args.note:
            eprint("ERROR: --validate 需要 --note <path>")
            return 1
        p = Path(args.note)
        if not p.exists():
            cand = vault / "papers" / args.note
            p = cand if cand.exists() else p
        v = validate_note_text(p.read_text(encoding="utf-8"))
        print(f"# 校验 {p.stem}: FAIL {len(v['fails'])} · WARN {len(v['warns'])}")
        for f in v["fails"]:
            print(f"  [FAIL {f['rule']}] {f['msg']}")
        for w in v["warns"]:
            print(f"  [WARN {w['rule']}] {w['msg']}")
        if not v["fails"] and not v["warns"]:
            print("  全部通过 ✅")
        return 7 if v["fails"] else 0

    if args.scan or args.auto or args.watch:
        new_pdfs = find_new_pdfs(pdf_root, vault)
        if not new_pdfs:
            print("[OK] 没有未笔记化的新 PDF")
            return 0
        print(f"发现 {len(new_pdfs)} 个未笔记化 PDF:")
        for i, p in enumerate(new_pdfs, 1):
            print(f"  [{i:2d}] {p.name}")
        if args.scan:
            return 0
        if not confirm("批量精读以上全部?"):
            print("取消。")
            return 0
        batch = new_pdfs[: args.limit] if args.limit else new_pdfs
        # 逐篇跑, 单篇失败不中断
        template = read_template(vault)
        profile = read_profile(vault, cfg)
        ok = fail = 0
        for p in batch:
            try:
                process_pdf(p, args, cfg, vault, topics, profile, template)
                ok += 1
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                eprint(f"[FAIL] {p.name}: {e}")
                fail += 1
        print(f"[DONE] 批量完成: 成功 {ok} · 失败 {fail}")
        if args.watch:
            print(f"[..] watch 模式: 每 {args.interval}s 轮询一次, Ctrl+C 停止")
            try:
                while True:
                    time.sleep(args.interval)
                    for p in find_new_pdfs(pdf_root, vault):
                        try:
                            process_pdf(p, args, cfg, vault, topics, profile, template)
                        except Exception as e:  # noqa: BLE001
                            eprint(f"[FAIL] {p.name}: {e}")
            except KeyboardInterrupt:
                print("\n[OK] watch 停止")
        return 0

    if not args.pdf:
        eprint("ERROR: 必须给 --pdf(或 --audit-only/--validate/--scan/--auto/--watch)")
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        cand = pdf_root / args.pdf
        if cand.exists():
            pdf_path = cand
        else:
            eprint(f"ERROR: PDF 不存在: {pdf_path}")
            return 1

    template = read_template(vault)
    profile = read_profile(vault, cfg)
    out = process_pdf(pdf_path, args, cfg, vault, topics, profile, template)
    print(f"[DONE] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
