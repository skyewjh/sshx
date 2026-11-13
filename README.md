# sshx — 常驻 SSH 桥

一次连接，多次操作。SSH 会话交给常驻 daemon 持有，之后每条远端命令都是一行
CLI，不必为每次探查重写 paramiko 脚本。

```bash
sshx up root@10.0.0.21 -p YOUR_PASSWORD --as box   # 只连一次
sshx run box 'ps aux | grep nginx'                 # 23 ms，复用同一条连接
sshx run box 'systemctl status api' -t 30
sshx run 'uptime'                                  # 省略 target = 最近用的那个
```

## 为什么

一次性 `exec_command` 用完即弃：每条命令都要付约 250–300 ms 的 TCP + SSH
握手和认证，脚本化时还得反复写 `paramiko.SSHClient()` 样板。

`sshx` 把连接留在后台进程里：

- **复用**：同一目标的后续命令约 20–30 ms，只剩远端执行 + 本地 IPC
- **保活**：transport 每 30 s 发 keepalive，掉线在下一条命令前自动重连
- **一致**：多个 CLI 调用共享同一份凭据；`--save` 后 daemon 重启也能重建连接
- **零配置启动**：CLI 探不到 daemon 就以 detached 进程拉起，不需要注册系统服务

## 结构

```
bridge.py   daemon：连接池 + SFTP，监听 127.0.0.1:7799（仅回环）
sshx.py     CLI：HTTP/JSON 客户端，daemon 不在时自动拉起
sshx        sh 包装（git-bash / MSYS）
sshx.bat    cmd / PowerShell 包装
~/.sshbridge/   token、creds.json、last、bridge.log
```

## 安装

```bash
git clone https://github.com/skyewjh/sshx.git
cd sshx
pip install -r requirements.txt       # 唯一依赖：paramiko >= 3.0

# 把 CLI 放进 PATH（示例：git-bash）
ln -s "$PWD/sshx" ~/bin/sshx    # 或 cp，确保 ~/bin 在 PATH 里
```

Python ≥ 3.8（代码依赖 `from __future__ import annotations` 下的 PEP 585 注解）。

## 用法

```bash
# 建立连接
sshx up root@10.0.0.21 -p YOUR_PASSWORD --as box
sshx up deploy@example.com --key ~/.ssh/id_ed25519 --as prod --save
sshx up box                        # 已存凭据的别名 = 重连它

# 执行
sshx run box 'ps aux | grep nginx'
sshx run box 'cd /srv/app && ./deploy.sh' -t 600
sshx run box - < script.sh         # 命令从 stdin 读，免引号地狱
sshx run box './run.sh' -d         # 后台启动，立刻返回日志路径
sshx run 'uptime'                  # 省略 target

# 文件
sshx put box ./app.py /srv/app/app.py
sshx get box /var/log/app.log ./app.log

# 连接池
sshx ls / sshx ping box / sshx down box / sshx down --all / sshx stop
```

`sshx run` 的退出码就是远端命令的退出码（超时 124），stderr 原样回传，尾部
一行 `[sshx box exit=0 0.023s]` 打在 stderr 上，所以

```bash
sshx run box 'cat data.csv' > local.csv    # stdout 干净
```

## 行为要点

- **不保留 cwd**：每条命令由 sshd 独立 `exec`，起始目录是登录目录。要目录就
  写 `cd /path && cmd`，放在同一条命令里。
- **后台进程用 `-d`**：内部是 `setsid nohup ... < /dev/null >> log`，不占
  channel。自己拼 `nohup cmd &` 会挂住 channel 直到超时——paramiko 的老坑。
- **没有 pty**：交互式命令（vim、top、密码提示）一律挂住直到超时。提权建议
  在目标机上配置 NOPASSWD，或先单独 `sudo -v` 缓存票据再执行。
- **端口**：`SSHX_PORT=7801 sshx ls`。端口是 daemon 的身份，CLI 与 daemon 必须同值。
- **开机自启**（可选）：不装也行，首个 `sshx` 调用会自动拉起。
  `schtasks /Create /TN sshx-bridge /SC ONLOGON /TR "pythonw <path>\bridge.py serve"`

## 安全

- daemon 只监听 `127.0.0.1`，不是网络服务。
- 除 `/v1/ping` 外所有接口要求 `X-Sshx-Token`（`~/.sshbridge/token`，首次启动
  生成），挡掉同机其他进程与浏览器 CSRF。
- 主机密钥用 `paramiko.AutoAddPolicy`——适合实验室/内网设备；面向生产请改成
  known_hosts 校验。
- `--save` 会把密码**明文**写进 `~/.sshbridge/creds.json`。共享机器上别用。

## HTTP 接口

`GET /v1/ping`（无需 token）、`GET /v1/ls`、`POST /v1/up`、`POST /v1/run`
（`{target?, cmd, timeout?, detach?}`）、`POST /v1/put`、`POST /v1/get`、
`POST /v1/down`、`POST /v1/stop`。`target` 省略时用最近一次使用的连接。

## License

MIT
