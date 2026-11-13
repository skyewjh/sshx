# sshx — 为 AI Agent 设计的常驻 SSH 桥

一次连接，多次操作。

AI agent（Hermes / Claude Code / Cursor / 自建 agent）做远端运维时，最常见的
浪费是**每一步都重写一遍连接代码**：新建 `paramiko.SSHClient()`、认证、执行、
读 stdout/stderr、猜退出码，然后下一轮对话再从零来一遍。

`sshx` 把 SSH 会话留在常驻 daemon 里，把"连远端"降级成一条命令：

```bash
sshx up root@10.0.0.21 -p YOUR_PASSWORD --as box   # agent 只需要做一次
sshx run box 'ps aux | grep nginx'                 # 之后每条命令 ~23 ms
sshx run box 'systemctl status api' -t 30
sshx run 'uptime'                                  # 省略 target = 最近用的那个
```

## 为什么 agent 特别需要它

| agent 的痛点 | sshx 的做法 |
|---|---|
| 每次探查都要生成一个新 `.py` 脚本 | 一条 `sshx run`，无需落地文件 |
| 每次新建连接，250–300 ms 握手 + 认证 | daemon 复用连接，后续命令 ~20–30 ms |
| 密码/token 要在每轮重新提供 | `--save` 后凭据留在 daemon，跨轮、跨进程复用 |
| agent 得自己解析输出判断成败 | **退出码 = 远端退出码**（超时 124），不需要猜 |
| stdout 里混着提示和状态 | 状态行走 stderr，`stdout` 是干净数据，可直接重定向/解析 |
| 每次 tool call 是新进程，没有内存 | CLI 无状态，daemon 有状态——正好跨越进程边界 |
| 需要先 bootstrap 才能用 | CLI 探不到 daemon 就自己 detached 拉起，零安装步骤 |

关键点：**agent 的每个 tool call 都是独立进程，状态无处存放**。把连接放进
daemon，等于给 agent 一个跨轮次存活的句柄。这就是它存在的理由。

## 接进你的 agent

把下面几行放进 `AGENTS.md` / `CLAUDE.md` / system prompt / skill 即可——之后
agent 就不会再手写 paramiko 了：

```markdown
## 远程 SSH

远端操作一律走 `sshx`，不要写 paramiko 脚本。

- 首次连接：`sshx up user@host -p PASS --as NAME`（--save 可持久化凭据）
- 之后执行：`sshx run NAME '命令'`，省略 NAME 即用最近连接
- 长命令加 `-t 秒`；要后台跑用 `-d`（返回日志路径）
- 传文件：`sshx put/get [NAME] 本地 远端`
- 判断成败看退出码；stdout 是干净数据，状态行在 stderr
```

## 为什么

一次性 `exec_command` 用完即弃：每条命令都付完整握手成本，脚本化时还要反复
写样板。`sshx` 把连接留在后台进程里：

- **复用**：同一目标的后续命令约 20–30 ms，只剩远端执行 + 本地 IPC
- **保活**：transport 每 30 s 发 keepalive，掉线在下一条命令前自动重连
- **一致**：多个 CLI 调用共享同一份凭据；`--save` 后 daemon 重启也能重建连接
- **零配置启动**：CLI 探不到 daemon 就以 detached 进程拉起

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
sshx run box 'cat data.csv' > local.csv    # stdout 干净，可直接解析
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
