# PaperPilot 文献工作台

> 本地文献工作台：**输入主题 → 自动检索顶刊 → 下载正版 PDF → 自动精读 → 调研报告**，一条龙。
> 完全自包含，不依赖任何账号体系 / Agent 环境。

一个跑在你本机的科研文献工作流工具。它把「找文献 → 判断值不值得读 → 下载官方 PDF → 精读做笔记 → 出推荐报告」这一整条流程，压缩成几次点击。

**版本**：v1.4.0（随包文档标注） · **平台**：Windows x64 · **许可**：AGPL-3.0

---

## 快速开始

1. 下载或克隆本仓库，**整个 `PaperPilot` 文件夹一起保留**（`PaperPilot.exe` 必须和 `_internal/` 待在一起，只拷 exe 会启动失败）。
2. 双击 `PaperPilot.exe`，会弹出确认框告诉你服务已启动。
3. 浏览器打开 <http://127.0.0.1:8734>。若端口被占用会自动顺延，实际地址写在 `PaperPilot/data/addr.txt`。

> ⚠️ 不要直接双击 `web/index.html` —— 那样打不开后端。

详细使用说明见 [`PaperPilot/使用说明.md`](PaperPilot/使用说明.md)，
处理流程见 [`PaperPilot/软件说明与处理流程.md`](PaperPilot/软件说明与处理流程.md)。

### 可选启动参数

| 环境变量 | 效果 |
|---|---|
| `PAPERPILOT_OPEN_BROWSER=1` | 启动时自动打开浏览器，不用点「是」 |
| `PAPERPILOT_SILENT=1` | 完全静默，连确认框都不弹 |

---

## 核心功能

| 痛点 | PaperPilot 的做法 |
|---|---|
| 文献太多 | OpenAlex / Crossref / arXiv 全网检索，可勾选「仅顶刊池（22 刊）」源头过滤 |
| 值不值得读 | LLM 按「相关 0.40 / 新颖 0.20 / 质量 0.15 / 方法 0.15 / 启发 0.10」五维打分，证据复核通过才入选 |
| 下载难 | 多通道递进：OA 直下 → 浏览器借道 → 出版商页借道 → EBSCO 机构通道 → ScienceDirect → arXiv 预印本 |
| 精读费时 | 下载后自动精读，R1–R12（含 R8b，共 13 项）机器校验 + 独立外部复核，笔记进 Obsidian 库 |
| 引用整理 | 一键导出 BibTeX / RIS，作者卷期页按 DOI 从 Crossref 自动补齐 |

**顶刊池（22 刊）**：TR Part A/B/C/D/E、Transportation Science、Operations Research、
Management Science、M&SOM、POM、IJOC、Mathematics of OR、Networks、EJOR、COR、OMEGA、
IEEE TITS / TEVC / TASE / TCYB / IoT-J / TII。
清单可在 `PaperPilot/_internal/scripts/or-paper-radar/config/radar_profile.json` 自行增删。

---

## 目录结构

```
PaperPilot/
├── PaperPilot.exe          # 主程序，双击启动
├── _internal/              # 运行库，必须与 exe 同级，不要拆分
├── data/                   # 运行时数据（addr.txt、日志、DOI 索引）
├── 使用说明.md
├── 软件说明与处理流程.md
├── THIRD-PARTY-NOTICES.md  # 第三方组件许可声明
└── LICENSE                 # AGPL-3.0 全文
```

---

## 配置

首次打开可在界面「设置」里填写（也可复制 `_internal/config.local.example.toml` 为 `config.local.toml` 编辑，**界面设置优先级更高**）：

| 项 | 说明 |
|---|---|
| API Key | OpenAI 兼容接口 Key。**不填也能用检索 / 下载 / 入库**，仅「打分排序」和「精读笔记」不可用 |
| Base URL | 接口地址，例如 `https://你的接口域名/v1/chat/completions` |
| 模型 | 填好上面两项后点 🔄 自动列出可用模型 |
| 文献库位置 | PDF 与笔记存放位置，默认 `文档\PaperPilotLibrary` |
| OpenAlex API Key | 可选，填了每天 $1 独立额度 |
| 机构门户 opid | 可选，所在学校 / 机构的 EBSCO 门户标识 |

⚠️ `settings.json` 会保存你的 API Key，**不要把这个文件提交到仓库或发给别人**。

---

## ⚠️ 合规声明（请务必阅读）

本工具的多通道下载设计**以使用者的合法访问权限为前提**：

1. **开放获取（OA）直链** —— 出版商公开提供的免费全文，无附加条件。
2. **机构订阅通道（EBSCO 等）** —— 仅在你所在机构已购买该数据库、且你本人具有合法访问权限时可用。校外需先用学校账号登录一次，登录态保存在软件专用目录中。
3. **出版商页借道** —— 依赖你本人的合法身份（校园网 IP 或已登录的机构账号）获取你有权访问的正式版 PDF。
4. **Sci-Hub 兜底通道** —— **默认关闭**。仅在开放获取 / 机构通道 / 出版商直链均无法获取时才会被考虑，需在「设置 → 高级」中由使用者自行勾选开启。**该通道的合法性与合规性因国家 / 地区、机构政策而异，是否启用完全由使用者自行判断并自行承担全部责任。** 作者不鼓励、也不为任何违反当地法律或机构规定的使用行为负责。

**建议**：仅用本工具获取你已经合法获得访问权限的文献（开放获取、机构订阅、已单独购买的作品）。

本仓库**不包含任何 API Key、账号密码、机构凭据或个人身份信息**，已在发布前完成扫描确认。

---

## 许可证

本项目以 **GNU Affero General Public License v3.0** 提供，全文见 [`PaperPilot/LICENSE`](PaperPilot/LICENSE)。
第三方组件许可见 [`PaperPilot/THIRD-PARTY-NOTICES.md`](PaperPilot/THIRD-PARTY-NOTICES.md)
（含 PyMuPDF / MuPDF —— 这也是本项目采用 AGPL 的直接原因）。

**关于 AGPL 的「对应源码」义务**：AGPL-3.0 要求分发对象代码时，接收者必须能够获取对应的完整源码。
本仓库当前包含的是 PyInstaller 打包产物（对象代码），其中**随包附带了部分脚本源码**
（位于 `PaperPilot/_internal/scripts/` 下的 8 个 `.py` 文件）；
**主程序入口等其余源码需由维护者补充到本仓库**，或通过 `THIRD-PARTY-NOTICES.md`
中载明的书面索取渠道提供（作者承诺三年内有效）。

---

## 相关链接

- 网页版（轻量在线体验，无需安装）：<https://paperpilot-radar.app.workbuddy.host/?sharecode=TIEYTILAQ4>
- 文献数据来源：[OpenAlex](https://openalex.org)（开放学术索引，CC0）
