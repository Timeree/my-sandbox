# mybox

把一个 HTTP 入口变成 TCP 隧道。一条 WebSocket 承载所有连接，`mybox ssh` 直达远程 shell，`-f` 转发任意端口，`--ftp` 顺手把内置的全权限 FTP 服务拉到你本地。

纯标准库，无第三方依赖。

## 安装

```bash
pip install git+https://github.com/Timeree/my-sandbox.git
```

`git clone` 被网络掐断时，改用归档包（GitHub 侧有几分钟缓存，可能落后一个提交）：

```bash
pip install https://codeload.github.com/Timeree/my-sandbox/tar.gz/refs/heads/main
```

升级到最新提交：

```bash
mybox update
```

## 服务端

```bash
mybox serve --port 3000
mybox serve -p              # 运行时生成随机密码并打印
mybox serve -p ABCD1234     # 指定密码
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-p`, `--password` | 无 | 握手口令校验，见下 |
| `--port` | `$PORT` 或 `3000` | HTTP/WebSocket 监听端口 |
| `--ssh-port` | `$SSH_PORT` 或 `22` | SSH 端口，按需拉起 sshd |
| `--ftp-port` | `$FTP_PORT` 或 `2121` | 内置 FTP 端口，随 serve 一起常驻 |

只接受 `/my-sandbox` 一个路径的 WebSocket 升级，其余 404。

### 握手密码 `-p`

这是网关 sharecode 之外、由服务端自己把关的一层校验，只在服务端开启：

- 不带 `-p`：不校验，行为同旧版。
- `mybox serve -p`：启动时生成一个 8 位 base32 随机密码（如 `S4FBA7WS`），打印在启动信息里。
- `mybox serve -p ABCD1234`：用指定密码。

开启后，客户端的 GET 请求必须带 `p=<密码>`，否则服务端返回 401、握手不通过。把密码作为 URL 的 query 参数即可，它和 sharecode 一样会原样进入握手请求：

```bash
mybox add "https://host/my-sandbox?sharecode=XXX&p=ABCD1234"
```

`add` 的连通探测和 `ssh` 都会自动携带这个 `p`。密码等同凭据，别和 URL 一起外泄。

`mybox serve` 起来就会在 `--ftp-port` 上常驻一个 **无用户名密码、全权限、根目录 `/`** 的 FTP 服务。客户端连不连由自己决定，不连它也不占隧道。因为无认证又能读写整个文件系统，**不要把这个端口直接暴露在公网**，只通过 SSH 隧道访问。

## 客户端

```bash
mybox add https://host/my-sandbox?sharecode=XXX     # name 自动生成 box1、box2...
mybox add prod https://host/my-sandbox?sharecode=XXX
mybox ls
mybox rename prod production
mybox ssh prod
```

### `mybox add [name] [url]`

只给一个参数时视为 url，name 依次自动生成 `box1`、`box2`。保存前会真的建立一次 WebSocket 握手，连不上直接失败，不会存入坏记录。

省略路径或只给 `/` 时自动补成 `/my-sandbox`，保持链接可读。

配置存在 `~/.config/mybox/boxes.json`（权限 600，url 里的 sharecode 等同凭据）。可用 `MYBOX_CONFIG_DIR` 改目录。

### `mybox ssh [name]`

name 省略时取第一个 box。自动完成建隧道、分配本地端口、填充密码，不需要再手动 `ssh`。

```bash
mybox ssh                          # 第一个 box
mybox ssh prod                     # 指定 box
mybox ssh prod -f 8080:9080        # 同时把远端 8080 映射到本地 9080
mybox ssh prod -f 8080:9080,3306:9306
mybox ssh prod --local-port 2222   # 固定本地 ssh 端口，默认随机
mybox ssh prod --ftp 35121         # 把内置 FTP 映射到本地 35121
mybox ssh prod --kill-stale        # 清理占用固定本地端口的残留进程后再连
mybox ssh prod -- ls -l /var/log   # -- 之后的参数原样交给 ssh
```

`--` 后面的东西不会经过 mybox 解析，选项插在目标主机之前，命令跟在主机之后，跟原生 `ssh` 完全一致。因此可以用它做初始自动执行，或者干任何 mybox 没封装的事：

```bash
mybox ssh prod -- 'uptime; df -h'
mybox ssh prod -- -t 'cd /app && ls; exec bash'   # 进目录、看一眼、留在 shell 里
mybox ssh prod -- -L 9090:localhost:80 -N         # 只做端口转发
mybox ssh prod -- -f -L 9090:localhost:80         # ssh 自己的 -f，后台运行
```

注意 mybox 自己也占用了 `-f`（转发），要用 ssh 的 `-f` 必须放到 `--` 之后。多条命令要整体加引号传一个字符串，否则会被 ssh 按空格重新拼接。

ssh 会话结束时，所有由它拉起的本地监听端口会一起关闭——包括 `--ftp`、`-f` 的转发端口、FTP 被动模式临时开的数据端口，以及本地 ssh 端口。不需要手动清理。

如果上次连接被强制中断（设备内存不足进程被杀、强关终端），`finally` 来不及执行，固定本地端口可能被残留进程占住，重连时报 `Address already in use`。加 `--kill-stale` 会在监听前找出占用这些固定端口的进程并终止，然后重连：

```bash
mybox ssh prod --local-port 2222 --ftp 35121 --kill-stale
```

