# 反重力视频工坊｜本地制片台 API 速查、费用表与排错手册

> **默认服务基址**：`http://127.0.0.1:8765`（端口可通过环境变量 `AGVS_PORT` 自定义）
> **调用铁律**：
> 1. 如果你的终端配置了全局 HTTP/SOCKS 代理，**所有 `curl` 调本地 `127.0.0.1` 服务务必加上 `--noproxy '*'`**（Python `httpx` 用 `trust_env=False`），防止本地环回请求误走代理导致超时或 502。
> 2. 严禁在命令行、Payload、日志或对话中打印/拼接明文 API Key（macOS 自动走系统钥匙串 `antigravity-video-studio`，Windows/Linux 自动走受控 `.secrets.env` 或环境变量）。
> 3. 所有路径与字段均对应 `backend/routers/{topics,studio,delivery,analytics,collab,sync_local}.py`。

---

## 一、系统健康、订阅档位预设、三口油箱与算力探测

| 能力 | 方法与路径 | 请求 / 说明 |
| :--- | :--- | :--- |
| 服务探活 | `GET /api/health` | 返回 `{"status":"ok","app":"antigravity-video-studio","brand_name":"反重力视频工坊","app_mode":"local_producer"}` |
| 获取 Google 订阅档位预设 | `GET /api/subscription-presets` | 返回 `free` / `ai_pro` / `ai_ultra` / `ai_ultra_dev` / `custom` 档位说明与推荐额度 |
| 一键应用订阅档位预设 | `POST /api/quota/apply-preset` | `{"tier":"ai_ultra_dev"}` → 写入 `SUBSCRIPTION_TIER` 并同步校准三口油箱红线，返回最新 `quota` 与 `config` |
| 三口油箱余额监控 | `GET /api/quota/summary` | 返回 `gemini`（本月赠金实耗与 `remaining_credit_usd`）、`flow`（月积分）、`deepseek` |
| 手动校准油箱额度 | `POST /api/quota/calibrate` | `{"gemini_daily_limit":250,"gemini_monthly_credit_usd":100.0,"flow_monthly_total":25000,"flow_remaining":24000}`（字段均可选） |
| 探测 Colab 状态与 CU 余额 | `GET /api/settings/probe-colab` | 检查官方 `colab` CLI / OAuth2 令牌、可用 CU 余额与活跃运行时 |
| 强制释放 Colab 运行时 | `POST /api/settings/colab/stop-session` | `{"endpoint":"..."}`（用完立即停机防空烧 CU） |
| 查看/修改配置 | `GET /api/settings` · `PATCH /api/settings` | 返回全部脱敏配置，支持修改 `SUBSCRIPTION_TIER`, `ENABLE_HOT_FEEDS`, `ENABLE_CLOUD_COLLAB`, `GOOGLE_PROXY_URL`, `CLI_*_MODEL`, `CLI_PRICING_JSON` 等 |
| 查看/修改提示词模板 | `GET /api/prompt-templates` · `PATCH /api/prompt-templates/{key}` | 内置键：`topic_scorer`, `benchmark_adapter`, `script_writer`, `shot_breaker`, `script_critic`, `multi_platform_copy`, `comment_reply`, `analytics_review` |

```bash
# 开拍 0 步：查服务存活 + 当前三口油箱余额
curl --noproxy '*' -s http://127.0.0.1:8765/api/health
curl --noproxy '*' -s http://127.0.0.1:8765/api/quota/summary

# 首次初始化：查看并应用订阅档位预设
curl --noproxy '*' -s http://127.0.0.1:8765/api/subscription-presets
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/quota/apply-preset \
  -H 'Content-Type: application/json' -d '{"tier":"ai_ultra_dev"}'
```

---

