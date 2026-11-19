"""One local window for SSH pairing and the temporary server desktop."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import select
import shlex
import socket
import socketserver
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.request import ProxyHandler, build_opener
import webbrowser

from stage import deployment_key, stage


PORT = 16080
REMOTE = "python3 -c " + shlex.quote(Path(__file__).with_name("stage.py").read_text(encoding="utf-8"))
SSH = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


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
    def __init__(self, local=False, alias=None, identity=None):
        self.local, self.alias = local, alias
        self.ssh = [*SSH, "-i", str(identity), "-o", "IdentitiesOnly=yes"] if identity else SSH
        self.client = self.forward = self.process = None

    def connect(self, fields, payload, confirm):
        data = json.dumps(payload).encode()
        if self.local:
            stage(payload)
            return ""
        if self.alias:
            result = subprocess.run([*self.ssh, "--", self.alias, REMOTE], input=data, capture_output=True, timeout=25, **WINDOW)
            if result.returncode or not result.stdout.rstrip().endswith(b"STAGE_OK"):
                raise ValueError("通过已有 SSH 保存失败。请让 AI 检查连接和服务器 Python 3。")
            return ""
        import paramiko

        class FirstContact(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                if not confirm(hostname, fingerprint(key)):
                    raise ValueError("服务器指纹未确认。")

        host, port, user, password = fields
        if self.forward:
            self.forward.shutdown()
            self.forward.server_close()
            self.forward = None
        if self.client:
            self.client.close()
            self.client = None
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(FirstContact())
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
            return fingerprint(client.get_transport().get_remote_server_key())
        except paramiko.BadHostKeyException:
            client.close()
            raise ValueError("服务器指纹与之前保存的记录不符。") from None
        except paramiko.AuthenticationException:
            client.close()
            raise ValueError("SSH 登录失败。请检查用户名或密码。") from None
        except BaseException:
            client.close()
            raise

    def open_desktop(self):
        port = PORT
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
        elif not self.local:
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
    session = Connection(args.local, args.ssh_alias, args.identity)
    ready = False
    busy = False
    access = secrets.token_urlsafe(32)
    labels = {True: "在本机准备配置", False: "连接到服务器"}
    ttk.Label(panel, text=labels[args.local], font=("TkDefaultFont", 15, "bold")).grid(row=row, sticky="w", pady=(0, 10))
    row += 1
    entries = []
    if not (args.local or args.ssh_alias):
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

    def confirm(host, value):
        done, answer = threading.Event(), []
        events.put(("confirm", (host, value, done, answer)))
        return done.wait(180) and bool(answer[0])

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
        run("connected", lambda: session.connect(fields, {"client_api_key": access, "public_key": public}, confirm))

    def copy():
        root.clipboard_clear()
        root.clipboard_append(access)
        status.set("客户端 API Key 已复制，请保存到聊天工具或密码管理器。")

    def poll():
        nonlocal busy, ready
        while not events.empty():
            action, value = events.get_nowait()
            if action == "confirm":
                host, value, done, answer = value
                answer.append(messagebox.askokcancel(
                    "核对 SSH 指纹", f"{host}\n{value}\n\n请与云控制台或管理员提供的指纹核对。",
                    default=messagebox.CANCEL, parent=root,
                ))
                done.set()
                continue
            busy = False
            if action == "connected":
                ready = True
                status.set("连接成功。请复制客户端 API Key，告诉 AI「已连接」并保持窗口打开。")
                if value:
                    identity.set("SSH 指纹，可发给 AI 核对\n" + value)
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
    identity = tk.StringVar(root)
    ttk.Label(panel, textvariable=identity, wraplength=418).grid(row=row + 4, sticky="w", pady=(6, 0))

    def close():
        if not busy:
            session.close()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--ssh-alias")
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--user", default="")
    args = parser.parse_args()
    if args.local and (os.name != "posix" or not Path("/proc").exists() or args.public_key):
        parser.error("本机安装要求 Linux。")
    if args.identity and not args.ssh_alias:
        parser.error("--identity 需要 --ssh-alias。")
    if args.ssh_alias and (args.ssh_alias.startswith("-") or any(c.isspace() for c in args.ssh_alias)):
        parser.error("SSH 目标格式错误。")
    try:
        public = deployment_key(args.public_key.read_text(encoding="utf-8")) if args.public_key else ""
        root = tk.Tk()
    except (OSError, ValueError, tk.TclError):
        parser.exit(1, "请检查公钥文件，并在用户可见的桌面运行连接助手。\n")
    window(root, args, public)
    root.mainloop()


if __name__ == "__main__":
    main()