只处理显式固定的端口（`--local-port`、`--ftp`、`-f` 的本地端口），不会碰随机端口，也不会终止自己或父进程。定位占用进程优先查 `/proc`（LISTEN、ESTABLISHED、CLOSE_WAIT 等所有 TCP 状态），查不到再回退 `lsof`；`SIGTERM` 无效时升级 `SIGKILL`。连接时若端口仍被占，会就地再清一次并重试。

清理过程在 stderr 打诊断，例如 `port 35121: 1 holder(s)` / `killed stale pid 12345`。若清理后该端口仍被占用（占用者不可见，如跨网络命名空间或其他用户的 socket），直接失败并报 `cannot listen: [Errno 98] Address already in use on 127.0.0.1:35121`，不会擅自换端口。此时需自行释放该端口或换一个本地端口。

连接过程会在 stderr 打日志，每步一行，方便定位卡在哪：

```
[mybox] connecting to prod: wss://host/my-sandbox?sharecode=XXX
[mybox] tunnel ready
[mybox] listening: ssh 127.0.0.1:41235, ftp 35121
[mybox] sshd ok: SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20
```

启动 ssh 前会先读一次远端 SSH banner。如果隧道通了但远端 sshd 没起，会直接报 `sshd on the box did not answer (remote port 22): connection closed before SSH banner`，不会让你对着空窗口猜。日志走 stderr，不污染 shell 输出。

| 参数 | 说明 |
|---|---|
| `-f/--forward REMOTE:LOCAL` | 远端端口 → 本地端口，逗号分隔或重复给出；`ftp:REMOTE:LOCAL` 强制按 FTP 处理 |
| `--ftp LOCAL_PORT` | 把远端 2121 的 FTP 映射到本地该端口，带被动模式改写 |
| `--remote-port` | 远端 sshd 端口，默认 22 |
| `--local-port` | 本地 ssh 端口，默认随机空闲端口 |
| `--user` | 登录用户，默认 `$MYBOX_USER` 或 `root` |
| `--password` | 登录密码，默认 `$MYBOX_PASS` 或 `myboxpass` |
| `-- ...` | 之后的内容原样传给 ssh（选项/命令/转发） |

`-f` 是纯 TCP 转发，不带协议感知。转发 21 或 2121 端口时会自动接管 FTP 被动模式（改写 `227`/`229` 里的地址端口，每个数据连接单独开一条流），所以 FileZilla 可以直接连本地端口。

### FTP

服务端的 FTP 跟着 `mybox serve` 常驻，客户端一条参数接上：

```bash
mybox ssh prod --ftp 35121
# FTP 客户端连 127.0.0.1:35121，用户名密码随便填
```

`--ftp` 走的就是 `-f 2121:LOCAL`，额外带上被动模式改写（自动替换 `227`/`229` 里的地址端口，每条数据连接单独开流），FileZilla 直接可用。`--ftp` 可以和 `-f`、`--` 混用：

```bash
mybox ssh prod --ftp 35121 -f 8080:9080
mybox ssh prod --ftp 35121 -- -L 9090:localhost:80
```

服务端改了 `--ftp-port` 时，客户端用完整写法标注：

```bash
mybox ssh prod -f ftp:3121:13121
```

### `mybox ls`

```
box1  https://host/my-sandbox?sharecode=XXX
prod  https://other/my-sandbox?sharecode=YYY
```

### `mybox remove [name]`

从记录表里移除一条 box，只删本地记录，不影响远端服务。name 省略时取第一条：

```bash
mybox remove prod
mybox remove        # 删第一条
```

### `mybox rename [old] new`

给记录改名，只改本地 key，url 不变。省略 old 时取第一条：

```bash
mybox rename prod production
mybox rename production   # 第一条改名为 production
```

新名已存在会拒绝，避免覆盖记录。

### `mybox update`

从仓库拉最新提交并覆盖安装：

```bash
mybox update
# 等价于 python -m pip install --upgrade --force-reinstall --no-deps git+https://github.com/Timeree/my-sandbox.git@main
```

`--force-reinstall` 保证覆盖安装，不会因为版本号相同被 pip 跳过。用当前解释器的 pip，装进 `mybox` 自身所在的环境。

## 技术细节

- 一条 WebSocket 复用所有 TCP 流：帧头 `sid(4) + kind(1)`，kind 为 `DATA / OPEN / FIN / RESET`。
- 每个流独立发送队列和写线程，共享读循环只做非阻塞入队；某条流的对端不读只堵它自己，不会拖垮整条隧道。队列有上限，积压溢出时丢弃该流而非无限占内存。
- 半关闭而非硬断：一侧 `shutdown(SHUT_WR)` 转成 FIN 帧，两个方向都 FIN 且写线程排空后才关闭 socket，避免发送缓冲区里的数据被丢弃。
- 服务端对 `--ssh-port` 做健康探测，缺失时拉起 sshd，轮询等待就绪后建连。
- 两端 `TCP_NODELAY` 加内核 `SO_KEEPALIVE`，关掉 Nagle 与延迟 ACK 叠加出的 40ms 固定停顿。
- FTP 预取：转发 `LIST/NLST/RETR/STOR` 时在同一帧里补一条 `PASV`，下一轮 `PASV` 直接命中缓存，省掉一个 RTT。
- 15 秒 WebSocket ping 双向保活，断线自动重连。

## 限制

- 依赖 WebSocket 升级，反向代理需放行 `Upgrade`。
- 服务端进程需常驻；宿主休眠或重启后需重新拉起。
- 只转发 TCP，不处理 UDP。
- `-f` 只对 21、2121 这两个端口做 FTP 被动模式改写，其他协议的多通道（SIP 等）数据连接仍需自行固定端口转发。