## 二、阶段 1｜选题雷达（国内热榜开关 · 通用联网搜题 · 爆款对标转译）

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| 支持的热榜源 | `GET /api/hot-sources` | `douyin` / `toutiao` / `baidu` / `bilibili` / `weibo`（直连免代理；若 `ENABLE_HOT_FEEDS=false` 会标注未启用） |
| 拉实时热榜 | `GET /api/hot/{source}?force=false` | `force=true` 跳过 10 分钟缓存；关闭热榜开关时优雅返回 `enabled: false` 与空列表 |
| 热榜送 AI 赛道打分 | `POST /api/topics/score-hot` | `{"items":[{"title":"...","source":"douyin","hot_value":1200000}],"provider":null}` → 返回 `score_theme` 等 5 维分与双轨标签 `content_tier` |
| **通用主题联网搜题** | `POST /api/topics/search-topic` | `{"keyword":"深夜食堂","theme":"治愈系微短剧","provider":null}` |
| **全网爆款结构对标转译雷达** | `POST /api/topics/search-benchmarks` | Body 可选：`{"focus":"高完播科幻微短剧","theme":"赛博朋克萌宠","keyword":"","count":6,"provider":null}` → 调用 `benchmark_adapter` 模板转译成原创选题 |
| 查询选题库 | `GET /api/topics?grade=S&status=new&content_tier=【精品口碑级】` | Query 参数均可选，包含 `score_theme` 主题契合分 |
| 手动录选题 | `POST /api/topics` | `{"title":"...","source":"manual","summary":"","ai_pitch":"","recommended_project_id":1}` |
| 选题立项为新分集 | `POST /api/topics/{topic_id}/promote` | `{"project_id":1}` → 返回新建 `episode` |
| 平台活动与流量扶持雷达 | `GET /api/campaigns?include_archived=false` | 返回当期活动、切入角 `angle_pitch` 与必带 `#话题` |
| 活动一键转选题 | `POST /api/campaigns/{campaign_id}/to-topic` | 无 Body，自动挂接活动标签入库 |

```bash
# 全网爆款结构对标转译（任意题材，按项目主题自动适配）
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/topics/search-benchmarks \
  -H 'Content-Type: application/json' \
  -d '{"focus":"强反转萌宠科幻微短剧","theme":"3D 虚拟搭档","count":6}'
```

---

## 三、阶段 2–3｜剧目、资产圣经（防穿帮护盾）、剧本与分镜

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| 剧目列表 / 创建 / 更新 | `GET /api/projects` · `POST /api/projects` · `PATCH /api/projects/{project_id}` | `PATCH` 可更新 `season_outline`、`continuity_ledger`（防吃书账本）、`global_style_prompt` |
| 资产列表 | `GET /api/projects/{project_id}/assets` | 返回含 `negative_guard_en` 的全部跨集资产 |
| 新建资产（带永久护盾） | `POST /api/projects/{project_id}/assets` | `{"asset_type":"character","name":"角色_机械猫01","flow_tag":"@MechaCat01","en_anchor_prompt":"...","negative_guard_en":"strictly NO organic fleshy paws, keep matte white ceramic shell and tiny brass bell collar intact"}` |
| 更新资产 / 补写护盾 | `PATCH /api/assets/{asset_id}` | 支持 `negative_guard_en`、`en_anchor_prompt`、`name`、`flow_tag`、`notes` 等 |
| 上传实拍底图 / 定妆图 | `POST /api/assets/{asset_id}/upload` | `multipart/form-data`: `slot=raw\|clean\|voice_sample`, `file=@/path/to/photo.jpg` |
| CLI 棚拍洗图 | `POST /api/assets/{asset_id}/cli-generate-clean` | Body 可选：`{"image_size":"2K","count":2,"mode":"portrait"}`（`mode`: `portrait`\|`full`） |
| 选定棚拍候选图 | `POST /api/assets/{asset_id}/select-clean-candidate` | `{"candidate_path":"/path/to/candidate.jpg"}` |
| 分集列表 / 新建 / 更新 | `GET /api/projects/{project_id}/episodes` · `POST /api/projects/{project_id}/episodes` · `PATCH /api/episodes/{episode_id}` | `PATCH` 支持 `title`, `hook_0_3s`, `ending_cta`, `next_ep_hook`, `script_text`, `bgm_prompt`, `bgm_volume`, `vo_mode`, `selected_cover_path`, `status` |
| AI 生成剧本（双轮主编精修） | `POST /api/episodes/{episode_id}/generate-script` | `{"topic_title":"...","topic_pitch":"...","extra_notes":"说人话、强前3秒钩子、≥62秒","editorial_mode":true,"provider":null}` |
| 毒舌主编二次挑刺返工 | `POST /api/episodes/{episode_id}/critique-script` | `{"extra_notes":"压短台词、强化第1镜视觉冲突","provider":null}` |
| AI 拆解 9:16 分镜 | `POST /api/episodes/{episode_id}/break-shots` | `{"replace_existing":true,"provider":null}` → 返回全集分镜 + 编译后 Prompt（含 `active_guards`） |
| 查看 / 新增 / 修改分镜 | `GET /api/episodes/{episode_id}/shots` · `POST /api/episodes/{episode_id}/shots` · `PATCH /api/shots/{shot_id}` | `PATCH` 可改 `frame_prompt_en`, `end_frame_prompt_en`, `video_prompt_en`, `cn_dialogue`, `subtitle_text`, `audio_strategy`, `duration_sec`, `trim_start`, `trim_end`, `ref_asset_ids` |
| CLI 生成原创配乐 BGM | `POST /api/episodes/{episode_id}/cli-generate-bgm` | Body 可选：`{"prompt":"..."}`（默认读 `episodes.bgm_prompt`） |
| 手动上传 BGM | `POST /api/episodes/{episode_id}/upload-bgm` | `multipart/form-data`: `file=@/path/to/bgm.mp3` |

