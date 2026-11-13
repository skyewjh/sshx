#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sshx —— 常驻 SSH 桥的命令行客户端。

一次 up，之后所有操作复用同一条 SSH 连接（daemon 自动保活/重连）：

    sshx up user@host -p PASS --as box      # 建立常驻连接
    sshx run 'ps aux | grep python'         # 复用（可省略 target）
    sshx run box 'systemctl status gb' -t 20
    sshx put box ./a.py /home/x/a.py
    sshx get box /var/log/syslog ./syslog
    sshx run box './deploy.sh' -d           # 后台启动，不占 channel
    sshx ls / sshx down [box] / sshx ping
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "bridge.py")
HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".sshbridge")
TOKEN_FILE = os.path.join(CONF_DIR, "token")
PORT = int(os.environ.get("SSHX_PORT", "7799"))
BASE = "http://127.0.0.1:%d" % PORT

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$|^[A-Za-z0-9_-]+:[0-9]+$")


def token() -> str:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def http(path, payload=None, timeout=900):
    """返回 (status, obj)；daemon 不可达返回 (0, None)。payload=None => GET。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        method="POST" if data is not None else "GET",
        headers={"X-Sshx-Token": token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"ok": False, "error": body}
    except (urllib.error.URLError, ConnectionError, OSError):
        return 0, None


def ping() -> bool:
    st, obj = http("/v1/ping", None, timeout=3)
    return bool(st == 200 and obj and obj.get("service") == "sshx")


def spawn() -> None:
    os.makedirs(CONF_DIR, exist_ok=True)
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(
        [sys.executable, BRIDGE, "serve", "--port", str(PORT)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, cwd=HERE, creationflags=flags,
    )


def ensure() -> None:
    if ping():
        return
    spawn()
    for _ in range(80):
        time.sleep(0.1)
        if ping():
            return
    sys.stderr.write("sshx: 桥接 daemon 启动失败，看 %s\n" %
                     os.path.join(CONF_DIR, "bridge.log"))
    sys.exit(255)


def call(path, payload=None, timeout=900):
    ensure()
    st, obj = http(path, payload, timeout)
    if obj is None:
        ensure()
        st, obj = http(path, payload, timeout)
    if obj is None:
        sys.stderr.write("sshx: daemon 无响应\n")
        sys.exit(255)
    return obj


def out(s: str) -> None:
    if s:
        sys.stdout.write(s if s.endswith("\n") else s + "\n")


def fail(obj):
    sys.stderr.write("sshx: %s\n" % (obj.get("error") or "unknown error"))
    sys.exit(255)


# ---------------- 命令实现 ----------------

def known_aliases():
    """已知目标名 = daemon 里的活跃连接 ∪ 落盘凭据 ∪ 上次使用。

    daemon 冷启动时 /v1/ls 是空的，若不合并 creds，'sshx run srv ...' 会把
    srv 当成命令的一部分（典型症状: bash: srv: command not found, exit 127）。
    """
    ensure()
    names = set()
    st, obj = http("/v1/ls", None, timeout=5)
    if st == 200 and obj:
        names |= {s["alias"] for s in obj.get("sessions", [])}
        if obj.get("last"):
            names.add(obj["last"])
    try:
        with open(os.path.join(CONF_DIR, "creds.json"), encoding="utf-8") as f:
            names |= set(json.load(f).keys())
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(CONF_DIR, "last"), encoding="utf-8") as f:
            last = f.read().strip()
            if last:
                names.add(last)
    except OSError:
        pass
    return names


def split_args_target(args, aliases):
    """把位置参数拆成 (target|None, 其余)；target 可省略。"""
    if len(args) >= 2 and (args[0] in aliases or TARGET_RE.match(args[0])):
        return args[0], args[1:]
    return None, args


def cmd_up(a):
    payload = {"target": a.target, "user": a.user, "password": a.password,
               "key": a.key, "port": a.port, "alias": a.alias, "save": a.save}
    obj = call("/v1/up", payload, timeout=60)
    if not obj.get("ok"):
        fail(obj)
    print("✓ %s 已连接 %s@%s:%s  (%s认证, %sms)" % (
        obj["alias"], obj["user"], obj["host"], obj["port"],
        "密钥" if obj.get("auth") == "key" else "密码", obj.get("connect_ms")))
    if a.save:
        print("  凭据已落盘，daemon 重启后可自动重连")


def cmd_run(a):
    aliases = known_aliases()
    target, rest = split_args_target(a.args, aliases)
    suspect = None
    if target is None and len(a.args) >= 2 and re.match(r"^[A-Za-z0-9._-]+$", a.args[0]):
        suspect = a.args[0]
    cmd = " ".join(rest)
    if cmd.strip() == "-":
        cmd = sys.stdin.read()
    if not cmd.strip():
        sys.stderr.write("sshx: 缺少要执行的命令\n")
        sys.exit(2)
    obj = call("/v1/run", {"target": target, "cmd": cmd,
                           "timeout": a.timeout, "detach": a.detach},
               timeout=a.timeout + 120)
    if obj.get("error"):
        fail(obj)
    out(obj.get("stdout", ""))
    sys.stdout.flush()          # 状态行必须排在正文之后
    if obj.get("stderr"):
        sys.stderr.write(obj["stderr"])
        sys.stderr.flush()
    if a.detach:
        if obj.get("log"):
            sys.stderr.write("[sshx] 已后台启动 → 日志 %s\n" % obj["log"])
    elif not a.quiet:
        tag = "TIMEOUT" if obj.get("timed_out") else "exit=%s" % obj.get("exit")
        sys.stderr.write("[sshx %s %s %ss]\n" % (
            obj.get("alias"), tag, obj.get("elapsed")))
    rc = obj.get("exit", 0)
    if rc == 127 and suspect:
        sys.stderr.write(
            "sshx: 提示——'%s' 不是已注册目标，被当命令执行了；"
            "若是主机先 sshx up %s -u USER -p PASS\n" % (suspect, suspect))
    sys.exit(0 if rc == 0 else (rc if 0 < rc < 256 else 1))


def _two(a, usage):
    aliases = known_aliases()
    target, rest = split_args_target(a.args, aliases)
    if len(rest) != 2:
        sys.stderr.write("sshx: 用法 %s\n" % usage)
        sys.exit(2)
    return target, rest[0], rest[1]


def local_path(p: str) -> str:
    """把 git-bash 风格路径 (/tmp/x, /c/Users/x) 转成 native Windows 路径。

    daemon 是原生 Python，'/tmp/x' 会被解析成 C:\\tmp\\x；CLI 也可能是原生
    Python，所以这里统一用 cygpath 兜底。"""
    if os.path.exists(p):
        return os.path.abspath(p)
    try:
        r = subprocess.run(["cygpath", "-w", "-a", p],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().replace("\\", "/")
    except Exception:
        pass
    return p


def cmd_put(a):
    target, local, remote = _two(a, "sshx put [target] LOCAL REMOTE")
    obj = call("/v1/put", {"target": target, "local": local_path(local), "remote": remote})
    if not obj.get("ok"):
        fail(obj)
    print("✓ %s ← %s  %d bytes  %.2fs" % (obj["remote"], local, obj["size"], obj["elapsed"]))


def cmd_get(a):
    target, remote, local = _two(a, "sshx get [target] REMOTE LOCAL")
    obj = call("/v1/get", {"target": target, "remote": remote, "local": local_path(local)})
    if not obj.get("ok"):
        fail(obj)
    print("✓ %s → %s  %d bytes  %.2fs" % (remote, obj["local"], obj["size"], obj["elapsed"]))


def cmd_ls(a):
    obj = call("/v1/ls", None)
    sessions = obj.get("sessions") or []
    if not sessions:
        print("(无连接)  用 sshx up user@host -p PASS 建立")
        return
    print("%-16s %-26s %-3s %-7s %-5s %-5s %s" %
          ("ALIAS", "TARGET", "UP", "IDLE", "USES", "重连", "认证"))
    for s in sessions:
        tgt = "%s@%s:%s" % (s["user"], s["host"], s["port"])
        print("%-16s %-26s %-3s %-7s %-5s %-5s %s" % (
            s["alias"], tgt, "✓" if s["up"] else "✗",
            "%ss" % s["idle"], s["uses"], s["reconnects"], s.get("auth", "")))
    if obj.get("last"):
        print("\n默认目标: %s" % obj["last"])
    if any(not s["up"] for s in sessions):
        print("(✗ 的连接已断开，下一条命令会自动重连)")


def cmd_down(a):
    obj = call("/v1/down", {"all": True} if a.all else {"target": a.target})
    print("✓ 断开 %s 条连接" % obj.get("closed", 0))


def cmd_ping(a):
    obj = call("/v1/run", {"target": a.target, "cmd": "echo __pong__", "timeout": 10})
    if obj.get("error"):
        fail(obj)
    ok = "__pong__" in (obj.get("stdout") or "")
    print("%s %s  (%sms)" % ("✓" if ok else "✗", obj.get("alias"),
                             round((obj.get("elapsed") or 0) * 1000)))
    sys.exit(0 if ok else 1)


def cmd_start(a):
    ensure()
    print("✓ daemon 在 127.0.0.1:%d (token: %s)" % (PORT, TOKEN_FILE))


def cmd_stop(a):
    if not ping():
        print("(daemon 未运行)")
        return
    call("/v1/stop", {})
    time.sleep(0.4)
    print("✓ daemon 已停止 (连接全部关闭)")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="sshx", description="常驻 SSH 桥：一次连接，多次操作")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("up", help="建立常驻连接")
    p.add_argument("target", help="[user@]host[:port]")
    p.add_argument("-u", "--user")
    p.add_argument("-p", "--password")
    p.add_argument("--key", help="私钥路径")
    p.add_argument("--port", type=int)
    p.add_argument("--as", dest="alias", help="别名")
    p.add_argument("--save", action="store_true", help="凭据落盘(600)，支持重启自动重连")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("run", help="在已有连接上执行命令")
    p.add_argument("args", nargs="+", help="[target] CMD...")
    p.add_argument("-t", "--timeout", type=float, default=120)
    p.add_argument("-d", "--detach", action="store_true", help="后台启动，立刻返回")
    p.add_argument("-q", "--quiet", action="store_true", help="不打印尾部状态行")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("put", help="上传文件")
    p.add_argument("args", nargs="+")
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("get", help="下载文件")
    p.add_argument("args", nargs="+")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("ls", help="列出连接")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("down", help="断开")
    p.add_argument("target", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("ping", help="探测连接是否仍可用")
    p.add_argument("target", nargs="?")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("start", help="后台启动 daemon")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="停止 daemon")
    p.set_defaults(func=cmd_stop)
    return ap


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = build_parser()
    a = ap.parse_args()
    if not getattr(a, "func", None):
        ap.print_help()
        sys.exit(2)
    a.func(a)


if __name__ == "__main__":
    main()
