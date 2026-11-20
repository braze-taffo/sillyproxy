import argparse
from contextlib import closing
import ipaddress
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from configure import protected_json


AUTH_COOKIES = ("SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID")


def find_auth_cookie_profile(profile_root):
    root = Path(profile_root)
    if not root.is_dir():
        return None
    candidates = [root / "Default"]
    candidates.extend(
        sorted(entry for entry in root.iterdir() if entry.is_dir() and entry.name.startswith("Profile "))
    )
    placeholders = ",".join("?" for _ in AUTH_COOKIES)
    query = (
        f"SELECT 1 FROM cookies WHERE host_key LIKE '%google.com' "
        f"AND name IN ({placeholders}) LIMIT 1"
    )
    for profile in candidates:
        for rel in ("Cookies", "Network/Cookies"):
            db = profile / rel
            if not db.is_file():
                continue
            for uri in (f"file:{db}?mode=ro", f"file:{db}?mode=ro&immutable=1"):
                try:
                    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as con:
                        if con.execute(query, AUTH_COOKIES).fetchone():
                            return profile.name
                    break
                except sqlite3.Error:
                    continue
    return None


def wait_for_cookies(profile_root, timeout):
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        found = find_auth_cookie_profile(profile_root)
        if found:
            print(f"已检测到 Google 登录态写入磁盘（{found}）。")
            return 0
        if time.monotonic() >= deadline:
            print("尚未检测到 Google 登录 Cookie 写入磁盘，请保持登录窗口打开并确认已完成登录或刷新页面后重试。", file=sys.stderr)
            return 1
        time.sleep(2)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/gemini-playground/config.json")
    parser.add_argument("--model", default="gemini-playground", help="model id sent to the gateway")
    parser.add_argument("--generate", action="store_true", help="send one normal and one streaming test prompt")
    parser.add_argument("--wait-cookies", action="store_true", help="wait for Chrome to flush Google login cookies to disk before stopping the login window")
    parser.add_argument("--profile-dir", default="/var/lib/gemini-playground/chrome-profile")
    parser.add_argument("--wait-timeout", type=int, default=40)
    args = parser.parse_args()
    if args.wait_cookies:
        return wait_for_cookies(args.profile_dir, args.wait_timeout)
    cfg = protected_json(args.config)
    bind = ipaddress.ip_address(cfg["bind"])
    if bind.is_unspecified:
        bind = ipaddress.ip_address("::1" if bind.version == 6 else "127.0.0.1")
    host = f"[{bind}]" if bind.version == 6 else str(bind)
    base = f"http://{host}:{int(cfg['port'])}"
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(path, auth=True, body=None):
        headers = {"Authorization": "Bearer " + cfg["client_api_key"]} if auth else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return opener.open(Request(base + path, headers=headers, data=body), timeout=210)

    with get("/healthz", auth=False) as res:
        health = json.load(res)
        if health.get("service") != "gemini-playground":
            raise ValueError("Wrong service.")
        if health.get("version"):
            print("网关版本", health["version"])
    try:
        with get("/v1/models", auth=False):
            raise ValueError("Missing authentication.")
    except HTTPError as exc:
        if exc.code != 401:
            raise
        exc.close()
    with get("/v1/models") as res:
        models = [entry["id"] for entry in json.load(res)["data"]]
        if args.model not in models:
            raise ValueError("Wrong model.")
        print("可用模型", ", ".join(models))
    with get("/status") as res:
        status = json.load(res)
        print("浏览器状态", status["state"])
        if status["state"] != "ready":
            return 1
    if not args.generate:
        return 0
    for streaming in False, True:
        payload = json.dumps({
            "model": args.model, "stream": streaming,
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        }).encode()
        with get("/v1/chat/completions", body=payload) as res:
            if streaming:
                if res.headers.get_content_type() != "text/event-stream":
                    raise ValueError("Expected event stream.")
                text = []
                for line in res:
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    event = json.loads(data)
                    if "error" in event:
                        raise ValueError("Upstream streaming failed.")
                    for choice in event["choices"]:
                        text.append(choice["delta"].get("content", ""))
                else:
                    raise ValueError("Missing completion marker.")
                value = "".join(text)
            else:
                value = json.load(res)["choices"][0]["message"]["content"]
            if not value.strip():
                raise ValueError("No text returned.")
            print("SSE 生成通过。" if streaming else "普通生成通过。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(verify())
    except HTTPError as exc:
        print(f"网关返回 HTTP {exc.code}。", file=sys.stderr)
        sys.exit(1)
    except (URLError, OSError, ValueError, KeyError, TypeError, IndexError):
        print("验证失败。请检查网关服务和浏览器状态。", file=sys.stderr)
        sys.exit(1)