---

## 四、阶段 4｜首尾帧 CLI 生图与门禁 1 编号拼图

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| CLI 生单镜首帧/尾帧 | `POST /api/shots/{shot_id}/cli-generate-frame` | `{"slot":"start_frame","image_size":"2K","count":4}`（重点镜 `count=4`，普通镜 `count=2`；尾帧用 `slot="end_frame"`，自动带本镜首帧作第 1 参考） |
| 锁定单镜首/尾帧冠军 | `POST /api/shots/{shot_id}/select-frame-candidate` | `{"slot":"start_frame","candidate_path":"/path/to/cli_candidates/xxx.jpg"}` |
| **门禁 1 编号对比总览图** | `POST /api/episodes/{episode_id}/qc-frames-sheet` | Body 可选：<br>• `{}`：拼全集定稿首尾帧总览 JPG；<br>• 指定单镜候选：`{"shot_no":1,"slot":"start_frame","include_candidates":true,"badges":["AI推荐","","",""],"cols":2}`；<br>• 显式路径：`{"candidate_paths":["..."],"labels":["#1 S01 A"],"badges":["AI推荐"],"cols":2,"title":"..."}`<br>返回 `sheet_path`（供 `read_file` 看图与展示给用户） |

```bash
# 全集首尾帧编号总览图（空 JSON 默认模式）
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/episodes/1/qc-frames-sheet \
  -H 'Content-Type: application/json' -d '{}'
```

---

