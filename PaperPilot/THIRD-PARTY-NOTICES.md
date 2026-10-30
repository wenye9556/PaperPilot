# 源码与第三方组件声明

**PaperPilot 论文领航台 v1.0.0**

本程序以 **GNU Affero General Public License v3.0（AGPL-3.0）** 提供，完整条款见同目录的 `LICENSE`。
你可以自由使用、复制、修改、再分发本程序；分发修改版时，必须以同一许可证提供对应源码。

## 一、对应源码（Corresponding Source）怎么拿

AGPL-3.0 要求：分发本程序的对象代码时，接收者必须能拿到对应的完整源码。以下任一方式均可：

1. **随包源码**：与本程序同版本号的源码包（`PaperPilot_source_v1.0.0.zip`），通常与本程序一起提供。
2. **项目仓库**：`https://github.com/wenye/PaperPilot`
   （若该地址失效，见第 3 条；作者承诺至少保持一条渠道长期有效。）
3. **书面索取**：向把本程序提供给你的人提出请求，作者承诺在 **三年内** 有效，收费不超过实际分发成本。

版本对应关系：本包为 `v1.0.0`，源码包版本号与本包 `关于` 面板显示的版本号一致。

## 二、第三方组件及其许可证

本程序打包分发了以下开源组件，各自保留其原始许可证与版权声明。

| 组件 | 用途 | 许可证 |
|---|---|---|
| PyMuPDF / MuPDF | PDF 文本提取（本项目采用 AGPL 的直接原因） | AGPL-3.0 或 Artifex 商业许可 |
| Flask / Werkzeug / Jinja2 / itsdangerous / MarkupSafe / click | 本地 HTTP 服务与模板 | BSD-3-Clause |
| requests / websocket-client | HTTP 请求、浏览器调试协议（CDP） | Apache-2.0 |
| urllib3 / PyYAML / charset-normalizer | 传输重试、配置解析、编码识别 | MIT |
| certifi | CA 根证书 | MPL-2.0 |
| numpy / Pillow / lxml / psutil / brotli / greenlet | 被 PyInstaller 连带打包（依赖链引入） | BSD-3-Clause / MIT / Apache-2.0 等 |
| pywin32 | Windows 集成（部分版本） | PSF-style |
| PyInstaller | **仅用于构建**，不构成运行时可替换组件 | GPLv2 + 官方打包例外（不使被打包程序受 GPL 约束） |

> 本表用于指引，不替代各组件上游的许可证正文。若你的用途触发额外义务（例如商业分发
> PyMuPDF），请以 Artifex 官方条款为准：https://artifex.com/licensing/

## 三、本程序不包含什么

- 不含任何 API Key、账号、机构门户 id 或订阅权限；
- 不托管、不传播任何论文全文，不做遥测，不上传你的文献库；
- 检索元数据来自公开 API（OpenAlex / Crossref / arXiv），受各自条款约束。

## 四、免责声明

本程序按 AGPL-3.0「按原样」提供，**无任何担保**。你只能访问你本来就有权访问的内容；
是否使用某条下载通道，取决于你所在机构的订阅条款与当地法规，由使用者自行判断并承担后果。
发现安全问题请勿公开开 issue，见仓库 `SECURITY.md`。
