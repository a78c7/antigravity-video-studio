# 自托管多人协作部署参考（可选）

> **说明**：反重力视频工坊默认以 **纯本地单机模式（`APP_MODE=local_producer`）** 运行，免登录、零服务器成本，个人创作者无需部署本目录中的任何文件。
> 仅当你需要与团队成员远程审片、共同修改剧本/分镜或投票挑选首尾帧与封面时，才需要在自有 Linux VPS 上部署 `cloud_collab` 协作端。

---

## 一、架构与安全边界

- **本地制片台（`APP_MODE=local_producer`）**：负责全部 AI 生成（Gemini / Flow / Colab）、本地音视频渲染与油箱额度管理，不暴露给公网。
- **自托管协作端（`APP_MODE=cloud_collab`）**：部署在你的 Linux 服务器上，**彻底禁用全部本地媒体生成与 Google 凭据接口**，开启强制账号登录，仅用于团队审片、剧本协同修改、投票、规则建议与评论草稿管理。
- **双向同步**：本地制片台通过 HTTPS + `X-Sync-Token` 与自托管协作端按需拉取团队修改、推送首尾帧/试卡/网页轻量预览片（`web_preview.mp4`）/终版母带与封面。

---

## 二、部署步骤（通用 Ubuntu / Debian + systemd + Nginx）

### 1. 准备代码与前端静态产物

仓库已内置预构建的 `backend/static/`；如果你改动了前端源码，建议先在本地电脑构建后再上传（避免在低配 VPS 上编译前端）：

```bash
cd frontend && npm install && npm run build
```

将项目同步至服务器 `/opt/agvs`（注意排除 `.env`、`.secrets.env`、`data/`、`.venv/`、`frontend/node_modules/`）。

### 2. 创建专用运行用户与 Python 3.12+ 虚拟环境

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin agvs || true
sudo mkdir -p /opt/agvs /etc/agvs
sudo chown -R agvs:agvs /opt/agvs

# 在 /opt/agvs 下创建虚拟环境并安装后端依赖（推荐使用 uv 或 python3.12 -m venv）
sudo -u agvs python3.12 -m venv /opt/agvs/.venv
sudo -u agvs /opt/agvs/.venv/bin/pip install --upgrade pip
sudo -u agvs /opt/agvs/.venv/bin/pip install fastapi "uvicorn[standard]" "httpx[socks]" openai pydantic python-multipart python-dotenv pillow
```

### 3. 配置受控环境变量

```bash
sudo cp /opt/agvs/deploy/self-hosted/collab.env.example /etc/agvs/collab.env
sudo chown root:agvs /etc/agvs/collab.env
sudo chmod 640 /etc/agvs/collab.env
```

编辑 `/etc/agvs/collab.env`，生成强随机密钥并修改初始账号密码：

```bash
# 生成 64 位随机十六进制密钥（分别填入 SESSION_SECRET 与 SYNC_API_TOKEN）
openssl rand -hex 32
```

### 4. 配置 systemd 服务

```bash
sudo cp /opt/agvs/deploy/self-hosted/agvs-collab.service.example /etc/systemd/system/agvs-collab.service
sudo systemctl daemon-reload
sudo systemctl enable --now agvs-collab.service
sudo systemctl status agvs-collab.service
```

默认监听 `127.0.0.1:8766`，不直接对公网暴露后端端口。

### 5. 配置 Nginx HTTPS 反向代理

参考 `nginx.conf.example` 配置站点与 TLS 证书（如 Certbot / Let's Encrypt）：

```bash
sudo cp /opt/agvs/deploy/self-hosted/nginx.conf.example /etc/nginx/sites-available/agvs-collab.conf
# 将文件中的 studio.example.com 与证书路径替换为你的实际域名和证书路径
sudo ln -sf /etc/nginx/sites-available/agvs-collab.conf /etc/nginx/sites-enabled/agvs-collab.conf
sudo nginx -t && sudo systemctl reload nginx
```

### 6. 在本地制片台绑定协作端

打开本地制片台 `http://127.0.0.1:8765` → **系统设置**：
1. 打开 **「启用自托管多人协作」** 开关；
2. 填入你的协作端地址（如 `https://studio.example.com`）与同一个 `SYNC_API_TOKEN`；
3. 保存后点击检测连通性，即可在审片节点一键推拉同步。

---

## 三、可选：接入开源推送服务（如 `ntfy`）

若希望在首尾帧、360p 试卡或成片同步上云时向团队成员手机发送无广告实时通知，可在同一台服务器或任意受控主机上按需部署开源 [ntfy](https://ntfy.sh/)，并在 `/etc/agvs/collab.env` 中将 `NTFY_ENABLED=true` 并填入你自己的服务地址、Topic 与鉴权 Token（不需要推送保持 `NTFY_ENABLED=false` 即可）。