## 五、阶段 5｜视频双模调度、手动 Flow 发车与 3 帧+音轨体检

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| **CLI 生单镜视频** | `POST /api/shots/{shot_id}/cli-generate-video` | 试卡：`{"resolution":"360p","count":1}`；定稿：`{"resolution":"1080p","count":1,"duration_sec":null}`（自动挂首/尾帧或参考资产与防穿帮护盾，瞬时故障轻量重试 1 次） |
| 标记某条 Take 为定稿 | `PATCH /api/takes/{take_id}` | `{"is_final":1,"rating":5,"notes":"1080p 验收通过"}` |
| **手动 Flow 一键发车包** | `POST /api/episodes/{episode_id}/prepare-manual-flow-pack` | Body 可选：`{"clear_existing":true,"include_assets":true,"image_count":4,"image_model":"Nano Banana 2.1 Pro","video_count":2}` → 输出 `pack_dir`（编号首尾帧）与 `checklist_path`（`逐镜Flow提示词清单.md`） |
| 收件箱扫描与认领 | `GET /api/inbox` · `POST /api/inbox/scan` · `POST /api/inbox/claim` | `claim`: `{"inbox_item_id":"...","target_type":"shot_video","target_id":12}`（`target_type`: `shot_video`\|`shot_start_frame`\|`shot_end_frame`\|`asset_clean`\|`asset_raw`） |
| **3 帧画面 + 音轨自动化体检** | `POST /api/episodes/{episode_id}/qc-inspect-videos` | Body 可选：`{"resolution_label":"360p","shot_ids":null,"cols":1}`（`resolution_label` 可选 `"360p"`/`"720p"`/`"1080p"`/`null`）→ 返回 `overview_sheet_path`、`warning_count` 及每镜 `qc_strip_path` + `report`（含 `mean_volume_db`、`max_volume_db`、`voice_band_mean_db`、`warnings`） |

```bash
# 执行 3 帧 + 音轨自动化体检
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/episodes/1/qc-inspect-videos \
  -H 'Content-Type: application/json' -d '{}'

# 生成手动 Flow 发车包
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/episodes/1/prepare-manual-flow-pack \
  -H 'Content-Type: application/json' -d '{}'
```

---

## 六、阶段 6｜三轨声音架构与配音接口

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| **官方 CLI 批量生成中文 TTS 旁白** | `POST /api/episodes/{episode_id}/cli-generate-tts` | Body 可选：`{"voice":"Kore","style_prompt":"温暖自然的电影旁白语气，语速适中","shot_ids":null}` → 为含 `cn_dialogue`/`subtitle_text` 的镜头生成 24kHz WAV 并绑定 |
| **上传真人/克隆干声或清为纯字幕** | `POST /api/shots/{shot_id}/upload-vo` | • 上传干声：`curl --noproxy '*' -X POST ... -F "file=@vo.wav" -F "vo_source=human"`<br>• 切纯字幕无旁白：`POST /api/shots/{shot_id}/upload-vo?clear=true` 或 JSON `{"clear":true}` |

---

## 七、阶段 7｜时间线、本机 1080p/HLG 渲染与 Colab 4K HLG HDR 终版

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| 查看时间线与三轨状态 | `GET /api/episodes/{episode_id}/timeline` | 每镜带 `vo_file_path`、`vo_source`、`qc_strip_path`、`effective_duration`、`missing_shots` |
| **本机真 1080p 三轨初审 / 本地 HLG** | `POST /api/episodes/{episode_id}/render-local` | Body 可选：`{"hdr_hlg":false,"burn_subtitles":true,"bgm_volume":null}`；自动按平台选用硬件/软件编码器（VideoToolbox / NVENC / libx265），字幕压在底部 **24%–26% 黄金安全区** |
| **Colab 4K/30fps + 10-bit HLG HDR 终版** | `POST /api/episodes/{episode_id}/dispatch-colab` | Body 可选：`{"gpu_tier":"L4","task_mode":"upscale_4k_30fps","hdr_hlg":true,"burn_subtitles":true}`<br>`task_mode` 可选：`upscale_4k_30fps`（默认）\| `upscale_4k_60fps` \| `ai_60fps_1080p` \| `probe_gpu` |
| 导出三轨 `.fcpxml` + `.srt`（兜底） | `POST /api/episodes/{episode_id}/export-fcpxml` | 无 Body；原生音在主轨、VO 在 `lane="1"`、BGM 在 `lane="-1"` |

