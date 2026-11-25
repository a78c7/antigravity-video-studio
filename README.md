# 反重力视频工坊（Antigravity Video Studio）

> **声明**：本项目为非官方社区开源工具，与 Google / Antigravity 官方无任何隶属关系。
>
> **专为 Google Gemini / Antigravity（反重力）订阅用户打造的本地 AI 短剧全流程制片台。**
>
> 把你每月订阅附带的 **Gemini API 赠金、Google Flow 网页积分、Google Colab GPU 算力** 彻底串联榨干 —— **0 封号、省积分、告别剪映手搓**，一条龙跑通「选题 → 资产防穿帮 → 剧本分镜 → 首尾帧定图 → 360p 试卡/1080p 定稿 → 三轨混音 → Colab 4K HLG HDR 安全区字幕母带 → 双比例封面与 AI 原创证明」。

---

## 为什么做这个工具？

如果你订阅了 Google AI Pro / Ultra 或拥有 Google Developer Program 月度赠金，大概率遇到过这些痛点：

- **三口福利油箱各自孤立**：API 赠金、Flow 月积分、Colab 算力互相割裂，要么月底清零浪费，要么不小心花 50 积分/镜在 Flow 网页买 4K 把额度一把烧光；
- **角色跨镜变脸穿帮**：手动复制 Prompt 动不动漏掉角色特征约束，上一镜还好好的主角，下一镜就长变形、换衣服；
- **素材与命名一团糟**：网页下载一堆随机 UUID 文件，几十条试卡和定稿混在下载目录里找不着北；
- **剪映手搓太耗命**：每次都要手拉三轨（原生环境音 + 旁白 + BGM）、手调人声压低配乐、手调竖屏字幕安全区防底栏遮挡。

**反重力视频工坊**用一个轻量单进程本地 Web 制片台（FastAPI + SQLite WAL）搭配 **Antigravity 专属导演 Skill（`skills/agvs-director`）** 解决整条链路：**AI 干脏活累活（搜选题、写分镜、并发抽卡、看图排雷、听音查幻觉、混音压字幕），所有审美与花钱节点弹出选择题交给你拍板。**

---

## 三口独立福利油箱分工（按订阅档位一键校准）

内置 **5 档 Google 订阅预设**（免费 AI Studio / Google AI Pro / Google AI Ultra / AI Ultra + API 月赠金 / 自定义），一键校准三口油箱红线，互不混用、不悄悄超支：

| 油箱 | 来源 | 主力分工 | 省钱与风控策略 |
| :--- | :--- | :--- | :--- |
| **油箱 1：Gemini API / 月度赠金** | AI Studio 免费层或 Developer Program 赠金 | 剧本/分镜策划 + 官方 `gemini-api` CLI 全链路出图、360p 试卡、1080p 定稿、原创配乐与中文 TTS | 先跑低价 360p 试卡 + 抽帧听音体检，确认无误再并发升 1080p；单集 CLI 全套估算约 `$8–$10`，余额不足自动预警切手动 Flow。 |
| **油箱 2：Google Flow 网页积分** | Google AI Pro / Ultra 月度积分 | 「手动 Flow 模式」下生成视频镜头 | 一键打好编号首尾帧与逐镜 Prompt 发车包；仅导出 **免费 1080p**，绝不花高额积分买 Flow 4K。 |
| **油箱 3：Google Colab GPU 算力** | Colab Pro / Pro+ CU 算力 | 终版 **4K/30fps + 10-bit HLG HDR + 底部 24%–26% 安全区字幕** 超分母带 | 默认选 **L4 GPU**（抗排队、省 CU），单集约 `0.4 CU`，出片完成自动停机；未配置 Colab 时自动走本机 1080p 渲染。 |

> 模型 ID 与参考单价全部支持在网页「系统设置」中实时自定义，随官方模型升级零改代码平滑切换。

---

## 安全与体验红线

