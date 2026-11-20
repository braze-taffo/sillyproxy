# AI 安装指引

> 当前版本：`v0.1.0`（以仓库根目录 [`VERSION`](VERSION) 为准）

用户发来这个仓库链接并要求安装或更新时，读完本文件再操作。

**读完本文件后，第一件事是停下来问用户，不要先自己扫描本机或服务器的 SSH 配置，也不要试探任何主机。** 本项目面向远程 Linux 服务器部署（Ubuntu 24.04 或 26.04 x86-64）。把下面两个问题一次问清，并在**这一次回复的最后**注明当前版本号（以 [`VERSION`](VERSION) 为准，例如在末尾写「当前版本：v0.1.0」），等用户回答后再动手：

1. 你已经有能直接登录服务器的 SSH 别名或密钥，还是只有公网 IP、端口、用户名和密码？
2. 服务器是什么系统（例如 Ubuntu 24.04 或 26.04）？

用户回答前不要执行连接、探测、安装类命令。每一步先在实际环境核验，再继续。用户让你停在部署测试前时，保持线上环境不变。

## 1. 连接到服务器

按用户的回答二选一。

**用户已有 SSH 别名或密钥**：在用户看得见的电脑打开连接助手。有别名就使用别名，指定私钥时用 `--identity`，SSH 端口等其它参数使用已核对的 SSH 配置。

~~~sh
python3 tools/connect.py --ssh-alias my-server
~~~

**用户只有用户名和密码**：在 AI 运行环境生成一次性的 Ed25519 密钥，私钥保留在该环境，交给用户的文件只有公钥。使用隔离的目标路径生成，避开已有私钥。以 Linux/macOS Shell 为例：

~~~sh
ssh-keygen -q -t ed25519 -N '' -f /path/to/setup-only
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt
.venv/bin/python tools/connect.py --public-key /path/to/setup-only.pub
~~~

连接前核对 SSH 服务器指纹，遇到指纹变化时让用户用云控制台或管理员资料确认。Windows 虚拟环境的 Python 是 `.venv\Scripts\python.exe`，也可用 `py -3` 启动已具备依赖的 Python。首次 SSH 密码只在用户电脑的弹窗里输入。指纹核对成功后，助手把公钥加入授权文件，保存客户端 Key。验证 AI 自己能用新密钥登录，再接手服务器操作。

如果你的 AI 终端在云端，把项目文件和公钥交给用户在自己的电脑启动安装助手。让用户在连接成功后复制客户端 API Key，保持窗口打开，并告诉你「已连接」。部署和登录阶段可复用该窗口的 SSH 连接。实在无法弹窗时，让用户使用自己的 SSH 终端，并用 SSH 隧道打开下文的临时登录界面。SSH 密码和 Google 凭据都由用户自己输入。

服务器安装面向 Ubuntu 24.04 或 26.04 x86-64。先查系统、架构、可用端口以及管理员权限。AI 能非交互使用 sudo 时用 `sudo -n`。已经是 root 就直接运行管理员命令。需要交互 sudo 时，由用户在自己的终端输入密码。

## 2. 安装并启动网关

把当前仓库下载或复制到目标主机。检查 TCP 8788 是否空闲，同时确认登录用的回环端口 15900 和 16080 没有被已有服务占用。根据权限安装缺少的软件包。下面是 Ubuntu 上所需的依赖：

~~~sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl python3 python3-venv xvfb xauth x11-utils x11vnc novnc websockify
~~~

