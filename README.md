# mybox

把一个 HTTP 入口变成 TCP 隧道。一条 WebSocket 承载所有连接，`mybox ssh` 直达远程 shell，也能顺手转发任意端口。

纯标准库，无第三方依赖。

## 安装

```bash
pip install .
```

## 服务端

```bash
mybox serve --port 3000
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--port` | `$PORT` 或 `3000` | HTTP/WebSocket 监听端口 |
| `--ssh-port` | `$SSH_PORT` 或 `22` | SSH 端口，按需拉起 sshd |

只接受 `/`、`/my-sandbox`、`/my-sandbox/` 三个路径的 WebSocket 升级，其余 404。根路径兼容旧链接。

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
```

| 参数 | 说明 |
|---|---|
| `-f/--forward REMOTE:LOCAL` | 远端端口 → 本地端口，逗号分隔或重复给出 |
| `--remote-port` | 远端 sshd 端口，默认 22 |
| `--local-port` | 本地 ssh 端口，默认随机空闲端口 |
| `--user` | 登录用户，默认 `$MYBOX_USER` 或 `root` |
| `--password` | 登录密码，默认 `$MYBOX_PASS` 或 `myboxpass` |

`-f` 是纯 TCP 转发，不带协议感知。需要 FTP 之类的多通道协议时，自己在两端封装。

### `mybox ls`

```
box1  https://host/my-sandbox?sharecode=XXX
prod  https://other/my-sandbox?sharecode=YYY
```

## 技术细节

- 一条 WebSocket 复用所有 TCP 流：帧头 `sid(4) + kind(1)`，kind 为 `DATA / OPEN / FIN / RESET`。
- 每个流独立发送队列和写线程，共享读循环只做非阻塞入队；某条流的对端不读只堵它自己，不会拖垮整条隧道。队列有上限，积压溢出时丢弃该流而非无限占内存。
- 半关闭而非硬断：一侧 `shutdown(SHUT_WR)` 转成 FIN 帧，两个方向都 FIN 且写线程排空后才关闭 socket，避免发送缓冲区里的数据被丢弃。
- 服务端对 `--ssh-port` 做健康探测，缺失时拉起 sshd，轮询等待就绪后建连。
- 两端 `TCP_NODELAY` 加内核 `SO_KEEPALIVE`，关掉 Nagle 与延迟 ACK 叠加出的 40ms 固定停顿。
- 15 秒 WebSocket ping 双向保活，断线自动重连。

## 限制

- 依赖 WebSocket 升级，反向代理需放行 `Upgrade`。
- 服务端进程需常驻；宿主休眠或重启后需重新拉起。
- 只转发 TCP，不处理 UDP。
