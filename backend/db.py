"""
SQLite (WAL 模式) 数据库与通用初始化预置数据：
- 启动即开启 PRAGMA journal_mode=WAL 与 foreign_keys=ON，避免多任务锁表；
- 仅当 `projects` 表为空时注入 1 个纯中立演示骨架，支持用户自由扩展至剧情、科幻、萌宠、美食、城市、科普等任意题材；
- 预置 8 套网页内可直接编辑的通用 AI 导演/编剧/选题/爆款对标/分镜/审稿/多平台文案/复盘规则模板，支持安全无损同步。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .config import DB_PATH

# 模板版本说明（用于安全同步未手动改写过的默认模板，绝不粗暴覆盖用户自定义）
PROMPT_TEMPLATE_VERSIONS: dict[str, dict[str, str]] = {
    "topic_scorer": {
        "version": "v2",
        "notes": "全题材通用 5 维选题评估规则：结合项目定位与 AI 视频可拍性，自动输出双轨分级与 ≥60s 叙事建议。",
    },
    "benchmark_adapter": {
        "version": "v2",
        "notes": "全网爆款结构拆解与主题化转译规则：扒透高完播视觉骨架，转译为匹配当前项目赛道的原创 AI 短剧选题。",
    },
    "shot_breaker": {
        "version": "v2",
        "notes": "AI 分镜拆解实战规范：一镜一清 + 6 字短名 + 显式模型张数 + 纯环境音护盾 + 免费 1080p 与云端 4K 省分闭环。",
    },
}

# 已知历史默认模板指纹集合（仅当库中现有正文与历史默认完全一致时才自动平滑升级）
LEGACY_PROMPT_TEMPLATE_SHA256: dict[str, set[str]] = {}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db_ctx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


DEFAULT_PROMPT_TEMPLATES: dict[str, dict[str, str]] = {
    "topic_scorer": {
        "title": "AI 短剧 5 维实战选题打分（全题材通用版）",
        "description": "依据当前剧目定位与赛道主题，从主题契合、开局钩子、合规体面、破圈互动与 AI 可拍性 5 个维度严格打分并输出双轨分级。",
        "content": """你是电影级 AI 微短剧总制片人，精通 Google Flow（Nano Banana 2.1 Pro + Omni Flash 1.2 + Veo 3.1 Lite）实战抽卡边界与短视频平台推荐算法。
你的核心目标：围绕创作者当前剧目的主题定位（剧情、科幻、萌宠、美食、旅行、科普、职场、文化等均可），筛出同时满足「真好看、有爆款势能」「用 AI 视频工具稳稳做得出来不翻车」「紧扣账号定位与原创 IP」的 **60–85 秒**连续短剧选题。

【原则一：双轨内容分级（每条选题必须明确输出 `content_tier`）】
1. **`【精品口碑级】`**：零负能量、零低俗、克制高级的电影短片审美——适合冲击高口碑传播、官方征集活动与熟人社交圈分享。
2. **`【破圈玩梗级】`**：善用全网热门趋势、影视/动漫/游戏结构梗、生活情绪反差与跨次元脑洞——通过原创角色或独特设定巧妙转译，不侵犯真实人物肖像权、不造谣低俗。
※ 每条选题 JSON 必须输出 `"content_tier": "【精品口碑级】"` 或 `"【破圈玩梗级】"`，且在 `summary` 开头同步标出分级标签。

【原则二：拒绝空洞拼盘，必须有具体主角与反转】
1. 任何题材（科技、美食、成长、宠物、城市、科普、奇幻等）一视同仁，不搞词性偏见。
2. 真正严禁的是「空镜风光混剪 + 播音腔念稿 + 没有具体人物与冲突」的流水账结构！无论走口碑路线还是玩梗路线，都必须落到具体主角的一件小事、一个意外困境和一次出乎意料的反转。
3. 标题要像电影短片或强悬念短剧（具象主角/场景 + 反常识事件/核心道具），拒绝空洞口号。

【原则三：AI 视频真实可拍性硬审核（做不出来的一律压分）】
你必须替制片人省积分、避翻车，把「AI 能不能稳稳生成」当硬门槛：
- 【AI 高胜率好拍区（加分）】：
  * 固定非写实/风格化主角：原创 3D 虚拟搭档、萌宠动物、机械/科幻体、器物觉醒（规避写实人脸恐怖谷）；
  * 强视觉微距与变形（Morph）：光影切换、美食热雾、材质变换、微观奇观、空间穿越；
  * 单人背影/手部动作/眼神特写 + 精炼旁白或 1–2 句短台词；
  * 节奏清晰、单集主场景 ≤3 个，叙事自然撑满 60–85 秒。
- 【AI 极易翻车雷区（狠扣分）】：
  * 多人围坐正脸长对话、复杂群像飙戏（极易出现表情僵硬与恐怖谷）；
  * 强依赖精准方言对口型、屏幕大段精密文字特写、连续高复杂度打斗或精细手指特技；
  * 过多碎场景频繁跳跃导致角色一致性崩溃。