使用系统已有的 Google Chrome，或者从 [Google Chrome 官方下载页](https://www.google.com/chrome/) 获取 Linux Debian/Ubuntu 64 位安装包，交给系统包管理器安装。确认 `/usr/bin/google-chrome` 存在。浏览器以专用的非 root 帐号运行；因为服务本身没有登录会话，Chrome 以 `--no-sandbox` 启动，隔离由 systemd 单元（`ProtectSystem=strict`、`ProtectHome=yes`、`PrivateTmp=yes`、`NoNewPrivileges`）承担。

~~~sh
sudo -n bash deploy/install.sh \
  --credentials-file "$HOME/.cache/gemini-playground/setup.json" \
  --port 8788
~~~

没用连接助手时省去 `--credentials-file`，安装器会生成客户端 Key。用户可在自己的管理员终端读取 `/etc/gemini-playground/config.json` 内的 `client_api_key` 并保存到客户端。AI 终端的检查只输出服务状态。默认绑定 `0.0.0.0`；配合 80 端口转发或已有反向代理时可显式加 `--bind 127.0.0.1`。更新时保留原浏览器资料和已有客户端 Key；只有主动传入新的凭据文件时才更新 Key。

## 3. 打开远程 Chrome，用户亲自登录

~~~sh
sudo -n systemctl start gemini-playground-login.service
curl --noproxy '*' -fsS --max-time 5 --retry 12 --retry-connrefused --retry-delay 1 \
  -o /dev/null http://127.0.0.1:16080/vnc.html
~~~

启动登录服务会暂停网关，并打开独立的普通 Chrome。Chrome 使用专用目录 `/var/lib/gemini-playground/chrome-profile`。登录服务的 VNC 和 noVNC 只监听服务器回环地址。

让用户回到连接助手点「打开远程 Google 登录窗口」。noVNC 会要求一个系统生成的单次登录窗口密码。安装者可以在服务器读取 `/run/gemini-playground-login/vnc-password`，通过当前用户的交互界面交给本人。这个临时密码只在本轮窗口存活期间有效。登录服务最多持续 20 分钟。使用 SSH 隧道打开网页。已有 SSH 连接但没用安装助手时，也可让用户在自己的终端运行：

~~~sh
ssh -N -L 127.0.0.1:16080:127.0.0.1:16080 my-server
~~~

让用户在自己电脑的浏览器打开 `http://127.0.0.1:16080/vnc.html`。用户在远程 Chrome 里亲自输入 Google 凭据、完成验证并进入 AI Studio 聊天页。让用户妥善保管 Chrome 登录资料，并在浏览器提示时选择是否保存 Google 密码。AI 等用户报告「登录好了」，再停止登录窗口并启动网关。这个顺序让 Chrome 有时间写回会话资料。

~~~sh
sudo -n systemctl stop gemini-playground-login.service
sudo -n systemctl start gemini-playground.service
curl --noproxy '*' -fsS --max-time 5 --retry 12 --retry-connrefused --retry-delay 1 \
  http://127.0.0.1:8788/healthz
sudo -n python3 deploy/verify.py
~~~

检查 `deploy/verify.py` 报告的浏览器状态为 `ready`。遇到 Google 提示验证、地区限制、配额用尽或登录失败时，把实际状态告知用户，等待用户在普通浏览器里处理。

AI Studio 的 Cookie 通知挡住运行按钮时，执行器会使用通知中可见的拒绝或关闭按钮。遇到需要用户选择的其它通知，把登录窗口交给用户。

Google 偶尔会拒绝单次发送并返回 `403 The caller does not have permission`，同一页面紧接着重发通常就能成功，执行器会自动重发。重发用尽仍失败时网关才返回该错误，让用户用同一个 Chrome 登录资料和同一个模型手动点「Run」对照网页提示。

返回 `429 google_quota` 表示该模型当天的使用额度已用尽，Google 的提示里会给出重置时间。这时不要反复重试（只会消耗额度），按模型名换一个可用的：编辑 `/etc/gemini-playground/config.json` 的 `upstream_models`，把有权限的模型排到前面，再重启网关。返回 403 且换模型无效时，通常是该账号没有该模型的权限，同样通过 `upstream_models` 选择账号可用的模型。

执行器点「Run」前会回读网页输入框。提示词缺失或被截断时，网关返回 `prompt_not_entered`，该次请求在网页上不会提交。

## 4. 公网连通与验收

确认服务器公网 IP、云安全组、防火墙和 NAT。按用户要求放行 API TCP 端口，例如 TCP 8788，保留 SSH 和其它原有规则。VNC 端口 15900 和 16080 仅用于 SSH 隧道。公网 HTTP 上的 API Key 和请求都是明文。能确定客户端 IP 时可限定来源；已有 HTTPS 转发时可关闭代理响应缓冲，让 SSE 及时到达客户端。

如果云服务器已放行 TCP 80，且主机没有服务占用该端口，可以使用仓库里的标准 HTTP 入口。把网关配置为监听 `127.0.0.1:8788`，再安装这两个单元。此时 Base URL 是 `http://公网IP/v1`。

~~~sh
sudo -n install -m 0644 deploy/gemini-playground-http.{socket,service} /etc/systemd/system/
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now gemini-playground-http.socket
~~~

端口 80 已有网站或网关时，使用它自己的转发配置，保留原有站点。

检查 `gemini-playground.service` 已启动且已设为开机启动，从外部网络访问实际入口的 `/healthz`，确认服务标识为 `gemini-playground`。普通和 SSE 的真实测试各会提交一个网页聊天请求，可使用：

~~~sh
sudo -n python3 deploy/verify.py --generate
sudo -n python3 deploy/verify.py --generate --model gemini-4-hydrogen
~~~

不带 `--model` 时用 `gemini-playground`（自动挑选），带具体模型名时只验证该模型。

交付给用户的客户端配置是公网 Base URL、客户端 API Key 和模型名。模型让用户自己选：`GET /v1/models` 会列出 `gemini-playground`（自动挑选）以及配置里启用的具体模型；客户端填哪个就用哪个。安装成功并确认可以重试或运维后，从登录账号目录删除本次暂存的 `~/.cache/gemini-playground/setup.json`。如本次添加了临时公钥且用户保有自己的原始 SSH 登录方式，仅清理本次新增的完整公钥行。以后需要重新登录，可以重新执行第 3 节。
