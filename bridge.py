#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sshx bridge daemon —— 常驻 SSH 连接池，一次连接、多次操作。

本机 127.0.0.1:7799 上的 JSON/HTTP 服务，内部持有 paramiko SSH 会话池。
对外命令行走 sshx.py（会按需自动拉起本进程）。

    sshx.py serve     前台运行（调试用）
    sshx.py start     后台常驻（CLI 也会自动拉起）
    sshx.py stop      停止

接口（全部要求 X-Sshx-Token 头，除 /v1/ping）：
    GET  /v1/ping                    存活探测
    GET  /v1/ls                      连接池快照
    POST /v1/up                      建立/覆盖连接
    POST /v1/run                     {target?, cmd, timeout?, detach?, log?}
    POST /v1/put | /v1/get           SFTP 传输
    POST /v1/down                    断开
    POST /v1/stop                    停守护进程
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import shlex
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paramiko

HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".sshbridge")
CREDS_FILE = os.path.join(CONF_DIR, "creds.json")
TOKEN_FILE = os.path.join(CONF_DIR, "token")
LAST_FILE = os.path.join(CONF_DIR, "last")
LOG_FILE = os.path.join(CONF_DIR, "bridge.log")
DEFAULT_PORT = int(os.environ.get("SSHX_PORT", "7799"))
MAX_OUT = 8 * 1024 * 1024
DEFAULT_KEY = os.path.join(HOME, ".ssh", "id_ed25519")

_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _LOG_LOCK:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def ensure_conf_dir() -> None:
    os.makedirs(CONF_DIR, exist_ok=True)