- **0 封号红线**：绝不使用任何非官方脚本自动化操控或爬取 `flow.google.com`；手动 Flow 模式只生成本地编号发车包，由你自己在官方网页点击生成并下载，软件自动监听认领。
- **本地优先、密钥安全存储**：全部项目、数据库与素材仅保存在本机 `data/` 目录，绝不擅自上传外部网盘；API Key 在 macOS 自动加密存入系统钥匙串（`antigravity-video-studio`），在 Windows / Linux 自动写入受控 `.secrets.env` 或环境变量，代码与日志零明文回显。
- **代理精准分流、默认不强绑**：代理配置默认留空（直连）；按需填写本地代理后，**仅 Google 域名走代理**，国内热榜与备用文本模型强制直连不偷跑代理流量。
- **真正跨平台轻量化**：同时支持 **macOS / Windows / Linux**，内置预构建静态前端（普通用户无需安装 Node.js），本地编码自动探测硬件加速（VideoToolbox / NVENC）并优雅回退软件编码。

---

## 快速上手 A：Antigravity AI 一句话引导安装与配置（强烈推荐）

如果你正在使用 **Antigravity（反重力）**，无需手工折腾任何 `.env` 文件，直接把本仓库内置的专属导演 Skill 交给 AI 即可全自动问答式起步：

### 第 1 步：拉取仓库并装载 Skill

```bash
git clone https://github.com/a78c7/antigravity-video-studio.git
cd antigravity-video-studio
```

将本仓库设为 Antigravity 当前工作区，或者把导演 Skill 复制到你的全局 Skills 目录：

```bash
# macOS / Linux
mkdir -p ~/.gemini/config/skills
cp -R skills/agvs-director ~/.gemini/config/skills/agvs-director
```

```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force "$HOME\.gemini\config\skills" | Out-Null
Copy-Item -Recurse -Force "skills\agvs-director" "$HOME\.gemini\config\skills\agvs-director"
```

### 第 2 步：给 Antigravity 发一句话

直接在对话中发送：

> **“请读取 `skills/agvs-director/SKILL.md`，帮我初始化配置反重力视频工坊。”**

Antigravity 会自动启动 **「第 0 阶段｜首次开箱智能配置向导」**，通过交互式选择题一步步带你完成：

1. 选择你的 **Google 订阅档位** → 自动校准三口油箱红线与出片策略；
2. 确认 **网络直连或本地代理地址**；
3. 选择是否开启 **国内五大热榜选题雷达**；
4. 选择是否配置 **Google Colab 云端 4K HDR 渲染**（或先用本机 1080p 起步）；
5. 选择 **纯本地单机** 或 **团队自托管协同**；
6. 引导你安全保存 API Key、拉起本地服务并打开浏览器。

### 第 3 步：8 阶段 AI 导演协同制片（4 处强制拍板门禁）

配置完成后，直接告诉 Antigravity 你想拍什么题材（剧情、科幻、萌宠、美食、城市故事、科普皆可），它会严格按 SOP 协同推进：

1. **选题雷达与立项** → 多路搜题、爆款结构对标转译、≥60 秒结构规划；
2. **资产圣经与永久防穿帮** → 短名规范 + `negative_guard_en` 永久护盾 + 防吃书账本；
3. **剧本共创与分镜拆解** → 初稿 + 毒舌主编二次精修 + 原创配乐 + 9:16 分镜；
4. **首尾帧生图（门禁 1 弹窗定图）** → 分级抽卡 + AI 逐张看图排雷 + 编号总览拼图；
5. **视频双模生成（引擎选择 + 门禁 2 弹窗放行）** → CLI 360p 试卡体检/1080p 定稿 或 手动 Flow 发车包；
6. **三轨声音架构（门禁 4 弹窗选配音）** → 原生环境音 + 官方 TTS/真人干声/纯字幕 + BGM 侧链自动闪避；
7. **母带渲染** → 本机 1080p 初审 + Colab 4K/30fps 10-bit HLG HDR 安全区字幕终版（或导出 `.fcpxml` 兜底）；
8. **交付收口** → 9:16 & 3:4 双比例封面、多平台文案、《AI 原创制作证明》、一键清废片。

---

## 快速上手 B：纯命令行手动启动（macOS / Linux / Windows）

### 前置依赖

