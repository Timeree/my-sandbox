# mybox

把一个 HTTP 入口变成 TCP 隧道。一条 WebSocket 承载所有连接，`mybox ssh` 直达远程 shell，`mybox ftp` 起一个全权限 FTP 服务，`-f` 转发任意端口。

纯标准库，无第三方依赖。

## 安装

```bash
pip install https://codeload.github.com/Timeree/my-sandbox/tar.gz/refs/heads/main
```

走的是 tarball 直链，`git clone` 经常被网络掐断，这条路更稳。

升级到最新提交：

```bash
mybox update
```

## 服务端

```bash
mybox serve --port 3000
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | `$PORT` 或 `3000` | HTTP/WebSocket 监听端口 |
| `--ssh-port` | `$SSH_PORT` 或 `22` | SSH 端口，按需拉起 sshd |

只接受 `/my-sandbox` 一个路径的 WebSocket 升级，其余 404。

## 客户端

```bash
mybox add https://host/my-sandbox?sharecode=XXX     # name 自动生成 box1、box2...
mybox add prod https://host/my-sandbox?sharecode=XXX
mybox ls
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

| 参数 | 说明 |
|---|---|
| `-f/--forward REMOTE:LOCAL` | 远端端口 → 本地端口，逗号分隔或重复给出；`ftp:REMOTE:LOCAL` 强制按 FTP 处理 |
| `--remote-port` | 远端 sshd 端口，默认 22 |
| `--local-port` | 本地 ssh 端口，默认随机空闲端口 |
| `--user` | 登录用户，默认 `$MYBOX_USER` 或 `root` |
| `--password` | 登录密码，默认 `$MYBOX_PASS` 或 `myboxpass` |
| `-- ...` | 之后的内容原样传给 ssh（选项/命令/转发） |

`-f` 是纯 TCP 转发，不带协议感知。转发 21 或 2121 端口时会自动接管 FTP 被动模式（改写 `227`/`229` 里的地址端口，每个数据连接单独开一条流），所以 FileZilla 可以直接连本地端口。

### `mybox ftp [port]`

在被控主机上跑一个内置 FTP 服务，默认 2121，根目录 `/`，**无用户名密码、全权限**。

```bash
mybox ftp          # 0.0.0.0:2121
mybox ftp 3121
```

配合隧道使用：

```bash
# 被控主机
mybox ftp

# 本地
mybox ssh prod -f 2121:35121
# 然后用 FTP 客户端连 127.0.0.1:35121，用户名密码随便填
```

因为没有认证又能读写整个文件系统，**不要把它直接暴露在公网**，只通过 SSH 隧道访问。只有远端 21 和 2121 会自动启用 FTP 智能转发，自定义端口要显式标注：

```bash
mybox ssh prod -f ftp:3121:13121
```

### `mybox ls`

```
box1  https://host/my-sandbox?sharecode=XXX
prod  https://other/my-sandbox?sharecode=YYY
```

### `mybox update`

从仓库拉最新提交并覆盖安装：

```bash
mybox update
# 等价于 python -m pip install --upgrade git+https://github.com/Timeree/my-sandbox.git@main
```

用的是当前解释器的 pip，装进 `mybox` 自身所在的环境。

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
