"""One local window for SSH pairing and the temporary server desktop."""
import argparse
import json
import os
from pathlib import Path
import queue
import re
import secrets
import select
import shlex
import socket
import socketserver
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk
from urllib.request import ProxyHandler, build_opener
import webbrowser

from stage import deployment_key


PORT = 16080
REMOTE = "python3 -c " + shlex.quote(Path(__file__).with_name("stage.py").read_text(encoding="utf-8"))
SSH = ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class Forward(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, connection):
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    remote = connection.get_transport().open_channel(
                        "direct-tcpip", ("127.0.0.1", PORT), self.client_address, timeout=7,
                    )
                    with remote:
                        sides = {self.request: remote, remote: self.request}
                        while True:
                            for ready in select.select(list(sides), [], [], 20)[0]:
                                data = ready.recv(32768)
                                if not data:
                                    return
                                sides[ready].sendall(data)
                except Exception:
                    return

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


class Connection:
    def __init__(self, alias=None, identity=None):
        self.alias = alias
        self.ssh = [*SSH, "-i", str(identity), "-o", "IdentitiesOnly=yes"] if identity else SSH
        self.client = self.forward = self.process = None

    def connect(self, fields, payload):
        data = json.dumps(payload).encode()
        if self.alias:
            result = subprocess.run([*self.ssh, "--", self.alias, REMOTE], input=data, capture_output=True, timeout=25, **WINDOW)
            if result.returncode or not result.stdout.rstrip().endswith(b"STAGE_OK"):
                raise ValueError("通过已有 SSH 保存失败。请让 AI 检查连接和服务器 Python 3。")
            return
        import paramiko

        host, port, user, password = fields
        if self.forward:
            self.forward.shutdown()
            self.forward.server_close()
            self.forward = None
        if self.client:
            self.client.close()
            self.client = None
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(host, port=port, username=user, password=password or None,
                           allow_agent=not password, look_for_keys=not password, timeout=8,
                           banner_timeout=8, auth_timeout=12)
            client.get_transport().set_keepalive(30)
            stdin, stdout, _ = client.exec_command(REMOTE, timeout=12)
            stdin.write(data)
            stdin.channel.shutdown_write()
            if stdout.read(4097).strip() != b"STAGE_OK" or stdout.channel.recv_exit_status():
                raise ValueError("SSH 已连接，但保存配置失败。请检查服务器 Python 3 和目录权限。")
            self.client = client
        except paramiko.AuthenticationException:
            client.close()
            raise ValueError("SSH 登录失败。请检查用户名或密码。") from None
        except BaseException:
            client.close()
            raise

    def open_desktop(self):
        if self.alias:
            if self.process is None or self.process.poll() is not None:
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                self.process = subprocess.Popen(
                    [*self.ssh, "-N", "-o", "ExitOnForwardFailure=yes", "-L",
                     f"127.0.0.1:{port}:127.0.0.1:{PORT}", "--", self.alias],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **WINDOW,
                )
                self.local_port = port
            port = self.local_port
        else:
            if self.client is None or not self.client.get_transport().is_active():
                raise ValueError("SSH 连接已断开。请重新连接。")
            if self.forward is None:
                self.forward = Forward(self.client)
            port = self.forward.server_address[1]
        url = f"http://127.0.0.1:{port}/vnc.html"
        opener = build_opener(ProxyHandler({}))
        for _ in range(12):
            try:
                with opener.open(url, timeout=1) as response:
                    if b"noVNC" in response.read(4096):
                        return url + "?autoconnect=true&resize=scale"
            except OSError:
                pass
            time.sleep(.25)
        raise ValueError("登录窗口尚未启动。等 AI 确认后再重试。")

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
        if self.forward:
            self.forward.shutdown()
            self.forward.server_close()
        if self.client:
            self.client.close()