```bash
# 本机 1080p 三轨 + 安全区字幕初审
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/episodes/1/render-local \
  -H 'Content-Type: application/json' \
  -d '{"hdr_hlg":false,"burn_subtitles":true}'

# 派发 Colab L4 输出 4K/30fps + 10-bit HLG HDR + 安全区字幕终版
curl --noproxy '*' -s -X POST http://127.0.0.1:8765/api/episodes/1/dispatch-colab \
  -H 'Content-Type: application/json' \
  -d '{"gpu_tier":"L4","task_mode":"upscale_4k_30fps","hdr_hlg":true,"burn_subtitles":true}'
```

---

## 八、阶段 8｜自动双比例封面、多平台文案、AI 原创证明与清废片

| 能力 | 方法与路径 | 请求参数 / Payload 示例 |
| :--- | :--- | :--- |
| **自动生成 9:16 + 3:4 封面候选** | `POST /api/episodes/{episode_id}/cli-generate-covers` | Body 可选：`{"prompt_916":null,"prompt_34":null,"headline":"","highlight_shot_ids":null,"count_per_ratio":2,"image_size":"2K"}` → 返回 `sheet_path`、`covers_9_16`、`covers_3_4`、`candidates`、`total_cost_usd` |
| **锁定选定封面** | `POST /api/episodes/{episode_id}/select-cover` | `{"candidate_index":1}`（1-based 编号）或 `{"cover_path":"/path/to/cover.jpg"}` 二选一 |
| 生成多平台差异化文案 | `POST /api/publish-copy/generate` | `{"episode_id":1,"extra_notes":"同步带当期活动话题"}` |
| 导出《AI 原创制作证明（`.md`）》 | `POST /api/episodes/{episode_id}/export-ai-proof` | 无 Body → 返回 `proof_path` |
| **一键清理本集未定稿废片** | `POST /api/episodes/{episode_id}/cleanup-drafts` | 无 Body → 删除 `is_final=0` 的 360p 试卡与落选 Take，保护定稿、参考图与终版成片；返回 `removed_files`、`freed_mb` |
| 发布数据复盘 & 评论草稿 | `POST /api/publish-logs` · `POST /api/comments/draft` · `POST /api/comments/{draft_id}/to-topic` | 记录播放/完播/点赞、生成安全评论回复、灵感一键转下集选题 |

---

## 九、默认 CLI 模型与估算单价参考（均可在设置页动态覆写）

> 模型 ID（`CLI_IMAGE_MODEL`、`CLI_VIDEO_MODEL`、`CLI_MUSIC_MODEL`、`CLI_TTS_MODEL`）与单价（`CLI_PRICING_JSON`）均可通过「系统设置」或 `PATCH /api/settings` 随 Google 官方迭代即时调整，无需改代码。

| 能力 | 默认模型 ID | 内置参考单价 | 单集（~62s · 10 镜）参考估算 |
| :--- | :--- | :--- | :--- |
| **生图 / 首尾帧 / 封面** | `gemini-nano-banana-2.1` | `1K`: `$0.0336/张` · **`2K`: `$0.0504/张`** · `4K`: `$0.113/张` | 分级抽卡 ~26 张（2K）≈ **`$1.31`**；4 张双比例封面 ≈ **`$0.20`** |
| **视频试卡 / 定稿** | `gemini-omni-1.2-flash` | **`360p`: `$0.023/s`** · `720p`: `$0.07/s` · **`1080p`: `$0.104/s`** · `4k`: `$0.209/s` | 62s 360p 试卡 ≈ **`$1.43`**；62s 1080p 定稿 ≈ **`$6.45`** |
| **原创配乐 BGM** | `lyria-3.5` | 固定 **`$0.08/首`** | 1 首 ≈ **`$0.08`** |
| **中文旁白 TTS** | `gemini-3.8-flash-tts` | 估算 **`$0.01/句`** | 2–5 句 ≈ **`$0.02–$0.05`** |
| **全流程参考合计** | — | — | 约 **`$8–$10 / 集`**（以官方实际账单为准） |

---

## 十、常见排错速查

