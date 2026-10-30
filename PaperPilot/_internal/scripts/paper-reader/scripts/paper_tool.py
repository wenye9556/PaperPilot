#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_tool.py — 从 PDF 提取文本并裁剪章节（v2：只有精读，无略读）。

用法:
    python paper_tool.py <pdf> [-o outdir] [--mode deep|meta] [--max-chars N]

    meta = 只取首页前 1500 字符(标题+摘要), 用于批量分类, 0 成本
    deep = 精读稿(全文, 默认裁掉 参考文献/附录; 文献综述章默认也裁,
           综述类论文加 --with-litreview 保留)

依赖: 优先用系统自带的 pdftotext(poppler)；若可用 PyMuPDF 则自动回退。
     两者都需要 0 网络、0 成本（本地完成）。

输出(outdir 下, 以 PDF 文件名为 slug):
    <slug>.txt            全文
    <slug>.sections.json  识别出的章节(csv: heading / kind / start_idx / end_idx)
    <slug>.deep.txt       精读稿
    <slug>.meta.txt       标题+摘要片段(批量分类用)

退出码: 0 成功; 2 缺依赖; 3 无文本层(疑似扫描版); 4 乱码(缺 CMap 的中文 PDF)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

# Windows 下 Python 默认用活动代码页解码 argv, 中文路径会变乱码;
# 非 UTF-8 模式启动时自我重启为 UTF-8 模式
if os.name == "nt" and sys.flags.utf8_mode != 1:
    os.execv(sys.executable, [sys.executable, "-X", "utf8", os.path.abspath(__file__)]
             + sys.argv[1:])


# ---------------------------------------------------------------- 章节分类
# kind 取值: intro / litreview / model / algorithm / experiment / conclusion /
#            other / reference / appendix
LIT_KEYWORDS = [
    "literature", "related work", "background", "state of the art",
    "literature review", "prior work", "文献综述", "相关工作", "研究现状", "国内外研究",
]
REF_KEYWORDS = ["references", "bibliography", "参考文献"]
APPENDIX_KEYWORDS = [
    "appendix", "appendices", "acknowledgment", "acknowledgement",
    "acknowledgments", "acknowledgements", "附录", "致谢",
]
CONCLUSION_KEYWORDS = [
    "conclusion", "summary", "future work", "closing remarks",
    "结论", "总结", "展望",
]
INTRO_KEYWORDS = ["introduction", "intro", "引言", "绪论", "导论"]
MODEL_KEYWORDS = [
    "model", "formulation", "problem statement", "problem definition",
    "problem description", "framework", "mathematical", "notation",
    "routing", "scheduling", "optimization", "optimisation", "optimize",
    "模型", "问题描述", "问题建模", "问题定义", "符号", "规划", "调度",
]
ALGO_KEYWORDS = [
    "algorithm", "method", "methodology", "approach", "metaheuristic",
    "heuristic", "solution", "算法", "方法", "求解",
]
EXPERIMENT_KEYWORDS = [
    "experiment", "result", "case study", "numerical", "computational",
    "evaluation", "discussion", "sensitivity", "benchmark", "performance",
    "simulation", "实验", "算例", "案例", "结果", "数值", "仿真", "验证",
]


def classify_heading(text):
    t = re.sub(r"^\s*\d+(\.\d+)*\.?\s*", "", text).strip().lower()
    if not t:
        return "other"
    for kw in REF_KEYWORDS:
        if kw in t:
            return "reference"
    for kw in APPENDIX_KEYWORDS:
        if kw in t:
            return "appendix"
    for kw in LIT_KEYWORDS:
        if kw in t:
            return "litreview"
    for kw in CONCLUSION_KEYWORDS:
        if kw in t:
            return "conclusion"
    for kw in INTRO_KEYWORDS:
        if kw in t:
            return "intro"
    for kw in MODEL_KEYWORDS:
        if kw in t:
            return "model"
    for kw in ALGO_KEYWORDS:
        if kw in t:
            return "algorithm"
    for kw in EXPERIMENT_KEYWORDS:
        if kw in t:
            return "experiment"
    return "other"


