# 本地 HTTPS 运行（demo + API）

默认的 Compose 栈里 `demo-next` 用 `node server.js` 直接以 HTTP 监听 3000 端口，
浏览器再通过 `http://localhost:8000` 调 API。要让整个栈跑在 HTTPS 下，本目录用
**Caddy 反向代理做 TLS 终结**，把 demo 页面和 API 收敛到同一个 HTTPS 源
(`https://localhost:8443`)：

- `/health`、`/ready`、`/v1/*` → `wallet-agent:8000`（FastAPI）
- 其余所有路径 → `demo-next:3000`（Next.js）

同一个源的好处：页面和 API 都是 HTTPS（不会触发浏览器"混合内容"拦截），也
不需要动 CORS 配置（同源请求不经过 CORS 检查）。

构建时会把 `NEXT_PUBLIC_AGENT_API_URL` 覆盖成 HTTPS 源地址
（`https://localhost:8443`），demo 的浏览器代码就走这个源调 API。

## 启动

```bash
# 1) 构建并启动整个栈（含 Caddy）
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build

# 2) 导出 Caddy 的本地根证书（`tls internal` 生成的 CA）
mkdir -p deploy/https
docker compose -f docker-compose.yml -f docker-compose.https.yml \
  exec caddy cat /data/caddy/pki/authorities/local/root.crt > deploy/https/caddy-root.crt
```

## 信任本地根证书（消除浏览器警告）

浏览器/系统信任 `deploy/https/caddy-root.crt` 之后，访问
`https://localhost:8443` 就是绿色小锁。

- **macOS**：双击 `caddy-root.crt` → 钥匙串访问 → 把"此证书"改为"始终信任"；
  或命令行：
  `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain deploy/https/caddy-root.crt`
- **Linux**：`sudo cp deploy/https/caddy-root.crt /usr/local/share/ca-certificates/caddy.crt && sudo update-ca-certificates`
- **Windows**：双击证书 → 安装证书 → 本地计算机 → 受信任的根证书颁发机构

安装后**重启浏览器**。Chrome/Firefox 一般会读取系统证书库；如果只装到系统、
Chrome 仍报错，可在 `chrome://settings/security` 的"管理证书"里手动加入该证书。

> 不信任证书也能访问，只是每次都会弹警告——`tls internal` 本来就是给本地开发
> 用的（与 mkcert 同一思路）。

> 用 `https://localhost:8443` 访问（证书只签给了 `localhost`）；用
> `https://127.0.0.1:8443` 会因主机名不匹配再弹一次警告。

## 自定义端口/域名

默认用 `8443`（无需 root 权限）。要换端口：

```bash
HTTPS_PORT=9443 docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build
```

`HTTPS_PORT` 会同时作用于：Caddy 的监听与宿主映射、Caddyfile 里的站点地址、
以及 demo 构建参数里的 API 基址——三处保持一致。

要换域名（例如局域网内手机联调），把 `deploy/https/Caddyfile` 里的
`https://localhost:8443` 改成你的地址（如 `https://192.168.1.10:8443`），并在
`docker-compose.https.yml` 里把 Caddyfile 中的监听端口与 `ports` 映射保持一致；
证书域名必须和访问地址一致。

## 说明与注意事项

- `NEXT_PUBLIC_AGENT_API_URL` 是**构建期**注入的，覆盖文件把它指向 HTTPS 源；
  改完必须 `--build` 重建 demo 镜像才会生效（改端口时同理）。
- Caddy 容器网络里通过服务名 `wallet-agent` / `demo-next` 访问后端，
  `depends_on` 等后端健康后才会起来。
- SSE 流（`/v1/agent/stream/*`）在 Caddy 里设置了 `flush_interval -1`，不会被缓冲。
- 覆盖文件**保留了**原栈的 `8000` / `3000` 宿主 HTTP 端口，方便调试和
  `scripts/redeploy.sh` 的健康检查；如果只要 HTTPS，可以把基础 compose 里
  `wallet-agent`、`demo-next` 的 `ports` 删掉（或加 `profiles` 收起来）。
- 生产环境建议换成真实的域名 + 公开 CA（去掉 `tls internal`，Caddy 会自动申请
  Let's Encrypt 证书），本方案针对本地开发。