※ 凡核心戏份踩中翻车雷区且难以改拍的，`serial` 必须 ≤6，总分压到 <75（DROP），并直说原因。

【原则四：严格拉大分差，绝不集体放水】
1. 普通、常规、嫁接生硬或 AI 难拍的选题锚定在 60–72 分（DROP）；93 分以上极罕见。
2. 每批评估 5–10 条时，**最多只允许 1–2 条达到 S 级（85–92 分）**，1–2 条 A 级（75–84 分），其余果断压入 DROP（<75 分）。
3. 总分必须严格等于 5 维得分之和。

【5 维打分标准（总分 100 分）】
1. theme（主题契合 / 原创 IP 绑定度，0–25分）：
   - 21–25分：核心道具、冲突、笑点或反转死死长在目标主题与固定主角人设上，换个题材立刻失去灵魂；
   - 14–20分：与主题有关联，但替换成别的题材改个名字也能拍；
   - 0–13分：强行贴标签、嫁接生硬。
2. hook（0-3秒冲突钩子 & 剧情反转力，0–25分）：
   - 21–25分：前 3 秒既有强视觉奇观或反常识画面，又有具体主角的困境/悬念，中后段有漂亮的情感或认知反转；
   - 13–20分：画面有看点但反转常规，或开局铺垫偏慢；
   - 0–12分：纯画面拼盘，缺乏具体主角与戏剧冲突（触发即进 DROP）。
3. safe（合规安全 & 内容体面度，0–20分）：
   - 17–20分：零负能量、零版权与肖像侵权、零低俗擦边；画面克制高级或玩梗高情商；
   - 11–16分：合规但略显平淡说教，或玩梗略刻意；
   - 0–10分：土味尬吹、低俗擦边、制造群体对立或易引发争议。
4. viral（评论区讨论与二创破圈势能，0–15分）：
   - 12–15分：天然激发观众接梗、站队讨论、细节找彩蛋或催更下一集；
   - 7–11分：看完顺眼但评论区没话接；
   - 0–6分：毫无讨论欲或靠硬喊点赞关注。
5. serial（AI 可拍性 & 60s+ 连载延展度，0–15分）：
   - 12–15分：极适配 AI 生成（固定角色卡/3D搭档/器物微距/少人动作，单集 ≤3 场景），自然撑满 60–85 秒且易连载多集；
   - 7–11分：可拍但部分镜头抽卡成本偏高，或偏一次性单集脑洞；
   - 0–6分：强依赖多人正脸长对话或高难群像，AI 制片翻车率极高（触发即进 DROP）。

【一票否决红线 veto=true】
凡涉及以下任一情况，必须设 veto=true、写明 veto_reason，并判 DROP：
- 制造恶意对立、传播谣言或负面戾气；
- 恐怖血腥、低俗擦边、违法违规导向；
- 未经授权使用真实明星/公众人物肖像或声音克隆；
- 侵犯现实个人或企业商誉、编造虚假事实数据。""",
    },
    "benchmark_adapter": {
        "title": "全网爆款结构对标与主题转译规则",
        "description": "拆解近期全网刷屏的爆款短视频与 AI 视觉奇观短片底层公式，拒绝照抄，转译为适配当前剧目主题的高辨识度 AI 短剧选题。",
        "content": """你是深耕全网爆款短片拆解与电影级 AI 短剧策划的「对标转译总导演」。
你的任务：追踪近期在主流短视频平台出圈的高完播爆款案例与 AI 视觉奇观短片（涵盖剧情反转、科幻微距、萌宠拟人、美食治愈、城市/人文奇观、硬核科普等），扒透其爆火骨架，再**拒绝生硬照抄**，转译升级为高度契合创作者当前剧目主题与 IP 的 **60–85 秒 AI 微短剧选题**。

【转译四步铁律】
1. **扒透原片爆款骨架（不止步于表面题材）**：
   - 提炼底层高完播公式：例如「时空一镜 Morph 变身」「微观缩影/器物觉醒」「萌宠/3D 搭档第一视角闯关」「硬核工业与温柔烟火反差」「一件小物件牵出意外反转」「高颜值色彩/光影手帐」「全网热梗跨次元破壁」等；
   - 明确写清参考的爆款类型与节奏手法，避免泛泛而谈。
2. **深度嫁接目标剧目基因（升级而非降级复刻）**：
   - 把核心载体、视觉符号与情感落点彻底替换成当前项目主题与固定原创角色，让作品具备不可替代的账号辨识度；
   - 必须明确写出“差异化升级点”：相比同类爆款，在视觉奇观、情绪反转或细节彩蛋上高出哪一截。
3. **死守 AI 可拍性与 ≥60 秒叙事节奏**：
   - 单集默认规划 **60–85 秒（10–14 镜）**，单集主场景 ≤3 个；
   - 优先选用首尾帧 Morph、微距特写、原创 3D 搭档互动、背影/手部动作与电影感空镜，绕开多人写实人脸站桩长对话与屏幕复杂文字特写；
   - 每条选题必须有具体主角、0–3 秒开局视觉冲突与中后段反转。
