import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat


def protected_json(path):
    with Path(path).open("rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError("配置须为权限 600 的普通文件。")
        value = json.load(file)
        if not isinstance(value, dict):
            raise ValueError("配置须为 JSON 对象。")
        return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--bind", type=ipaddress.ip_address)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    previous = protected_json(args.current) if args.current.exists() else {}
    staged = protected_json(args.credentials_file) if args.credentials_file else {}
    if args.credentials_file and set(staged) != {"client_api_key"}:
        raise ValueError("凭据文件格式错误。")
    config = {
        "bind": str(args.bind or previous.get("bind", "0.0.0.0")),
        "port": args.port if args.port is not None else previous.get("port", 8788),
        "client_api_key": staged.get("client_api_key", previous.get("client_api_key")) or secrets.token_urlsafe(32),
    }
    models = previous.get("upstream_models")
    if models is not None:
        if (not isinstance(models, list) or not 1 <= len(models) <= 8
                or not all(isinstance(m, str) and re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,60}", m) for m in models)):
            raise ValueError("upstream_models 格式错误。")
        config["upstream_models"] = models
    if type(config["port"]) is not int or not 1024 <= config["port"] <= 65535:
        raise ValueError("端口必须在 1024 到 65535 之间。")
    if not isinstance(config["client_api_key"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", config["client_api_key"]):
        raise ValueError("客户端 API Key 格式错误。")
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(config, file)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    print(config["bind"], config["port"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
