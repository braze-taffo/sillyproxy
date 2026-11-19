import base64
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def deployment_key(text):
    fields = text.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError("请选择 Ed25519 公钥。")
    blob = base64.b64decode(fields[1], validate=True)
    if len(blob) != 51 or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ValueError("公钥格式错误。")
    return f"ssh-ed25519 {fields[1]} gemini-playground-deploy"


def stage(payload, home=None):
    home = home or Path.home()
    key = payload["client_api_key"]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
        raise ValueError("客户端 Key 格式错误。")
    public = deployment_key(payload["public_key"]) if payload.get("public_key") else ""
    directory = home / ".cache" / "gemini-playground"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("凭据目录不能是符号链接。")
    directory.chmod(0o700)
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"client_api_key": key}, output)
            output.write("\n")
        os.replace(temporary, directory / "setup.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    if public:
        import fcntl

        ssh = home / ".ssh"
        ssh.mkdir(mode=0o700, exist_ok=True)
        if ssh.is_symlink():
            raise ValueError("SSH 目录不能是符号链接。")
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(ssh / "authorized_keys", flags, 0o600), "ab+") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("SSH 授权文件不是普通文件。")
            fcntl.flock(file, fcntl.LOCK_EX)
            file.seek(0)
            previous = file.read()
            encoded = public.encode()
            if encoded not in previous.splitlines():
                if previous and not previous.endswith(b"\n"):
                    file.write(b"\n")
                file.write(encoded + b"\n")
                file.flush()
                os.fsync(file.fileno())


if __name__ == "__main__":
    try:
        data = sys.stdin.buffer.read(8193)
        if len(data) > 8192:
            raise ValueError("Payload too large.")
        stage(json.loads(data))
    except (OSError, TypeError, KeyError, ValueError):
        print("STAGE_FAILED", file=sys.stderr)
        sys.exit(1)
    print("STAGE_OK")