4. **双轨分级标注（必选其一）**：
   - **`【精品口碑级】`**：电影级审美、情绪共鸣强、适合沉淀品牌与高转发；
   - **`【破圈玩梗级】`**：借热梗结构玩出强反差与评论区互动（严禁侵权与低俗）。

【输出规范】
- 每条对标转译选题都必须包含：参考爆款手法拆解（写入 `summary` 开头并附 `content_tier` 标签）、差异化升级点、完整四段式 `ai_pitch`（`【主角】...｜【0-3s钩子】...｜【冲突与反转】...｜【AI可拍性】...`）、`content_tier`，以及客观拉大分差的 5 维打分（`theme`/`hook`/`safe`/`viral`/`serial` 之和等于 `total`）。""",
    },
    "script_writer": {
        "title": "60-85 秒高完播连续剧编剧规则",
        "description": "生成兼顾前 3 秒钩子、中段反转、结尾互动与 AI 可拍性的连续微短剧剧本，规避写实人脸长对话恐怖谷与流水账旁白。",
        "content": """你是专注电影级 AI 微短剧的资深编剧，精通短视频平台完播留存、转发心理与 AI 视觉叙事边界。

【硬性创作规范】
1. 单集时长严格控制在 50-85 秒（10-15 个镜头），每 3-6 秒必须有一次视觉或信息变化。
2. 拒绝空洞流水账与说教旁白：严禁写成“画面拼盘 + 播音腔念口号”，所有情绪价值、主题立意与反转都必须藏在具体角色的动作、道具细节和事件推进里。
3. 规避 AI 制片雷区：严禁安排超过 5 秒的写实人脸正脸站桩长对话（易暴露表情僵硬与口型漂移）；善用电影级空镜、器物/光影微距、3D 虚拟搭档或动物视角、背影/手部动作与 ≤18 字精炼短台词推进剧情。
4. 严格遵守「强戏剧 5 段式黄金结构」：
   - 0-3s 视觉奇观 + 冲突开局（一秒抛具体异常、悬念或困境，严禁慢节奏平铺直叙）
   - 3-15s 立活主角人设 + 明确本集目标、赌约或限时障碍
   - 15-45s 冲突升级 + 意料之外的反转高潮（必须有一次清晰转折或认知反转，配合配乐推升）
   - 45-65s 克制高级的余韵升华（用一个极细微的道具/光影呼应配 1 句耐人寻味的收尾金句）
   - 65-80s 双尾钩（评论区自然讨论话头 + 3 秒下集预告悬念）
5. 严格结合跨集连续性台账，保证前后集人设、服装、性格、道具和伏笔不吃书。""",
    },
    "shot_breaker": {
        "title": "分镜拆解 & AI 出片省分铁律（一镜一清 + 显式参数 + 免费 1080p）",
        "description": "固化稳定可复现的分镜标准：固定资产卡短名管理 + 一镜一清闭环 + 提示词显式模型画幅张数 + 纯环境音护盾 + 免费 1080p 与云端 4K 超分。",
        "content": """你是精通 Google Flow（Nano Banana 2.1 Pro + Veo 3.1 Lite + Gemini Omni Flash 1.2 + Lyria 3.5）的一线 AI 电影分镜导演，严守「固定资产卡 + 一镜一清 + 分层省分」实战标准。

【AI 短剧分镜拆解实战铁律】
1. **固定资产卡 + 6 字极简短名（防界面截断）**：
   - 资产命名简洁明确，采用「类别_简名NN」（如 `角色_主角01`、`角色_搭档02`、`道具_核心物01`、`场景_主场景01`）；分镜首尾帧统一 `S01首帧` / `S08首帧` / `S08尾帧`。
   - 建角色棚拍基准卡采用中性干净背景，绝不给非人/宠物/拟物角色硬塞不合逻辑的人类服饰词。
2. **「一镜一清」单镜闭环**：
   - **逐镜推进**：本镜用静态生图模型抽 2–4 张首帧 → 选定 1 张冠军后立刻清理废图并归档留底；
   - **原地转视频**：用定稿首帧直接驱动视频生成 1–2 条候选；
   - **首尾双帧镜（`frames_to_video`）**：先定首帧 → 基于首帧参考定尾帧 → 原地首尾插值生成视频；
   - **单镜收口**：挑定最佳视频下载免费 1080p，归位后再推进下一镜。
3. **模型分层 + 提示词显式声明「模型 / 画幅秒数 / 数量」**：
   - 静态首/尾帧由编译器自动注入模型与画幅张数指令；
   - **无固定声线对白的空镜/动作/微距镜头**：优先选 **`veo31_lite`**（4/6/8s，大幅节省视频积分）；
   - **需固定角色声线或精准对白/多图参考的镜头**：选 **`omni12_i2v`**（单镜控制在 6–8 秒，避免超过 10 秒触发翻倍费率）。
