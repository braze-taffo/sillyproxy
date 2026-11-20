# SillyProxy — Gemini Playground 网关

用自己的 Google 账号登录 AI Studio 网页。网关复用这份浏览器登录状态，通过网页聊天界面提交提示词，再把回答提供给 OpenAI 兼容客户端。**无需创建 Gemini API Key。**

仓库地址：<https://github.com/braze-taffo/sillyproxy>

~~~sh
git clone https://github.com/braze-taffo/sillyproxy
~~~

## 给 AI 安装

把本仓库的 GitHub 链接发给一个能操作终端的 AI，请它阅读 README 和 [AI 安装指引](INSTALL_WITH_AI.md) 后带你安装。你只需要提供服务器的连接方式，并亲自在远程 Chrome 里登录 Google。

1. AI 先问清楚服务器的连接方式和系统，再使用已有 SSH 连接，或在你需要输入密码时在你的电脑上打开安装助手。密码只在窗口里输入。
2. AI 安装服务并开启临时登录窗口。你在安装助手里点「打开远程 Google 登录窗口」，在 Chrome 里完成登录，进入 AI Studio 后告诉 AI「登录好了」。
3. AI 结束临时窗口，启动网关并验证。把安装助手生成的客户端 API Key 填到你的客户端。

| 客户端字段 | 填写内容 |
|---|---|
| 接口类型 | OpenAI 兼容 |
| Base URL | `http://服务器公网IP:8788/v1` |
| API Key | 安装助手生成的客户端 API Key |
| 模型 | `gemini-playground`（自动挑选），或 `/v1/models` 里列出的具体模型 |

公网 IP 和端口即可使用。如果云服务器只开放标准 HTTP，安装指引提供端口 80 的配置，客户端填 `http://服务器公网IP/v1`。HTTP 会明文传输客户端 Key 和请求内容，可在云安全组限制来源 IP。已有 HTTPS 入口也可以接入。客户端附带的温度（`temperature`）、最大长度（`max_tokens`）、流式用量统计（`stream_options`）等额外参数会被网关静默忽略，统一使用 AI Studio 网页默认设置。

## 工作原理

网关直接启动系统安装的 Google Chrome，而不是让 Playwright 启动它：用正常桌面 UA、去掉自动化标记，并以 `--headless=new` 运行。Playwright 只通过调试端口接入。AI Studio 的 BotGuard 会拒绝自动化环境，这一步是能否生成的关键。

生成时网关在网页输入框填入提示词并点击运行，同时注入脚本旁路读取页面自己发出的 `GenerateContent` 请求流。网关按 AI Studio 的 protobuf-as-JSON 帧格式解析出正文，因此回答是网页真实文本，不含「Model 21:04」之类的界面表头，思维链摘要也会被过滤。流式请求按帧即时转发，是真正的增量 SSE。

Google 偶尔会拒绝某次发送（`403 The caller does not have permission`），而同一页面紧接着再发就能成功，所以网关会在同一个已就绪的页面上自动重发，重发次数用尽才把错误交给客户端。已经转发过内容的流不会重发，避免重复输出。

## 模型选择

`GET /v1/models` 返回网关提供的全部模型。客户端在请求里写哪个 `model`，就用哪个：

- 写具体模型名（如 `gemini-4-hydrogen`），网关**只用这个模型**，它不可用时如实返回错误，不会偷偷换别的。
- 写 `gemini-playground`，网关按配置里的 `upstream_models` 顺序自动挑一个可用的，适合不想关心模型的客户端。

`upstream_models` 决定“自动挑选”的候选范围，也决定 `/v1/models` 里列出的具体模型：

~~~json
"upstream_models": ["gemini-4-hydrogen", "gemini-4-neon", "gemini-4-argon"]
~~~

改完重启网关生效。不同 Google 账号、不同时段对各模型的授权和额度不同：常见表现是某模型 403「无权限」，或 429「达到该模型的使用上限（若干小时后重置）」。这属于 Google 侧限制，换一个有权限、还有额度的模型即可。