def read_token() -> str:
    ensure_conf_dir()
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def dump_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_last() -> str | None:
    try:
        with open(LAST_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_last(alias: str) -> None:
    ensure_conf_dir()
    with open(LAST_FILE, "w", encoding="utf-8") as f:
        f.write(alias)


class BridgeError(Exception):
    pass


def decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def split_target(target: str):
    """'root@1.2.3.4:2222' -> (user|None, host, port|None)"""
    user = None
    rest = target.strip()
    if "@" in rest:
        user, rest = rest.rsplit("@", 1)
    port = None
    if rest.count(":") == 1:
        host, ps = rest.rsplit(":", 1)
        if ps.isdigit():
            rest, port = host, int(ps)
    return (user or None), rest, port


class Session:
    """一条保持在线、可自动重连的 SSH 连接。"""

    def __init__(self, alias, host, user, port=22, password=None,
                 key_file=None, passphrase=None, save=False):
        self.alias = alias
        self.host = host
        self.user = user
        self.port = port or 22
        self.password = password
        self.key_file = key_file
        self.passphrase = passphrase
        self.created = time.time()
        self.last_used = time.time()
        self.uses = 0
        self.reconnects = 0
        self.client = None
        self._lock = threading.RLock()
        self.connect()
        if save:
            self.persist()

    # ---------- 连接管理 ----------
    def connect(self) -> float:
        t0 = time.time()
        self.connect_ms = 0.0
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(
            hostname=self.host, port=self.port, username=self.user,
            timeout=10, banner_timeout=15, auth_timeout=15,
            allow_agent=False, look_for_keys=False,
        )
        if self.key_file:
            kw["key_filename"] = self.key_file
            if self.passphrase:
                kw["passphrase"] = self.passphrase
        elif self.password:
            kw["password"] = self.password
        else:
            # 无密码: 退回 key / agent 认证
            kw["look_for_keys"] = True
            kw["allow_agent"] = True
            if os.path.exists(DEFAULT_KEY):
                kw["key_filename"] = DEFAULT_KEY
        c.connect(**kw)
        tr = c.get_transport()
        if tr is not None:
            tr.set_keepalive(30)
        self.client = c
        self.connect_ms = (time.time() - t0) * 1000
        return time.time() - t0

    def alive(self) -> bool:
        try:
            tr = self.client.get_transport() if self.client else None
            return bool(tr and tr.is_active())
        except Exception:
            return False

    def ensure(self) -> None:
        with self._lock:
            if self.alive():
                return
            log("reconnect %s (%s@%s:%s)" % (self.alias, self.user, self.host, self.port))
            try:
                self.client.close()
            except Exception:
                pass
            self.connect()
            self.reconnects += 1

    def close(self) -> None:
        with self._lock:
            try:
                if self.client:
                    self.client.close()
            except Exception:
                pass
            self.client = None

    def persist(self) -> None:
        ensure_conf_dir()
        creds = load_json(CREDS_FILE, {})
        creds[self.alias] = {
            "host": self.host, "user": self.user, "port": self.port,
            "password": self.password, "key_file": self.key_file,
            "passphrase": self.passphrase,
        }
        dump_json(CREDS_FILE, creds)

    def info(self) -> dict:
        return {
            "alias": self.alias, "host": self.host, "user": self.user,
            "port": self.port, "up": self.alive(),
            "age": round(time.time() - self.created, 1),
            "idle": round(time.time() - self.last_used, 1),
            "uses": self.uses, "reconnects": self.reconnects,
            "connect_ms": round(getattr(self, "connect_ms", 0.0), 1),
            "auth": "key" if (self.key_file or not self.password) else "password",
        }

    # ---------- 执行 ----------
    def run(self, cmd: str, timeout: float = 120, detach: bool = False) -> dict:
        if detach:
            return self.run_detach(cmd)
        t0 = time.time()
        self.ensure()
        tr = self.client.get_transport()
        chan = tr.open_session(timeout=15)
        try:
            chan.settimeout(5.0)
            chan.exec_command(cmd)
            out, err = bytearray(), bytearray()
            truncated = False
            deadline = t0 + timeout
            timed_out = False
            while True:
                moved = False
                try:
                    while chan.recv_ready():
                        out += chan.recv(65536)
                        moved = True
                    while chan.recv_stderr_ready():
                        err += chan.recv_stderr(65536)
                        moved = True
                except socket.timeout:
                    pass
                except Exception:
                    break
                if len(out) + len(err) > MAX_OUT:
                    truncated = True
                    break
                if not moved:
                    if chan.exit_status_ready():
                        try:
                            while chan.recv_ready():
                                out += chan.recv(65536)
                            while chan.recv_stderr_ready():
                                err += chan.recv_stderr(65536)
                        except Exception:
                            pass
                        break
                    if time.time() > deadline:
                        timed_out = True
                        break
                    time.sleep(0.02)
            if timed_out:
                rc = 124
            else:
                try:
                    rc = chan.recv_exit_status()
                except Exception:
                    rc = -1
            self.last_used = time.time()
            self.uses += 1
            return {
                "ok": rc == 0, "exit": rc, "stdout": decode_bytes(bytes(out)),
                "stderr": decode_bytes(bytes(err)),
                "elapsed": round(time.time() - t0, 3),
                "timed_out": timed_out, "truncated": truncated,
                "alias": self.alias, "target": "%s@%s:%s" % (self.user, self.host, self.port),
            }
        finally:
            try:
                chan.close()
            except Exception:
                pass

    def run_detach(self, cmd: str, logpath: str | None = None) -> dict:
        """真正后台启动: setsid+nohup+全部 fd 重定向, 不在 channel 上留管道。"""
        self.ensure()
        logpath = logpath or "/tmp/sshx-%s-%s.log" % (
            time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])
        inner = "setsid nohup bash -c %s >> %s 2>&1 < /dev/null &" % (
            shlex.quote(cmd), shlex.quote(logpath))
        wrapped = "( %s ) ; sleep 0.4 ; echo __SSHX_STARTED__ %s" % (inner, logpath)
        r = self.run(wrapped, timeout=30)
        r["log"] = logpath
        so = r.get("stdout") or ""
        r["detached"] = "__SSHX_STARTED__" in so
        r["stdout"] = "\n".join(
            ln for ln in so.splitlines() if not ln.startswith("__SSHX_STARTED__")
        ).strip()
        r["ok"] = r["detached"]
        return r

    # ---------- 文件 ----------
    def put(self, local: str, remote: str) -> dict:
        self.ensure()
        t0 = time.time()
        sftp = self.client.open_sftp()
        try:
            sftp.put(local, remote)
            st = sftp.stat(remote)
            return {"ok": True, "remote": remote, "size": st.st_size,
                    "elapsed": round(time.time() - t0, 3), "alias": self.alias}
        finally:
            sftp.close()

    def get(self, remote: str, local: str) -> dict:
        self.ensure()
        t0 = time.time()
        sftp = self.client.open_sftp()
        try:
            sftp.get(remote, local)
            return {"ok": True, "local": local, "size": os.path.getsize(local),
                    "elapsed": round(time.time() - t0, 3), "alias": self.alias}
        finally:
            sftp.close()