# 判定一行是否是"章节标题"。返回层级: 0=不是, 1=一级章节, 2=二级, ...
# 英文: "1. Introduction" / "3.2. Data set"
# 中文: "一、引言" / "3 结论"
# 无编号: "References" / "Acknowledgments" / "Appendix A"
EN_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})*\.?)\s+([A-Z][A-Za-z0-9 ,:&'-]{1,90})\s*$")
CN_HEAD = re.compile(
    r"^\s*((?:第)?[一二三四五六七八九十]{1,3}[章节]|\d{1,2})[\s、.．]*(引言|绪论|导论|文献综述|相关工作|"
    r"研究现状|问题(.{0,12})?|模型(.{0,12})?|方法|算法(.{0,12})?|实验(.{0,12})?|算例(.{0,12})?|"
    r"案例(.{0,12})?|结果(.{0,12})?|结论(.{0,12})?|总结(.{0,12})?|展望|参考文献|附录|致谢)\s*$"
)
UNNUMBERED_HEAD = re.compile(
    r"^\s*(Abstract|References|Bibliography|Acknowledgments?|Acknowledgements?|"
    r"Nomenclature|Appendices?|Appendix(?:\s+[A-Z0-9]+)?)\s*:?\s*$", re.I)


def is_garbled(text):
    """检测"缺 CMap 的中文 PDF"类乱码: pdftotext 会输出大量 ÿ(\xff)/控制符/U+FFFD。
    正常论文这些脏字符接近 0; 乱码中文 PDF 实测占比 >40%。阈值 2% 足够区分。
    修正(v2.1.1): 数学密集论文的公式符号也可能产生 U+FFFD(数学字体映射问题,
    非乱码)。区分方法: 分段检查——如果 FFFD 集中在英文密集区(公式段)而非
    全文均匀散布, 则判定为数学字体问题而非乱码, 放行并替换 FFFD 为 [math]。"""
    vis = re.sub(r"\s", "", text)
    if not vis:
        return False
    dirty = len(re.findall(r"[\x00-\x08\x0b\x0e-\x1f\x7f]", text))
    dirty += text.count("\xff") + text.count("\ufffd")
    if dirty / len(vis) <= 0.02:
        return False
    # 超过阈值——区分: 分段检查 FFFD 与英文/中文字符的共现关系
    seg_len = 1000
    true_garble_segs = 0
    math_segs = 0
    for i in range(0, len(text), seg_len):
        seg = text[i:i + seg_len]
        seg_fffd = seg.count("\ufffd")
        if seg_fffd <= 10:
            continue
        seg_alpha = len(re.findall(r"[a-zA-Z]", seg))
        seg_cjk = len(re.findall(r"[\u4e00-\u9fff]", seg))
        if seg_alpha > 100 or seg_cjk > 30:
            math_segs += 1        # 有足够可读文本(英/中) + FFFD 高 = 数学区
        else:
            true_garble_segs += 1  # 几乎无可读文本 + FFFD 高 = 真乱码
    # 判定: 存在数学区且真乱码段极少(<=3) → 数学论文, 非乱码
    # 真正的乱码 PDF 全文都是乱码, 不会有大量可读段
    if math_segs > 0 and true_garble_segs <= 3:
        return False
    return true_garble_segs > 0


def clean_math_fffd(text):
    """数学字体问题的 U+FFFD 替换为 [math] 占位符(可读且不干扰分析)。"""
    return text.replace("\ufffd", "[math]")


def heading_level(line):
    s = line.strip()
    if not s or len(s) > 90:
        return 0
    m = EN_HEAD.match(s)
    if m:
        num = m.group(1).rstrip('.')
        return num.count('.') + 1
    m = re.match(r"^\s*(\d{1,2}(?:\.\d{1,2})*\.?)\s*[\u4e00-\u9fff]", s)
    if m:
        return m.group(1).rstrip('.').count('.') + 1
    if CN_HEAD.match(s):
        return 1
    if UNNUMBERED_HEAD.match(s):
        return 1
    return 0


def page_map(lines):
    """逐行页码(1-based)。pdftotext 在每页末尾输出 \f。"""
    page_of_line = []
    pg = 1
    for ln in lines:
        page_of_line.append(pg)
        if "\f" in ln:
            pg += 1
    return page_of_line


def split_sections(full):
    lines = full.split("\n")
    page_of_line = page_map(lines)
    heads = []
    for i, ln in enumerate(lines):
        lvl = heading_level(ln)
        if lvl > 0:
            heads.append((i, ln.strip(), lvl))
    if len(heads) < 2:
        return [{"heading": "FULL", "kind": "other", "start": 0,
                 "end": len(lines), "page_start": 1,
                 "page_end": page_of_line[-1],
                 "text_joined": full}], False, lines

    sections = []
    current_main = "other"
    for j, (idx, h, lvl) in enumerate(heads):
        end = heads[j + 1][0] if j + 1 < len(heads) else len(lines)
        seg = "\n".join(lines[idx:end]).strip()
        if lvl == 1:
            current_main = classify_heading(h)
        # 子章节继承父章节的类型(用于略读裁剪; 关键: 让 2.1/2.2 正确归为 litreview)
        sections.append({"heading": h, "kind": current_main,
                         "lvl": lvl, "start": idx, "end": end,
                         "page_start": page_of_line[idx],
                         "page_end": page_of_line[end - 1],
                         "text_joined": seg})
    return sections, True, lines


# 精读时过滤掉的章节 kind(参考文献/附录/致谢)
DEEP_SKIP = {"reference", "appendix"}


def build_transcript(sections, skip_kinds, full):
    kept = [s for s in sections if s["kind"] not in skip_kinds]
    if len(kept) == 0:
        return full
    return "\n\n".join(s["text_joined"] for s in kept)


def extract_with_pdftotext(pdf, out_txt):
    # pdftotext 是**外部程序**（poppler 提供，Windows 上常见来源是 MiKTeX / TeXLive /
    # poppler-windows），打包时没有、目标机器上也常常没有。
    # 旧写法直接 subprocess.run(["pdftotext", ...])：PATH 里找不到时抛
    # FileNotFoundError 而**不是**返回非零 rc，于是 main() 里「rc != 0 就退回
    # PyMuPDF」的兜底根本走不到，整个脚本带着 traceback 崩掉。
    # 用户实录（v1.9.6）：Failed to execute script 'app' ... paper_tool.py line 268
    # in main / line 230 in extract_with_pdftotext → FileNotFoundError [WinError 2]。
    # 改成先 which 再 run，缺依赖统一返回 127，让 PyMuPDF 兜底正常接管。
    exe = shutil.which("pdftotext")
    if not exe:
        return 127, "pdftotext 不在 PATH 中（本机未安装 poppler）"
    try:
        p = subprocess.run([exe, "-enc", "UTF-8", pdf, out_txt],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, text=True)
        return p.returncode, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "pdftotext 调用失败: %s" % e


def extract_with_pymupdf(pdf, out_txt):
    import fitz  # noqa: F401  (延迟导入, 没有也不报错)
    doc = fitz.open(pdf)
    parts = []
    for page in doc:
        parts.append(page.get_text("text"))
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return 0, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--outdir", default=None)
    ap.add_argument("--mode", default="deep", choices=["deep", "meta"])
    ap.add_argument("--max-chars", type=int, default=400000)
    ap.add_argument("--with-litreview", action="store_true",
                    help="提取时保留文献综述/相关工作章节(默认裁掉省 token; 综述类论文才用)")
    args = ap.parse_args()

    pdf = os.path.abspath(args.pdf)
    if not os.path.exists(pdf):
        print("ERROR: PDF 不存在: %s" % pdf, file=sys.stderr)
        return 1

    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_",
                  os.path.splitext(os.path.basename(pdf))[0]).strip("_") or "paper"
    outdir = args.outdir or os.path.dirname(pdf)
    os.makedirs(outdir, exist_ok=True)

    txt_path = os.path.join(outdir, slug + ".txt")

    rc, err = extract_with_pdftotext(pdf, txt_path)
    if rc != 0:
        # pdftotext 失败(罕见): 尝试 PyMuPDF
        try:
            rc, err = extract_with_pymupdf(pdf, txt_path)
        except Exception as e:  # noqa: BLE001
            err = str(e)
        if rc != 0:
            print("ERROR: 无法提取 PDF 文本 (%s)。提示: 扫描版 PDF 需要 OCR。"
                  % (err.strip() or "未知错误"), file=sys.stderr)
            return 2

    with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
        full = f.read()

    visible = re.sub(r"\s", "", full)
    # 乱码或无文本检测: 若 pdftotext 提取异常(缺 CMap 产生大量 U+FFFD), 尝试 PyMuPDF 救砖
    check_text = full[:1500] if args.mode == "meta" else full
    if len(visible) < 400 or is_garbled(check_text):
        try:
            rc_mu, _ = extract_with_pymupdf(pdf, txt_path)
            if rc_mu == 0:
                with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
                    full_mu = f.read()
                vis_mu = re.sub(r"\s", "", full_mu)
                check_mu = full_mu[:1500] if args.mode == "meta" else full_mu
                if len(vis_mu) >= 400 and not is_garbled(check_mu):
                    full = full_mu
                    visible = vis_mu
                    check_text = check_mu
        except Exception:
            pass

    # 无文本层检测
    if len(visible) < 400:
        print("ERROR: almost no text layer (likely a SCANNED PDF). Need OCR "
              "(e.g. ocrmypdf) or a text-layer version. Do NOT read raw output.",
              file=sys.stderr)
        return 3

    if is_garbled(check_text):
        print("ERROR: extracted text is GARBLED (PDF lacks CMap, common in old "
              "Chinese PDFs). Do NOT read it. Ask the user to re-export the PDF "
              "(Print-to-PDF in Adobe/SumatraPDF) or supply a text-layer version.",
              file=sys.stderr)
        return 4
    # 数学区 U+FFFD 清理(非乱码场景)
    full = clean_math_fffd(full)

    # mode=meta: 只取首页前 1500 字符(标题+摘要), 用于批量分类; 不切分章节
    if args.mode == "meta":
        meta = full[:1500]
        out_name = os.path.join(outdir, slug + ".meta.txt")
        with open(out_name, "w", encoding="utf-8") as f:
            f.write(meta)
        print("OK mode=meta slug=%s chars=%d" % (slug, len(meta)))
        print("transcript=%s" % os.path.abspath(out_name))
        return 0

    if len(full) > args.max_chars:
        full = full[:args.max_chars]

    sections, detected, _ = split_sections(full)

    sec_meta = [{"heading": s["heading"], "kind": s["kind"],
                 "pages": "p%d-%d" % (s["page_start"], s["page_end"]),
                 "chars": len(s["text_joined"])} for s in sections]
    with open(os.path.join(outdir, slug + ".sections.json"), "w",
              encoding="utf-8") as f:
        json.dump({"detected": detected, "sections": sec_meta},
                  f, ensure_ascii=False, indent=2)

    skip = DEEP_SKIP
    if not args.with_litreview:
        # 默认不读文献综述章节(省 token); 综述类论文或用户显式要求才保留
        skip = skip | {"litreview"}
    transcript = build_transcript(sections, skip, full)

    out_name = os.path.join(outdir, slug + ".deep.txt")
    with open(out_name, "w", encoding="utf-8") as f:
        f.write(transcript)

    # 打印给 agent 看的摘要(不占正文 token, 但让 agent 了解结构)
    print("OK mode=deep slug=%s" % slug)
    print("full_chars=%d transcript_chars=%d sections_detected=%s"
          % (len(full), len(transcript), detected))
    for s in sec_meta:
        print("  [%s] %s (%s, %d chars)" % (s["kind"], s["heading"][:60], s["pages"], s["chars"]))
    print("transcript=%s" % os.path.abspath(out_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())