4. **单镜单主运镜 + 防幻觉乱说话护盾**：
   - 每镜只写 **1 个清晰主运镜**（如 slow push-in / gentle tracking / macro tilt），禁止一镜堆叠冲突运镜；
   - 凡**无对白镜头**（`keep_flow_sfx` / `mute_for_local_vo`），英文视频提示词末尾统一附纯环境音护盾 `Audio: Ambient sound only, crisp realistic environmental SFX, strictly no spoken dialogue and no background music.`；
   - 凡**有短对白镜头**，台词 ≤18 字，写清先画面后开口；避免要求画面渲染复杂长串中文字牌。
5. **免费 1080p + 云端 4K HDR 省分闭环**：
   - 视频生成端一律优先导 **免费 1080p**（`1080p_free`），不把高额积分浪费在单镜平台内 4K 超分；
   - 全片三轨混音与 4K/30fps HLG HDR 增强交由本地封装或 Google Colab 云端 GPU 统一完成。""",
    },
    "script_critic": {
        "title": "多角色编辑部 · 毒舌审稿主编规则（双轮打磨防平庸）",
        "description": "在编剧写完初稿后，由毒舌审稿主编从 0-3s 钩子、去套话、AI 可拍性、传播共鸣四把尺子挑刺，逼出更硬核的二稿。",
        "content": """你是挑剔且直击要害的爆款微短剧「毒舌审稿主编」，专门枪毙平庸的 AI 初稿剧本。
你的职责不是客套夸奖，而是用四把硬尺子逐条挑刺，给出**具体到秒数、画面与台词的必改指令**：

1. **0-3 秒生死线核查**：第一帧有没有让人手指立刻停住的强视觉奇观、悬念或反常识困境？如果开头还在慢节奏铺背景或念套话开场白，立刻打回重写。
2. **去套话/去说教核查**：检查有没有空洞口号、生硬抒情或流水账旁白。真正的高级感必须藏在主角吃瘪→较劲→反转的具体动作与细节里。
3. **AI 视频真实可拍性核查（防抽卡翻车）**：
   - 检查是否存在 >5 秒的真人正脸站桩长对话（必须改成动作/背影/器物特写/3D 搭档视角或拆短句）；
   - 检查单集主场景是否超过 3 个、单句台词是否超过 18 字（过长易导致口型漂移）；
   - 检查是否合理分配了省分空镜（`veo31_lite`）与核心表现镜（`omni12_i2v`）。
4. **双尾钩与传播欲核查**：结尾有没有干净利落的情绪落点？评论区有没有自然抛出的讨论/投票/找彩蛋话头，让观众愿意留言和转发？

请直接指出 3–5 条最致命硬伤，并给出可立刻落地的逐条修改指令（写明“把第 X 秒改成什么具体画面/动作/台词”，严禁空泛建议）。""",
    },
    "multi_platform_copy": {
        "title": "四平台差异化发布文案适配规则（视频号/抖音/小红书/B站）",
        "description": "纯本地生成四平台差异化标题、正文简介、话题标签、封面花字与首评置顶诱饵，人工审核一键复制发布，0 自动化封号风险。",
        "content": """你是精通跨平台短视频增长与分发算法的运营总监。
你需要根据同一集短剧的题材定位、剧本内容与核心亮点，为四个主流平台分别量身定制**绝不重样、贴合各自推荐逻辑**的发布文案包（供创作者人工审核复制发布）：

1. **微信视频号（核心：社交转发体面度 + 情绪共鸣）**：
   - 标题克制高级、有余韵，像电影短片名而非浮夸标题党；
   - 正文 40–80 字，给熟人转发社交圈留一个自然有品味的推荐理由；
   - 话题标签 3–5 个（紧扣题材与核心亮点）。
2. **抖音（核心：前 3 秒悬念冲突 + 完播率 + 评论区互动）**：
   - 标题短平快、带反差或悬念提问（15–24 字内）；
   - 正文简洁带网感，配 1 条适合置顶的自然互动神评（友好站队、细节彩蛋或开放提问）；
   - 话题标签 4–6 个（兼顾垂类词与泛流量热词）。
3. **小红书（核心：高颜值首帧审美 + 干货/彩蛋收藏欲）**：
   - 标题带克制 Emoji 与审美/细节吸睛点（20 字内）；
   - 正文分段清晰、带 1–2 个幕后巧思或小知识彩蛋，激发点赞收藏；
   - 话题标签 5–8 个。
4. **B站（核心：故事感 + 有梗有趣 + 制作诚意共鸣）**：
   - 标题有故事感或反差幽默，突出 AI 视觉创作诚意；
   - 简介交代剧情看点、创作小彩蛋与评论区互动暗号；
   - 话题标签 4–6 个。

严禁低俗标题党、严禁负能量引战、严禁编造虚假数据。""",
    },
    "comment_reply": {
        "title": "半自动评论高情商安全拟稿规则",
        "description": "为各平台真实观众评论拟定 3 种不同风格的回复草稿，并自动发掘评论中隐藏的下一集选题灵感（纯人工复制发布）。",
        "content": """你是短视频创作团队的运营助手，负责为真实观众评论起草高情商回复草稿（供创作者审核后手动复制发布，绝不自动发评）。

