# my-sandbox

把单个 HTTP 入口变成 TCP 隧道。WebSocket 承载原始字节流，可转发 SSH、FTP 或任意 TCP 端口。

## 一行启动

```bash
python3 client/client.py --url "https://host/?sharecode=XXX" --ssh 2222 --ftp 2121
```

## 服务端

```bash
python3 server/server.py
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `3000` | HTTP 监听端口 |
| `FTP_ROOT` | `/` | FTP 根目录，默认系统根路径 |
| `FTP_USER` | `root` | FTP 用户名 |
| `FTP_PASS` | `myboxpass` | FTP 密码 |

SSH 与 FTP 均由服务端按需拉起，无需预装。SSH 自带配置（root 登录、`ClientAliveInterval 10` 保活心跳），不依赖 `/etc/ssh/sshd_config`。

根路径无状态页，返回 404。

## 客户端

`client/client.py` + `client/ws.py` 两个文件，仅标准库，两个文件放同一目录。

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
ssh root@127.0.0.1 -p 2222

# FTP
ftp -p 127.0.0.1 2121

# 把远端 3000 映射到本地 8080
python3 client/client.py --url https://host/?sharecode=XXX --forward 3000:8080
```

## 技术细节

- 一条 WebSocket 复用所有 TCP 流：帧头 `sid(4) + kind(1)`，kind 为 `DATA / OPEN / FIN / RESET`。
- 客户端首次转发才建 WebSocket，之后所有连接（含 FTP 数据连接）共用同一条，断线自动重连。
- 半关闭而非硬断：一侧 `shutdown(SHUT_WR)` 转成 FIN 帧，两个方向都 FIN 才关闭 socket，避免发送缓冲区里的数据被丢弃。
- 服务端 `OPEN` 异步建连，建连期间数据入队，不阻塞读循环。
- 服务端对 22/21 做健康探测，缺失时拉起 sshd / ftpd.py，轮询等待就绪后建连。
- 两端 `TCP_NODELAY`，关掉 Nagle 与延迟 ACK 叠加出的 40ms 固定停顿。
- FTP 被动模式：客户端拦截 `227`/`229`，本地起临时监听并把端口回给 FTP 客户端，数据连接再映射到远端 PASV 端口。
- FTP 预取：转发 `LIST/NLST/RETR/STOR` 时在同一帧里补一条 `PASV`，服务端处理完传输就绪下一个数据端口，下一轮 `PASV` 直接命中缓存，省掉一个 RTT。
- 控制通道本地应答 `TYPE / OPTS / CLNT / ALLO / SITE / REST / NOOP`，这些命令不再往返。
- 空闲 20 秒发 WebSocket ping 保活。

## 限制

- FTP 仅支持被动模式（PASV/EPSV），不支持 PORT/EPRT 与 FTPS。
- 依赖 WebSocket 升级，反向代理需放行 `Upgrade`。
- 服务端进程需常驻；宿主休眠或重启后需重新拉起。