- **Python 3.12+**（如已安装 `uv`，启动脚本会自动加速建环境装依赖）
- **FFmpeg**（用于本地剪辑、三轨混音与音画体检）：
  - macOS：`brew install ffmpeg`
  - Windows：`winget install Gyan.FFmpeg`
  - Ubuntu / Debian：`sudo apt install ffmpeg`
- *可选*：如需 CLI 自动出图/出视频，安装官方 `gemini-api` CLI；如需云端 4K HDR 渲染，安装官方 `google-colab-cli`。

### 一键启动

**macOS / Linux：**

```bash
chmod +x start.sh
./start.sh
```

**Windows（任选其一）：**

- 直接双击项目根目录的 **`start.bat`**；
- 或在 PowerShell 中运行：

```powershell
.\start.ps1
```

首次运行会自动创建 `.venv` 虚拟环境、安装依赖、启动 `http://127.0.0.1:8765` 并自动打开浏览器（可通过环境变量 `AGVS_PORT` 修改端口，设 `AGVS_NO_BROWSER=1` 禁止自动弹浏览器）。

> 仓库已包含预构建的 `backend/static/` 网页文件；仅当你二次开发前端源码时才需执行 `cd frontend && npm install && npm run build`。

---

## 可选功能模块

- **国内五大热榜选题雷达（可开关）**：内置抖音、今日头条、百度、B站、微博直连热榜抓取，默认开启；做纯原创或海外题材时，可在网页「系统设置」中一键关闭 `ENABLE_HOT_FEEDS`，关闭后界面自动隐藏热榜列。
- **可选自托管多人协作模式**：默认纯本地单机免登录运行；如果需要多人远程审片、共同改剧本/分镜或投票选封面，可按 [deploy/self-hosted/README.md](deploy/self-hosted/README.md) 将协作脱敏版部署到自有 Linux 服务器（零 Google 凭据上云、强制账号登录），并可按需接入开源 `ntfy` 接收实时审片推送。

---

## 仓库目录结构

```text
antigravity-video-studio/
├── backend/                 # FastAPI 后端、SQLite 数据层、媒体/质检引擎与预构建静态页
├── frontend/                # React + TypeScript + Vite 前端源码
├── skills/
│   └── agvs-director/       # Antigravity 专属开箱向导 + 8 阶段 AI 短剧导演 Skill
├── deploy/
│   └── self-hosted/         # 可选：通用 Linux VPS 自托管多人协作参考模板
├── scripts/
│   └── privacy_check.py     # 通用开源防泄密与本地黑名单巡检脚本
├── start.sh                 # macOS / Linux 一键启动脚本
├── start.ps1 / start.bat    # Windows 一键启动脚本
├── pyproject.toml           # Python 项目与精简依赖声明
├── .env.example             # 脱敏配置模板
└── LICENSE                  # GNU AGPL-3.0 协议全文
```

---

## 开发者防泄密巡检

项目内置零外部依赖的隐私巡检脚本，可一键排查明文密钥、私网 IP、硬编码本机路径及意外追踪的敏感文件，也支持通过被 `.gitignore` 忽略的 `.privacy-blocklist.local` 追加你自己的自定义禁词或正则：

```bash
python3 scripts/privacy_check.py
# 可选：挂载为 git pre-commit hook
ln -sf ../../scripts/privacy_check.py .git/hooks/pre-commit
```

---

## 开源协议与免责声明

- **开源协议**：本项目基于 **GNU Affero General Public License v3.0（AGPL-3.0）** 开源，完整条款见 [LICENSE](LICENSE)。基于本项目修改并通过网络对外提供托管服务时，须按 AGPL-3.0 协议公开对应修改源码。
- **免责声明**：
  1. 本项目为非官方社区开源工具，与 Google / Antigravity 官方无任何隶属关系。
  2. 请遵守 Google Gemini API、Google Flow、Google Colab 及各视频发布平台的服务条款与额度规定，合理合规使用官方能力。
  3. 创作者须自行确保所创作剧本、角色、音频与画面内容合法合规，不侵犯任何第三方肖像权、著作权或商标权。