def window(root, args, public):
    root.title("Gemini Playground 安装助手")
    root.minsize(465, 0)
    root.columnconfigure(0, weight=1)
    panel = ttk.Frame(root, padding=22)
    panel.grid(sticky="nsew")
    panel.columnconfigure(0, weight=1)
    row = 0
    events = queue.Queue()
    session = Connection(args.ssh_alias, args.identity)
    ready = False
    busy = False
    access = secrets.token_urlsafe(32)
    ttk.Label(panel, text="连接到服务器", font=("TkDefaultFont", 15, "bold")).grid(row=row, sticky="w", pady=(0, 10))
    row += 1
    entries = []
    if not args.ssh_alias:
        for title, value, mask in (
            ("服务器 IP 或主机名", args.host, ""), ("SSH 端口", str(args.port), ""),
            ("SSH 用户名", args.user, ""), ("SSH 密码（已有密钥时可留空）", "", "*"),
        ):
            ttk.Label(panel, text=title).grid(row=row, sticky="w")
            var = tk.StringVar(root, value=value)
            entry = ttk.Entry(panel, textvariable=var, show=mask)
            entry.grid(row=row + 1, sticky="ew", ipady=4, pady=(2, 7))
            if not entries:
                entry.focus_set()
            entries.append(var)
            row += 2
    status = tk.StringVar(root, value="连接后等待 AI 安装。登录时会打开服务器上的 Chrome。")
    ttk.Label(panel, textvariable=status, wraplength=418).grid(row=row, sticky="w", pady=(3, 8))

    def run(action, operation):
        nonlocal busy
        if busy:
            return
        busy = True
        status.set("正在处理，请稍候…")
        connect.state(["disabled"])
        login.state(["disabled"])

        def work():
            try:
                events.put((action, operation()))
            except ImportError:
                events.put(("error", "缺少 SSH 库。请让 AI 安装 tools/requirements.txt。"))
            except ValueError as exc:
                events.put(("error", str(exc)))
            except Exception:
                events.put(("error", "操作失败。请检查连接并让 AI 查看服务状态。"))
        threading.Thread(target=work, daemon=True).start()

    def start():
        fields = None
        if entries:
            host, port, user, password = (entry.get() for entry in entries)
            try:
                fields = (host.strip().strip("[]"), int(port), user.strip(), password)
                if not fields[0] or not fields[2] or not 1 <= fields[1] <= 65535:
                    raise ValueError()
            except ValueError:
                status.set("请检查服务器地址、端口和用户名。")
                return
            entries[-1].set("")
        run("connected", lambda: session.connect(fields, {"client_api_key": access, "public_key": public}))

    def copy():
        root.clipboard_clear()
        root.clipboard_append(access)
        status.set("客户端 API Key 已复制，请保存到聊天工具或密码管理器。")

    def poll():
        nonlocal busy, ready
        while not events.empty():
            action, value = events.get_nowait()
            busy = False
            if action == "connected":
                ready = True
                status.set("连接成功。请复制客户端 API Key，告诉 AI「已连接」并保持窗口打开。")
                copy_button.state(["!disabled"])
                if entries:
                    connect.configure(text="重新连接")
            elif action == "open":
                webbrowser.open(value)
                status.set("请在远程 Chrome 中完成 Google 登录。进入 AI Studio 后告诉 AI「登录好了」。")
            else:
                status.set(value)
            if ready:
                login.state(["!disabled"])
            if entries or not ready:
                connect.state(["!disabled"])
        root.after(100, poll)

    connect = ttk.Button(panel, text="连接并保存配置", command=start)
    copy_button = ttk.Button(panel, text="复制客户端 API Key", command=copy, state="disabled")
    login = ttk.Button(panel, text="打开远程 Google 登录窗口",
                       command=lambda: run("open", session.open_desktop), state="disabled")
    for index, button in enumerate((connect, copy_button, login), row + 1):
        button.grid(row=index, sticky="ew", ipady=5, pady=3)

    def close():
        if not busy:
            session.close()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)


def lock_private_key(path):
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, **WINDOW)
    match = re.search(rb"S-1-[0-9-]+", who.stdout or b"")
    if match:
        sid = match.group(0).decode("ascii")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:F"],
            check=False, capture_output=True, **WINDOW,
        )


def ensure_setup_key(path):
    """Generate (or reuse) an unencrypted OpenSSH Ed25519 key pair without shell quoting issues."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat, load_ssh_private_key,
    )

    private_path = Path(path)
    public_path = private_path.with_name(private_path.name + ".pub")
    if private_path.exists() or public_path.exists():
        if not (private_path.is_file() and public_path.is_file()):
            raise ValueError("目标密钥路径已有不完整文件，请换一个隔离路径。")
        pub_text = public_path.read_text(encoding="utf-8").strip()
        if not pub_text.endswith(" gemini-playground-deploy"):
            raise ValueError("目标路径已存在其它密钥文件，请换一个隔离路径。")
        try:
            loaded = load_ssh_private_key(private_path.read_bytes(), password=None)
        except Exception as exc:
            raise ValueError("现有临时私钥无法以无密码方式读取，请删除后重新生成。") from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError("现有临时私钥不是 Ed25519 格式。")
        expected = loaded.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
        normalized = deployment_key(pub_text)
        if normalized.split()[1] != expected.split()[1]:
            raise ValueError("现有临时公钥与私钥不匹配。")
        lock_private_key(private_path)
        return normalized

    private_path.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
    public_bytes = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    public_line = deployment_key(public_bytes.decode("ascii"))
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(private_bytes)
        lock_private_key(private_path)
        public_path.write_text(public_line + "\n", encoding="utf-8", newline="\n")
    except BaseException:
        private_path.unlink(missing_ok=True)
        public_path.unlink(missing_ok=True)
        raise
    return public_line


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh-alias")
    parser.add_argument("--identity", type=Path)
    key_group = parser.add_mutually_exclusive_group()
    key_group.add_argument("--public-key", type=Path)
    key_group.add_argument("--setup-key", type=Path)
    parser.add_argument("--keygen-only", action="store_true")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="")
    args = parser.parse_args()
    if args.identity and not args.ssh_alias:
        parser.error("--identity 需要 --ssh-alias。")
    if args.keygen_only and not args.setup_key:
        parser.error("--keygen-only 需要 --setup-key。")
    if args.ssh_alias and (args.ssh_alias.startswith("-") or any(c.isspace() for c in args.ssh_alias)):
        parser.error("SSH 目标格式错误。")
    try:
        if args.setup_key:
            public = ensure_setup_key(args.setup_key)
        elif args.public_key:
            public = deployment_key(args.public_key.read_text(encoding="utf-8"))
        else:
            public = ""
    except ImportError:
        parser.exit(1, "缺少 SSH 依赖库，请先安装 tools/requirements.txt。\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    if args.keygen_only:
        print(args.setup_key.with_name(args.setup_key.name + ".pub"))
        return
    try:
        root = tk.Tk()
    except tk.TclError:
        parser.exit(1, "无法打开图形窗口，请在用户可见的桌面运行连接助手。\n")
    window(root, args, public)
    root.mainloop()


if __name__ == "__main__":
    main()
