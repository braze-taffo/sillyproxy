#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    printf '%s\n' '用法: sudo bash deploy/install.sh [--credentials-file PATH] [--bind ADDRESS] [--port PORT]'
}

here=$(cd -- "$(dirname -- "$0")" && pwd -P)
args=()
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --credentials-file|--bind|--port)
            if (($# < 2)) || [[ "$2" == --* ]]; then usage >&2; exit 2; fi
            args+=("$1" "$2")
            shift 2 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if ((EUID != 0)) || [[ ! -d /run/systemd/system ]]; then
    printf '%s\n' '请在运行 systemd 的 Linux 电脑上用 sudo 或 root 启动。' >&2
    exit 1
fi
for command in google-chrome python3 Xvfb xauth xdpyinfo x11vnc websockify curl flock; do
    command -v "$command" >/dev/null || { printf '缺少 %s\n' "$command" >&2; exit 1; }
done
[[ -f /usr/share/novnc/vnc.html ]] || { printf '%s\n' '缺少 noVNC 网页资源。' >&2; exit 1; }

name=gemini-playground
app="/opt/$name"
settings="/etc/$name"
unit="$name.service"
login="$name-login.service"
install -d -m 0700 "$settings"
install -d -m 0755 "$app" "$app/releases"
exec 9>"$settings/.install.lock"
flock -n 9 || { printf '%s\n' '另一次安装正在运行。' >&2; exit 1; }
if systemctl is-active --quiet "$login"; then
    printf '%s\n' '先结束正在运行的登录窗口，再更新安装。' >&2
    exit 1
fi
if ! id "$name" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "/var/lib/$name" --shell /usr/sbin/nologin "$name"
fi
snapshot=$(mktemp -d "$settings/.install.XXXXXXXX")
release=$(mktemp -d "$app/releases/release.XXXXXXXX")
files=("$settings/config.json" "/etc/systemd/system/$unit" "/etc/systemd/system/$login")
previous=
started=0
active=0
enabled=0
if [[ -L "$app/current" ]]; then
    previous=$(readlink "$app/current")
elif [[ -e "$app/current" ]]; then
    printf '%s\n' '安装路径下已有其它目录。' >&2
    exit 1
fi
if systemctl is-active --quiet "$unit"; then active=1; fi
if systemctl is-enabled --quiet "$unit"; then enabled=1; fi

put_file() {
    local source=$1 target=$2 mode=$3 temporary
    temporary=$(mktemp "$target.XXXXXXXX")
    if ! install -m "$mode" -- "$source" "$temporary" || ! mv -fT -- "$temporary" "$target"; then
        rm -f -- "$temporary"
        return 1
    fi
}

point_current() {
    local target=$1
    ln -s -- "$target" "$release/.next-current" &&
        mv -fT -- "$release/.next-current" "$app/current"
}

finish() {
    local result=$? index path failed=0
    trap - EXIT
    if ((result != 0 && started)); then
        systemctl stop "$unit" || failed=1
        if ((!enabled)); then systemctl disable "$unit" || failed=1; fi
        if [[ -n "$previous" ]]; then point_current "$previous" || failed=1
        else rm -f -- "$app/current" || failed=1
        fi
        for index in "${!files[@]}"; do
            path=${files[$index]}
            if [[ -f "$snapshot/old-$index" ]]; then
                put_file "$snapshot/old-$index" "$path" "$(stat -c %a "$snapshot/old-$index")" || failed=1
            else rm -f -- "$path" || failed=1
            fi
        done
        systemctl daemon-reload || failed=1
        if ((active)); then systemctl restart "$unit" || failed=1; fi
    fi
    if ((failed)); then
        printf '自动恢复未完成。备份位于 %s。\n' "$snapshot" >&2
    else
        rm -rf -- "$snapshot"
        if ((result != 0)); then rm -rf -- "$release"; fi
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for index in "${!files[@]}"; do
    path=${files[$index]}
    if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
        printf '无法安全替换 %s\n' "$path" >&2
        exit 1
    fi
    if [[ -f "$path" ]]; then cp -p -- "$path" "$snapshot/old-$index"; fi
done
listen=$(python3 "$here/configure.py" --current "${files[0]}" --output "$snapshot/config.json" "${args[@]}")
read -r host port <<< "$listen"
case "$host" in
    0.0.0.0) host=127.0.0.1 ;;
    ::) host='[::1]' ;;
    *:*) host="[$host]" ;;
esac

umask 022
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -r "$here/../requirements.txt"
for source in gateway.py playground.py desktop.py display.py; do
    install -m 0644 "$here/../$source" "$release/$source"
done
(cd "$release" && .venv/bin/python -c 'import sys; from gateway import load_config; load_config(sys.argv[1])' "$snapshot/config.json")
umask 077
chmod 0755 "$release"
started=1
point_current "$release"
put_file "$snapshot/config.json" "${files[0]}" 0600
put_file "$here/$unit" "${files[1]}" 0644
put_file "$here/$login" "${files[2]}" 0644
systemctl daemon-reload
systemctl reset-failed "$unit" >/dev/null 2>&1 || true
systemctl restart "$unit"
for _ in {1..30}; do
    if systemctl is-active --quiet "$unit" &&
       curl --noproxy '*' -fsS --max-time 1 "http://$host:$port/healthz" 2>/dev/null |
           grep -Fq '"service": "gemini-playground"'; then
        systemctl enable "$unit"
        printf '网关启动成功，监听 %s。下一步是首次登录。\n' "$listen"
        exit 0
    fi
    sleep 1
done
printf '本机健康检查失败。运行 journalctl -u %s 查看原因。\n' "$unit" >&2
exit 1