1. **调 `127.0.0.1:8765` 报 502 / Connection Reset / 极慢超时**
   - 原因：终端环境变量设置了全局 HTTP 代理，把本机环回请求导进了代理。
   - 解决：`curl` 必加 `--noproxy '*'`；Python `httpx` 使用 `trust_env=False`。
2. **端口 8765 未响应或需重启服务**
   - macOS / Linux：用 `lsof -tiTCP:8765 -sTCP:LISTEN` 检查旧进程，确认后在项目根目录运行 `./start.sh`。
   - Windows：在 PowerShell 运行 `Get-NetTCPConnection -LocalPort 8765`，结束后重新运行 `.\start.ps1` 或双击 `start.bat`。
3. **CLI 报错 401 / Keyring 未授权 / 代理超时**
   - `gemini-api` 通过自身系统 keyring 鉴权，`cli_media.py` 会自动注入设置页中保存的 `GOOGLE_PROXY_URL`（留空则直连）。
   - 切勿在命令行或日志里回显明文 Key；若网络超时先核查代理设置是否可达，若报 400/配额不足先查 `GET /api/quota/summary`。
4. **音轨体检出现警告（`warning_count > 0`）怎么判**
   - `mean_volume_db < -48`：疑似静音/丢环境音，复查该镜是否有声。
   - `max_volume_db > -0.3`：疑似削顶爆音，必要时重拍或压低音量。
   - 无对白镜（`expect_dialogue=false`）检出中频（300–3400Hz）人声能量异常集中：**疑似模型幻觉乱说话**，复听后按需切 `audio_strategy="mute_for_local_vo"` 或重拍。
5. **验收 10-bit HLG HDR 成片与安全区字幕的快速命令**
   ```bash
   # 核查分辨率、像素格式、BT.2020 + HLG (arib-std-b67) 与音频流
   ffprobe -v error -show_entries stream=codec_type,codec_name,profile,pix_fmt,width,height,color_primaries,color_transfer,color_space,sample_rate,channels -of default=noprint_wrappers=1 "<成片.mp4>"

   # 抽取带字幕时间点静帧，再用 read_file 核对底部 24%–26% 安全区无遮挡
   ffmpeg -y -ss 18 -i "<成片.mp4>" -frames:v 1 qc_sub_check.jpg
   ```
6. **Colab 派发报 401 / GPU 分配失败**
   - 先调 `GET /api/settings/probe-colab` 检查 OAuth 令牌与 CU 余额；默认 `gpu_tier="L4"` 带自动降级，若有闲置残留运行时先调 `POST /api/settings/colab/stop-session` 释放。

---

## 十一、可选：本地 ↔ 自托管远程协作端同步接口（`/api/local-sync/*`）

> 仅在本地模式（`APP_MODE=local_producer`）且已配置 `CLOUD_STUDIO_URL` 时启用；在自托管协作端（`APP_MODE=cloud_collab`）自动禁用（返回 404）。同步请求使用 `trust_env=False` 直连用户自建服务端，通过 `X-Sync-Token` 鉴权，**绝不上传任何 `.env`、Google 凭据或油箱日志**。

| 能力 | 方法与路径 | 请求参数 / 说明 |
| :--- | :--- | :--- |
| 检测远程协作端状态 | `GET /api/local-sync/status` | 未配置时清晰返回未启用状态；已配置时探测连通性、延迟与同步游标 |
| 推送单集至协作端 | `POST /api/local-sync/push-episode/{episode_id}` | Body 可选：`{"notify_stage":"master_ready","include_final_master":true,"force_upload":false}`；高码率大文件自动压制轻量 `web_preview.mp4` 供团队网页秒播 |
| 拉回团队成员最新修改与投票 | `POST /api/local-sync/pull` | Body 可选：`{"since":""}` → 按时间戳合并团队剧本/分镜修订、投票 `review_votes`、规则建议 `rule_suggestions` 与新选题，返回 `readable_summary` |
