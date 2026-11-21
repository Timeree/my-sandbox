# my-sandbox

把单个 HTTP 入口变成 TCP 隧道。WebSocket 承载原始字节流，可转发 SSH、FTP 或任意 TCP 端口。

## 一行启动

服务端的 `/client.py` 直接提供客户端源码，下载即运行：

```bash
curl -sL "https://host/client.py?sharecode=XXX" -o ms.py && python3 ms.py --url "https://host/?sharecode=XXX" --ssh 2222 --ftp 2121
```

状态页会在浏览器当前地址下生成这条命令，直接复制。

## 服务端

```bash
python3 server/server.py
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `3000` | HTTP 监听端口 |
| `FTP_ROOT` | `server/ftproot` | FTP 根目录，启动时自动创建 |
| `FTP_USER` | `user` | FTP 用户名 |
| `FTP_PASS` | `pass` | FTP 密码 |

前置条件：`/etc/ssh/sshd_config` 存在；FTP 由服务端按需拉起，无需预装。

路径 `/` 返回状态页，5 秒刷新，显示 22/21 端口在线状态与连接数。路径 `/client.py` 返回客户端源码。

## 客户端

单文件，仅标准库。

```bash
python3 client/client.py --url https://host/?sharecode=XXX --ssh 2222 --ftp 2121
```

| 参数 | 说明 |
|---|---|
| `--url` | 隧道地址，query 参数原样保留 |
| `--ssh PORT` | 本地 `PORT` → 远端 22 |
| `--ftp PORT` | 本地 `PORT` → 远端 21，被动模式 |
| `--forward REMOTE:LOCAL` | 本地 `LOCAL` → 远端 `REMOTE`，可重复 |

三个转发参数都不给时直接退出。

```bash
# SSH
ssh user@127.0.0.1 -p 2222

# FTP
ftp -p 127.0.0.1 2121

# 把远端 3000 映射到本地 8080
python3 client/client.py --url https://host/?sharecode=XXX --forward 3000:8080
```

## 技术细节

- 一条 WebSocket 承载一条 TCP 连接，二进制帧即原始字节，无额外封装。
- 客户端每个本地连接向 `url?p=<port>` 发起 WebSocket；服务端按 `p` 连接 `127.0.0.1:<port>`。
- 服务端对 22/21 做健康探测，缺失时拉起 sshd / ftpd.py，轮询等待就绪后建连。
- FTP 被动模式：拦截控制通道的 `227`/`229` 响应，把数据端口替换为本地临时监听端口；每个数据连接单独开一条 WebSocket，30 秒无连接自动释放。
- 控制通道按 CRLF 分行，非 `227`/`229` 原样透传，支持跨帧拼接。
- `rbufsize = 0` 关闭 HTTP 请求读缓冲，避免握手阶段预读吞掉隧道字节。
- 任一方向 EOF 时对 socket 执行 `shutdown`，唤醒对端阻塞读，避免半关闭死锁。
- 心跳：收到 ping 帧回 pong。

## 限制

- FTP 仅支持被动模式（PASV/EPSV），不支持 PORT/EPRT 与 FTPS。
- 依赖 WebSocket 升级，反向代理需放行 `Upgrade`。
- 服务端进程需常驻；宿主休眠或重启后需重新拉起。