class Pool:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()

    # ---- 解析 ----
    def resolve(self, target: str | None) -> Session:
        with self.lock:
            if not target:
                target = read_last()
                if not target:
                    raise BridgeError("还没有默认连接；先执行: sshx up user@host -p PASS")
            if target in self.sessions:
                s = self.sessions[target]
                if not s.alive():
                    s.ensure()
                return s
            creds = load_json(CREDS_FILE, {})
            if target in creds:
                c = creds[target]
                s = Session(target, c["host"], c["user"], c.get("port", 22),
                            c.get("password"), c.get("key_file"), c.get("passphrase"))
                self.sessions[target] = s
                write_last(target)
                return s
            # 形如 user@host:port 的临时目标, 只能靠 key 认证
            user, host, port = split_target(target)
            if host:
                for alias, s in self.sessions.items():
                    if s.host == host and (not user or s.user == user):
                        return s
                if user:
                    s = Session(target, host, user, port or 22)
                    self.sessions[target] = s
                    write_last(target)
                    return s
            raise BridgeError("目标 %r 未连接；先 sshx up 它" % target)

    def up(self, target, user=None, password=None, key=None, port=None,
           alias=None, save=False) -> Session:
        creds = load_json(CREDS_FILE, {})
        base = creds.get(alias or target) or {}     # 「sshx up 已存别名」也要能重连
        u0, host, p0 = split_target(target)
        if base.get("host"):
            host = base["host"]
        user = user or base.get("user") or u0
        port = port or base.get("port") or p0 or 22
        password = password if password is not None else base.get("password")
        key = key or base.get("key_file")
        if password is None and not key:
            # 同一 host+user 的另一个别名存过密码，直接复用（免重输）
            for other in creds.values():
                if (other.get("host") == host and other.get("user") == user
                        and other.get("password")):
                    password = other["password"]
                    break
        if not host:
            raise BridgeError("缺少主机；用 sshx up [user@]host[:port]")
        if not user:
            raise BridgeError("缺少用户名；用 -u 指定或 sshx up user@host")
        if alias is None:
            alias = target if base else ("%s@%s" % (user, host))
        if port != 22 and not alias.endswith(":%d" % port):
            alias = "%s:%s" % (alias, port)
        with self.lock:
            old = self.sessions.pop(alias, None)
            if old:
                old.close()
            s = Session(alias, host, user, port, password=password, key_file=key, save=save)
            self.sessions[alias] = s
            write_last(alias)
            return s


POOL = Pool()


class Handler(BaseHTTPRequestHandler):
    server_version = "sshx/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---- 工具 ----
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _authed(self) -> bool:
        if self.path.split("?")[0] == "/v1/ping":
            return True
        got = self.headers.get("X-Sshx-Token") or ""
        return hmac.compare_digest(got, TOKEN)

    def _go(self, fn) -> None:
        if not self._authed():
            self._send(401, {"ok": False, "error": "bad token"})
            return
        try:
            self._send(200, fn())
        except BridgeError as e:
            self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    # ---- 路由 ----
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/v1/ping":
            self._send(200, {"ok": True, "service": "sshx", "pid": os.getpid()})
            return
        if p == "/v1/ls":
            self._go(lambda: {"ok": True, "sessions": [s.info() for s in POOL.sessions.values()],
                              "last": read_last()})
            return
        self._send(404, {"ok": False, "error": "no route " + p})

    def do_POST(self):
        p = self.path.split("?")[0]
        b = self._body()

        if p == "/v1/up":
            def f():
                s = POOL.up(b["target"], b.get("user"), b.get("password"),
                            b.get("key"), b.get("port"), b.get("alias"),
                            b.get("save", False))
                return {"ok": True, **s.info()}
            self._go(f)
        elif p == "/v1/run":
            def f():
                s = POOL.resolve(b.get("target"))
                return s.run(b["cmd"], float(b.get("timeout") or 120),
                             bool(b.get("detach")), )
            self._go(f)
        elif p == "/v1/put":
            def f():
                return POOL.resolve(b.get("target")).put(b["local"], b["remote"])
            self._go(f)
        elif p == "/v1/get":
            def f():
                return POOL.resolve(b.get("target")).get(b["remote"], b["local"])
            self._go(f)
        elif p == "/v1/down":
            def f():
                with POOL.lock:
                    if b.get("all"):
                        n = len(POOL.sessions)
                        for s in POOL.sessions.values():
                            s.close()
                        POOL.sessions.clear()
                        return {"ok": True, "closed": n}
                    t = b.get("target") or read_last()
                    s = POOL.sessions.pop(t, None)
                    if not s:
                        return {"ok": True, "closed": 0}
                    s.close()
                    return {"ok": True, "closed": 1, "alias": t}
            self._go(f)
        elif p == "/v1/stop":
            def f():
                log("stop requested")
                threading.Timer(0.2, lambda: os._exit(0)).start()
                return {"ok": True, "stopping": True}
            self._go(f)
        else:
            self._send(404, {"ok": False, "error": "no route " + p})


TOKEN = ""


def serve(port: int) -> None:
    global TOKEN
    ensure_conf_dir()
    TOKEN = read_token()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    log("sshx bridge listening on 127.0.0.1:%d pid=%d" % (port, os.getpid()))
    print("sshx bridge listening on 127.0.0.1:%d" % port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in POOL.sessions.values():
            s.close()
        log("bridge exit")


def main() -> None:
    ap = argparse.ArgumentParser(prog="bridge.py")
    ap.add_argument("action", nargs="?", default="serve", choices=["serve"])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    serve(a.port)


if __name__ == "__main__":
    main()