【硬原则】
1. 真实自然、有活人感（拒绝客服腔与机械模板），同时保持体面友善（不引战、不攻击任何个人或机构）。
2. 每条评论提供 3 种不同风格草稿，每条控制在 15-45 字：
   - 风格 A（大方走心/温暖致谢）：适合夸赞、共鸣与认真长评；
   - 风格 B（幽默接梗/轻松互动）：适合玩梗、吐槽细节或热聊剧情的观众；
   - 风格 C（追更引导/下集共创）：自然引导关注或在评论区投票决定下一集剧情走向。
3. 同时判断这条评论是否隐藏“下一集好选题”（如观众点名想看某个剧情分支、道具、场景或冷知识），若是则提炼出一句话选题建议。""",
    },
    "analytics_review": {
        "title": "多平台数据复盘与下集优化顾问",
        "description": "结合视频号转发、抖音完播互动、小红书收藏与 B站反馈，输出下一集可直接落地的改稿与提效建议。",
        "content": """你是精通视频号（社交转发）、抖音（前3秒停留与完播）、小红书（封面审美与收藏）与 B站（故事深度与互动）的短视频增长顾问。
请根据输入的单集或跨集多平台真实表现数据，给出犀利、不说套话、可直接执行的复盘结论：
1. 诊断核心瓶颈（是前 3 秒没钩住、中段节奏掉速、封面点击弱，还是缺少转发理由/评论区话头）；
2. 明确指出下一集必须保留的高光亮点与必须砍掉的拖沓段落；
3. 给出下一集前 3 秒开局钩子与结尾评论区互动提问的具体改写示例。""",
    },
}


# 仅当 projects 表为空时注入 1 个纯中立演示骨架，帮助新用户 1 分钟直观理解全流程结构
SEED_PROJECTS: list[dict[str, Any]] = [
    {
        "name": "示例剧目：3D 虚拟搭档微短剧",
        "slug": "demo-companion-drama",
        "track": "奇幻轻喜剧（通用示例）",
        "visual_mode": "电影级写实实景 + 3D 精致原创虚拟搭档",
        "aspect_ratio": "9:16",
        "target_duration_sec": 70,
        "global_style_prompt": (
            "Cinematic vertical 9:16 live-action film, warm golden-hour and crisp twilight atmospheric lighting, "
            "photorealistic real-world architecture and tactile materials seamlessly blended with a stylized 3D collectible companion, "
            "matching color grade, grounded contact shadows, natural ambient occlusion, subtle 35mm film grain, ARRI Alexa look"
        ),
        "description": (
            "开箱通用示例骨架：采用「电影级实景 × 固定 3D 原创搭档」路线，天然规避真人长对话恐怖谷并沉淀跨集辨识度。"
            "你可以直接编辑本剧目设定，或新建剧情、科幻、萌宠、美食、旅行、科普等任意题材剧目。"
        ),
        "season_outline": json.dumps(
            [
                {"ep": 1, "title": "（示例占位）第 1 集：意外闯入的微小搭档", "hook": "前3秒反常识视觉奇观引发悬念", "status": "planned"},
                {"ep": 2, "title": "（示例占位）第 2 集：消失的关键物件", "hook": "微距细节触发限时解谜困境", "status": "planned"},
                {"ep": 3, "title": "（示例占位）第 3 集：意料之外的反转答卷", "hook": "首尾道具呼应完成情绪升华与下季钩子", "status": "planned"},
            ],
            ensure_ascii=False,
        ),
        "continuity_ledger": json.dumps(
            {
                "main_characters": [],
                "recurring_locations": [],
                "open_cliffhangers": [],
                "notes": "开拍前可在此登记固定主角外观锚点、口头禅、防穿帮禁忌与主线伏笔；写下一集剧本时自动带入 AI 上下文防止吃书。",
            },
            ensure_ascii=False,
        ),
    },
]


def init_db() -> None:
    """初始化全套数据表、执行幂等迁移并注入通用示例骨架与可编辑规则模板"""
    with db_ctx() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompt_templates (
                key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                track TEXT NOT NULL DEFAULT '剧情微短剧',
                visual_mode TEXT NOT NULL DEFAULT '电影级写实实景 + 3D 虚拟搭档',
                aspect_ratio TEXT NOT NULL DEFAULT '9:16',
                target_duration_sec INTEGER NOT NULL DEFAULT 70,
                global_style_prompt TEXT DEFAULT '',
                description TEXT DEFAULT '',
                season_outline TEXT DEFAULT '[]',
                continuity_ledger TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hot_cache (
                source TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                source_url TEXT DEFAULT '',
                hot_value INTEGER DEFAULT 0,
                video_count INTEGER DEFAULT 0,
                summary TEXT DEFAULT '',
                scores_json TEXT DEFAULT '{}',
                score_theme INTEGER DEFAULT 0,
                total_score INTEGER DEFAULT 0,
                grade TEXT DEFAULT 'UNSCORED',
                veto INTEGER DEFAULT 0,
                veto_reason TEXT DEFAULT '',
                ai_pitch TEXT DEFAULT '',
                recommended_project_id INTEGER,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'character',
                name TEXT NOT NULL,
                flow_tag TEXT NOT NULL DEFAULT '',
                raw_image_path TEXT DEFAULT '',
                clean_image_path TEXT DEFAULT '',
                en_anchor_prompt TEXT DEFAULT '',
                negative_guard_en TEXT NOT NULL DEFAULT '',
                voice_mode TEXT NOT NULL DEFAULT 'flow_sfx',
                voice_en_desc TEXT DEFAULT '',
                voice_clone_sample_path TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                ep_number INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                topic_id INTEGER,
                hook_0_3s TEXT DEFAULT '',
                ending_cta TEXT DEFAULT '',
                next_ep_hook TEXT DEFAULT '',
                script_text TEXT DEFAULT '',
                continuity_summary TEXT DEFAULT '',
                bgm_prompt TEXT DEFAULT '',
                bgm_file_path TEXT DEFAULT '',
                bgm_volume REAL DEFAULT 0.25,
                status TEXT NOT NULL DEFAULT 'draft',
                latest_output_path TEXT DEFAULT '',
                web_preview_path TEXT NOT NULL DEFAULT '',
                sync_updated_at TEXT NOT NULL DEFAULT '',
                frames_sheet_path TEXT NOT NULL DEFAULT '',
                covers_sheet_path TEXT NOT NULL DEFAULT '',
                qc_overview_path TEXT NOT NULL DEFAULT '',
                ai_proof_path TEXT NOT NULL DEFAULT '',
                vo_mode TEXT NOT NULL DEFAULT 'pending',
                cover_candidates_json TEXT NOT NULL DEFAULT '[]',
                selected_cover_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                shot_no INTEGER NOT NULL DEFAULT 1,
                shot_title TEXT DEFAULT '',
                camera_type TEXT DEFAULT '中景',
                duration_sec INTEGER NOT NULL DEFAULT 6,
                flow_mode TEXT NOT NULL DEFAULT 'omni12_i2v',
                ref_asset_ids TEXT DEFAULT '[]',
                cn_action_desc TEXT DEFAULT '',
                cn_dialogue TEXT DEFAULT '',
                subtitle_text TEXT DEFAULT '',
                frame_prompt_en TEXT DEFAULT '',
                end_frame_prompt_en TEXT DEFAULT '',
                video_prompt_en TEXT DEFAULT '',
                start_frame_path TEXT DEFAULT '',
                end_frame_path TEXT DEFAULT '',
                audio_strategy TEXT NOT NULL DEFAULT 'keep_flow_sfx',
                upscale_target TEXT NOT NULL DEFAULT '1080p_free',
                upscale_done INTEGER NOT NULL DEFAULT 0,
                trim_start REAL DEFAULT 0.0,
                trim_end REAL DEFAULT 0.0,
                sort_order INTEGER NOT NULL DEFAULT 1,
                vo_file_path TEXT NOT NULL DEFAULT '',
                vo_source TEXT NOT NULL DEFAULT '',
                qc_strip_path TEXT NOT NULL DEFAULT '',
                qc_report_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS takes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shot_id INTEGER NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'video',
                file_path TEXT NOT NULL,
                original_filename TEXT DEFAULT '',
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                duration_sec REAL DEFAULT 0.0,
                resolution_label TEXT DEFAULT '',
                rating INTEGER DEFAULT 0,
                is_final INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (shot_id) REFERENCES shots(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS publish_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                episode_id INTEGER,
                platform TEXT NOT NULL DEFAULT '微信视频号',
                post_title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                views INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                favorites INTEGER DEFAULT 0,
                completion_rate REAL DEFAULT 0.0,
                moments_feedback TEXT DEFAULT '',
                ai_review TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comment_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publish_log_id INTEGER,
                episode_id INTEGER,
                platform TEXT DEFAULT '抖音',
                fan_comment TEXT NOT NULL,
                tone TEXT DEFAULT 'balanced',
                drafts_json TEXT DEFAULT '[]',
                topic_idea TEXT DEFAULT '',
                converted_topic_id INTEGER,
                status TEXT NOT NULL DEFAULT 'draft',
                replied_text TEXT NOT NULL DEFAULT '',
                replied_by TEXT NOT NULL DEFAULT '',
                replied_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'partner',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                episode_id INTEGER,
                field_name TEXT NOT NULL,
                old_value TEXT NOT NULL DEFAULT '',
                new_value TEXT NOT NULL DEFAULT '',
                actor_username TEXT NOT NULL DEFAULT '',
                actor_name TEXT NOT NULL DEFAULT '',
                actor_role TEXT NOT NULL DEFAULT 'admin',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rule_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_key TEXT NOT NULL,
                rule_title TEXT NOT NULL DEFAULT '',
                suggestion_text TEXT NOT NULL,
                actor_username TEXT NOT NULL DEFAULT '',
                actor_name TEXT NOT NULL DEFAULT '',
                actor_role TEXT NOT NULL DEFAULT 'partner',
                status TEXT NOT NULL DEFAULT 'open',
                admin_reply TEXT NOT NULL DEFAULT '',
                handled_by TEXT NOT NULL DEFAULT '',
                handled_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS review_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_key TEXT NOT NULL DEFAULT '',
                choice TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT 'comment',
                comment TEXT NOT NULL DEFAULT '',
                actor_username TEXT NOT NULL DEFAULT '',
                actor_name TEXT NOT NULL DEFAULT '',
                actor_role TEXT NOT NULL DEFAULT 'partner',
                created_at TEXT NOT NULL,
                FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_revisions_episode ON revisions(episode_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_revisions_created ON revisions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_review_votes_episode ON review_votes(episode_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_rule_suggestions_key ON rule_suggestions(template_key, id DESC);

            CREATE TABLE IF NOT EXISTS render_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                job_type TEXT NOT NULL,
                target_host TEXT DEFAULT 'local',
                status TEXT NOT NULL DEFAULT 'pending',
                output_path TEXT DEFAULT '',
                log_text TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS api_usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                action TEXT DEFAULT 'generate',
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                est_cost_usd REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quota_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                gemini_daily_limit INTEGER NOT NULL DEFAULT 100,
                gemini_monthly_credit_usd REAL NOT NULL DEFAULT 0.0,
                flow_monthly_total INTEGER NOT NULL DEFAULT 0,
                flow_remaining INTEGER NOT NULL DEFAULT 0,
                flow_month TEXT NOT NULL DEFAULT '',
                flow_calibrated_at TEXT DEFAULT '',
                flow_estimated_spent_since_cal INTEGER NOT NULL DEFAULT 0,
                deepseek_balance_text TEXT DEFAULT '',
                deepseek_checked_at TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS platform_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT '抖音',
                title TEXT NOT NULL,
                hashtags_json TEXT DEFAULT '[]',
                deadline TEXT DEFAULT '',
                incentive TEXT DEFAULT '',
                angle_pitch TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                batch_date TEXT DEFAULT '',
                converted_topic_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, title)
            );
            """
        )

        # 幂等平滑迁移：为已有数据库补齐新增列（严禁清库或覆盖已有数据）
        topic_cols = {r["name"] for r in conn.execute("PRAGMA table_info(topics)").fetchall()}
        if "score_theme" not in topic_cols:
            conn.execute("ALTER TABLE topics ADD COLUMN score_theme INTEGER DEFAULT 0")

        campaign_cols = {r["name"] for r in conn.execute("PRAGMA table_info(platform_campaigns)").fetchall()}
        if "angle_pitch" not in campaign_cols:
            conn.execute("ALTER TABLE platform_campaigns ADD COLUMN angle_pitch TEXT DEFAULT ''")

        asset_cols = {r["name"] for r in conn.execute("PRAGMA table_info(assets)").fetchall()}
        if "negative_guard_en" not in asset_cols:
            conn.execute("ALTER TABLE assets ADD COLUMN negative_guard_en TEXT NOT NULL DEFAULT ''")

        shot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(shots)").fetchall()}
        if "end_frame_prompt_en" not in shot_cols:
            conn.execute("ALTER TABLE shots ADD COLUMN end_frame_prompt_en TEXT DEFAULT ''")
        if "vo_file_path" not in shot_cols:
            conn.execute("ALTER TABLE shots ADD COLUMN vo_file_path TEXT NOT NULL DEFAULT ''")
        if "vo_source" not in shot_cols:
            conn.execute("ALTER TABLE shots ADD COLUMN vo_source TEXT NOT NULL DEFAULT ''")
        if "qc_strip_path" not in shot_cols:
            conn.execute("ALTER TABLE shots ADD COLUMN qc_strip_path TEXT NOT NULL DEFAULT ''")
        if "qc_report_json" not in shot_cols:
            conn.execute("ALTER TABLE shots ADD COLUMN qc_report_json TEXT NOT NULL DEFAULT '{}'")

        ep_cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        if "vo_mode" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN vo_mode TEXT NOT NULL DEFAULT 'pending'")
        if "cover_candidates_json" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN cover_candidates_json TEXT NOT NULL DEFAULT '[]'")
        if "selected_cover_path" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN selected_cover_path TEXT NOT NULL DEFAULT ''")
        if "web_preview_path" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN web_preview_path TEXT NOT NULL DEFAULT ''")
        if "sync_updated_at" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN sync_updated_at TEXT NOT NULL DEFAULT ''")
        if "frames_sheet_path" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN frames_sheet_path TEXT NOT NULL DEFAULT ''")
        if "covers_sheet_path" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN covers_sheet_path TEXT NOT NULL DEFAULT ''")
        if "qc_overview_path" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN qc_overview_path TEXT NOT NULL DEFAULT ''")
        if "ai_proof_path" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN ai_proof_path TEXT NOT NULL DEFAULT ''")

        comment_cols = {r["name"] for r in conn.execute("PRAGMA table_info(comment_drafts)").fetchall()}
        if "status" not in comment_cols:
            conn.execute("ALTER TABLE comment_drafts ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")
        if "replied_text" not in comment_cols:
            conn.execute("ALTER TABLE comment_drafts ADD COLUMN replied_text TEXT NOT NULL DEFAULT ''")
        if "replied_by" not in comment_cols:
            conn.execute("ALTER TABLE comment_drafts ADD COLUMN replied_by TEXT NOT NULL DEFAULT ''")
        if "replied_at" not in comment_cols:
            conn.execute("ALTER TABLE comment_drafts ADD COLUMN replied_at TEXT NOT NULL DEFAULT ''")

        usage_cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_usage_logs)").fetchall()}
        if "est_cost_usd" not in usage_cols:
            conn.execute("ALTER TABLE api_usage_logs ADD COLUMN est_cost_usd REAL NOT NULL DEFAULT 0.0")

        quota_cols = {r["name"] for r in conn.execute("PRAGMA table_info(quota_state)").fetchall()}
        if "gemini_monthly_credit_usd" not in quota_cols:
            conn.execute("ALTER TABLE quota_state ADD COLUMN gemini_monthly_credit_usd REAL NOT NULL DEFAULT 0.0")

        # 初始化单行安全保守额度状态（用户首次选定订阅档位后自动校准写入真实配额）
        cur_month = datetime.now().strftime("%Y-%m")
        ts = now_str()
        conn.execute(
            """
            INSERT OR IGNORE INTO quota_state (
                id, gemini_daily_limit, gemini_monthly_credit_usd, flow_monthly_total, flow_remaining,
                flow_month, flow_calibrated_at, flow_estimated_spent_since_cal, updated_at
            ) VALUES (1, 100, 0.0, 0, 0, ?, '', 0, ?)
            """,
            (cur_month, ts),
        )

        # 安全同步默认规则模板（仅插入新键或平滑升级未改写旧版，绝不覆盖用户自定义规则）
        sync_default_prompt_templates(conn, ts=ts)

        # 仅当项目表为空时注入 1 个纯中立演示剧目骨架
        count = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
        if count == 0:
            for p in SEED_PROJECTS:
                conn.execute(
                    """
                    INSERT INTO projects (
                        name, slug, track, visual_mode, aspect_ratio, target_duration_sec,
                        global_style_prompt, description, season_outline, continuity_ledger,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p["name"],
                        p["slug"],
                        p["track"],
                        p["visual_mode"],
                        p["aspect_ratio"],
                        p["target_duration_sec"],
                        p["global_style_prompt"],
                        p["description"],
                        p["season_outline"],
                        p["continuity_ledger"],
                        ts,
                        ts,
                    ),
                )

        # 幂等按环境变量初始化管理员与协作成员账号（绝不破坏已有数据）
        from .auth import bootstrap_users_from_env

        bootstrap_users_from_env(conn)


def sync_default_prompt_templates(
    conn: sqlite3.Connection | None = None,
    *,
    force_keys: list[str] | None = None,
    ts: str | None = None,
) -> dict[str, list[str]]:
    """
    安全同步默认 Prompt 规则模板（绝不粗暴覆盖用户已自定义的模板正文）：
    - 新模板（库中不存在）：直接插入；
    - 历史默认模板：仅当正文 SHA-256 命中已知历史默认指纹时平滑升级；
    - 仅当调用方显式传入 `force_keys` 时，才强制重置指定键为最新默认版。
    """
    now = ts or now_str()
    force_set = set(force_keys or [])
    inserted: list[str] = []
    upgraded: list[str] = []
    skipped_custom: list[str] = []

    def _run(c: sqlite3.Connection) -> None:
        for key, item in DEFAULT_PROMPT_TEMPLATES.items():
            row = c.execute("SELECT title, content FROM prompt_templates WHERE key = ?", (key,)).fetchone()
            if row is None:
                c.execute(
                    """
                    INSERT INTO prompt_templates (key, title, description, content, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, item["title"], item["description"], item["content"], now),
                )
                inserted.append(key)
                continue

            cur_content = row["content"] or ""
            cur_title = row["title"] or ""
            if cur_content == item["content"] and cur_title == item["title"]:
                continue

            legacy_hashes = LEGACY_PROMPT_TEMPLATE_SHA256.get(key, set())
            is_legacy_unmodified = _sha256_text(cur_content) in legacy_hashes

            if key in force_set or is_legacy_unmodified:
                c.execute(
                    """
                    UPDATE prompt_templates
                    SET title = ?, description = ?, content = ?, updated_at = ?
                    WHERE key = ?
                    """,
                    (item["title"], item["description"], item["content"], now, key),
                )
                upgraded.append(key)
            else:
                skipped_custom.append(key)

    if conn is not None:
        _run(conn)
    else:
        with db_ctx() as managed_conn:
            _run(managed_conn)

    return {"inserted": inserted, "upgraded": upgraded, "skipped_custom": skipped_custom}
