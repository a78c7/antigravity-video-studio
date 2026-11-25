"""
Google Flow 现役模型提示词编译器 & 积分预算器：
- 全面适配现役主力分层工作流：
  1. Nano Banana 2/Pro（生图/首帧定妆，不扣月度 Flow 视频积分，走独立日配额）
  2. Gemini Omni Flash 1.2（最长 15s，360p=1积分/秒、720p=3积分/秒，11-15s翻倍；支持 10 图参考）
  3. Veo 3.1 Lite（Ultra 5积分/条，Lower Priority 0积分；支持 3 图参考）
  4. Frames to Video（首尾帧插值）& Omni Extend（每次续写 10s）
  5. 1080p 超分（10s内 0 积分免费）；4K 超分固定 50 积分/条（自动黄牌省分预警）
"""

from __future__ import annotations

import json
import re
from typing import Any

FLOW_MODES: dict[str, dict[str, Any]] = {
    "nb2_frame": {
        "label": "Nano Banana 2/Pro 静帧定妆",
        "max_refs": 14,
        "desc": "不扣月度 Flow 积分（走独立每日出图配额），先合定首帧防跳变",
    },
    "omni12_i2v": {
        "label": "Omni Flash 1.2 图生视频 (主力)",
        "max_refs": 10,
        "desc": "主力视频模型：360p 抽卡 1 积分/秒、720p 3 积分/秒，最长 15 秒，支持声音资产",
    },
    "veo31_lite": {
        "label": "Veo 3.1 Lite (省积分/慢速0分)",
        "max_refs": 3,
        "desc": "4/6/8 秒 720p 基线，Ultra 仅 5 积分/条，Lower Priority 甚至 0 积分",
    },
    "frames_to_video": {
        "label": "首尾双帧插值 (Frames to Video)",
        "max_refs": 2,
        "desc": "锁定首帧与尾帧平滑过渡（主体构图勿跃迁过大）",
    },
    "omni_extend": {
        "label": "Omni Extend 续写 (+10秒)",
        "max_refs": 10,
        "desc": "基于上一镜尾帧续写动态（连续续写不超过 3 次防曝光漂移）",
    },
}

AUDIO_STRATEGIES: dict[str, str] = {
    "keep_flow_sfx": "保留 Flow 原生环境音效（禁 BGM）",
    "keep_flow_dialogue": "保留 Flow 原生中文短台词 + 环境音（禁 BGM）",
    "mute_for_local_vo": "仅取画面（后期使用 TTS / 角色配音轨统一合成）",
}


def _format_aspect_phrase(aspect: str) -> str:
    """将画幅比例转为 Google Flow 智能体可准确解析的方向+比例短语"""
    a = (aspect or "9:16").strip()
    if a in ("9:16", "3:4", "2:3", "4:5"):
        return f"vertical {a}"
    if a in ("16:9", "4:3", "3:2", "21:9"):
        return f"horizontal {a}"
    if a == "1:1":
        return "square 1:1"
    return a


def compile_shot_prompts(
    shot: dict[str, Any],
    project: dict[str, Any],
    assets_map: dict[int, dict[str, Any]],
    image_count: int = 4,
    image_model: str = "Nano Banana 2.1 Pro",
    video_count: int = 2,
) -> dict[str, Any]:
    """根据全局画风、选中资产英文锁词、中文台词、音频策略及显式 Flow 模型/张数指令，编译 Flow 专用与 CLI 纯净英文提示词"""
    flow_mode = shot.get("flow_mode") or "omni12_i2v"
    mode_meta = FLOW_MODES.get(flow_mode, FLOW_MODES["omni12_i2v"])
    max_refs = mode_meta["max_refs"]

    img_cnt = max(1, min(8, int(image_count or 4)))
    vid_cnt = max(1, min(4, int(video_count or 2)))
    img_model = (image_model or "Nano Banana 2.1 Pro").strip() or "Nano Banana 2.1 Pro"
    img_noun = "image" if img_cnt == 1 else "images"
    vid_noun = "video" if vid_cnt == 1 else "videos"

    raw_ref_ids = shot.get("ref_asset_ids") or "[]"
    if isinstance(raw_ref_ids, str):
        try:
            ref_ids = [int(x) for x in json.loads(raw_ref_ids)]
        except Exception:
            ref_ids = []
    else:
        ref_ids = [int(x) for x in raw_ref_ids]

    ref_assets = [assets_map[aid] for aid in ref_ids if aid in assets_map]
    warnings: list[str] = []
    if len(ref_assets) > max_refs:
        warnings.append(
            f"当前引用了 {len(ref_assets)} 个资产，超过 {mode_meta['label']} 上限（≤{max_refs}张），建议先在 Nano Banana 合成首帧再出视频。"
        )

    global_style = (project.get("global_style_prompt") or "").strip()
    aspect = project.get("aspect_ratio") or "9:16"
    aspect_phrase = _format_aspect_phrase(aspect)
    asset_anchors = [
        f"{a.get('flow_tag') or a['name']}: {a['en_anchor_prompt']}"
        for a in ref_assets
        if (a.get("en_anchor_prompt") or "").strip()
    ]
    anchor_block = "; ".join(asset_anchors)

    # 读取绑定资产的永久防穿帮负向护盾（negative_guard_en），按顺序去重并强制拼入全套编译词
    active_guards: list[str] = []
    seen_guard_keys: set[str] = set()
    for a in ref_assets:
        raw_guard = (a.get("negative_guard_en") or "").strip()
        if not raw_guard:
            continue
        cleaned_guard = re.sub(r"\s+", " ", raw_guard).strip(" ;.")
        if not cleaned_guard:
            continue
        norm_key = cleaned_guard.lower()
        if norm_key in seen_guard_keys:
            continue
        seen_guard_keys.add(norm_key)
        active_guards.append(cleaned_guard)
    guard_block = f"MANDATORY VISUAL CONSTRAINTS: {'; '.join(active_guards)}." if active_guards else ""

    # 1. 静态首帧 Prompt（开头显式声明模型 + 方向画幅 + 生成张数）
    frame_header = f"Generate {img_cnt} {aspect_phrase} {img_noun} using {img_model}."
    base_frame = (shot.get("frame_prompt_en") or "").strip()
    if not base_frame:
        cn_action = (shot.get("cn_action_desc") or "").strip()
        cam = shot.get("camera_type") or "Medium shot"
        base_frame = f"{cam} composition, {cn_action}" if cn_action else f"{cam} cinematic still frame"

    frame_parts = [p for p in [frame_header, base_frame, anchor_block, global_style, guard_block] if p]
    frame_parts.append("Clean composition, no garbled Chinese text, no watermarks, crisp cinematic keyframe.")
    compiled_frame_prompt = " ".join(frame_parts)

    # 纯净 CLI 生图词（剥离 Flow 智能体开头指令句，防止与 CLI 参数冲突）
    cli_frame_parts = [p for p in [base_frame, anchor_block, global_style, guard_block] if p]
    cli_frame_parts.append("Clean composition, no garbled Chinese text, no watermarks, crisp cinematic keyframe.")
    cli_frame_prompt = " ".join(cli_frame_parts)

    # 1b. 静态尾帧 Prompt（用于 Frames to Video 首尾双帧插值）
    base_end_frame = (shot.get("end_frame_prompt_en") or "").strip()
    if base_end_frame:
        end_frame_parts = [p for p in [frame_header, base_end_frame, anchor_block, global_style, guard_block] if p]
        end_frame_parts.append("Clean composition, no garbled Chinese text, no watermarks, crisp cinematic end keyframe.")
        compiled_end_frame_prompt = " ".join(end_frame_parts)
        cli_end_parts = [p for p in [base_end_frame, anchor_block, global_style, guard_block] if p]
        cli_end_parts.append("Clean composition, no garbled Chinese text, no watermarks, crisp cinematic end keyframe.")
        cli_end_frame_prompt = " ".join(cli_end_parts)
    else:
        compiled_end_frame_prompt = ""
        cli_end_frame_prompt = ""

    # 2. 动态视频 Prompt
    base_video = (shot.get("video_prompt_en") or "").strip()
    if not base_video:
        base_video = base_frame

    dialogue = (shot.get("cn_dialogue") or "").strip()
    audio_strategy = shot.get("audio_strategy") or "keep_flow_sfx"
    duration = int(shot.get("duration_sec") or 6)

    if flow_mode == "nb2_frame":
        flow_entry_guide = f"一镜一清：Flow 用 {img_model} 抽 {img_cnt} 张 → 定 1 冠军立刻删其余废图，下载 1 次归档（0 视频积分）"
        video_header = frame_header
    elif flow_mode == "frames_to_video":
        flow_entry_guide = (
            f"一镜一清（首尾双帧闭环）：①抽 {img_cnt} 首帧定 1 删废图 → ②拖首帧进框出 {img_cnt} 尾帧定 1 删废图"
            f" → ③原地转 Frames to Video（{aspect}·{duration}s·{vid_cnt}条）→ ④挑 1 条下「免费 1080p」"
        )
        video_header = f"Generate {vid_cnt} {aspect_phrase} {duration}s {vid_noun} using Frames to Video (first and last frame interpolation)."
    elif flow_mode == "veo31_lite":
        flow_entry_guide = (
            f"一镜一清：①抽 {img_cnt} 首帧定 1 冠军删废图 → ②直接拖冠军图进框转 Veo 3.1 Lite"
            f"（{aspect}·{duration}s·{vid_cnt}条，Ultra 仅 5 分/条）→ ③挑 1 条下「免费 1080p」"
        )
        video_header = f"Generate {vid_cnt} {aspect_phrase} {duration}s {vid_noun} using Veo 3.1 Lite."
    elif flow_mode == "omni_extend":
        flow_entry_guide = "一镜一清：基于上一镜定稿视频原地续写 Omni Extend（+10s，连续≤3次防曝光漂移）→ 挑最佳下「免费 1080p」"
        video_header = f"Extend this {aspect_phrase} video by 10s using Omni Extend."
    else:
        if dialogue and audio_strategy == "keep_flow_dialogue":
            flow_entry_guide = (
                f"一镜一清：①抽 {img_cnt} 首帧定 1 删废图 → ②拖进框 + 选 @角色卡/@Voice 转 Omni Flash 1.2"
                f"（{aspect}·720p·{duration}s·{vid_cnt}条，3分/秒）→ ③挑 1 条下「免费 1080p」"
            )
        else:
            flow_entry_guide = (
                f"一镜一清：①抽 {img_cnt} 首帧定 1 删废图 → ②拖冠军图进框转 Omni Flash 1.2"
                f"（{aspect}·{duration}s·{vid_cnt}条）→ ③挑 1 条下「免费 1080p」（全片 4K 留给 Colab GPU）"
            )
        video_header = f"Generate {vid_cnt} {aspect_phrase} {duration}s {vid_noun} using Omni Flash 1.2."

    video_parts = [p for p in [video_header, base_video, anchor_block] if p]
    if dialogue and audio_strategy == "keep_flow_dialogue":
        if len(dialogue) > 18:
            warnings.append("中文台词偏长（>18字），Flow 口型容易抢拍或漂移，建议压短到 15 字内或切后期旁白。")
        if flow_mode == "veo31_lite":
            warnings.append("本镜含对白：Veo 3.1 Lite 不支持挂载角色专属 @Voice 声线；若需固定声线请切为 Omni Flash 1.2「素材」模式。")
        video_parts.append(
            f'Let the visual action settle first. After a brief pause, the subject speaks naturally in Mandarin Chinese: "{dialogue}".'
        )
        video_parts.append(
            "Audio: clear natural Mandarin Chinese dialogue matching the scene acoustics, realistic ambient SFX, strictly no background music."
        )
    elif dialogue and audio_strategy == "mute_for_local_vo":
        video_parts.append("Expressive natural acting without speaking, mouths closed, designed for post voiceover.")
        video_parts.append(
            "Audio: Ambient sound only, crisp realistic environmental SFX, strictly no spoken dialogue and no background music."
        )
    else:
        # 防 Veo 3.1 Lite 在无对白镜头产生幻觉乱说话，强制附纯环境音护盾
        video_parts.append(
            "Audio: Ambient sound only, crisp realistic environmental SFX, strictly no spoken dialogue and no background music."
        )

    if global_style:
        video_parts.append(f"Consistent lighting & color grade: {global_style}.")
    if guard_block:
        video_parts.append(guard_block)

    if duration > 10 and flow_mode == "omni12_i2v":
        warnings.append("Omni Flash 1.2 超过 10 秒区间积分费率翻倍，建议压在 6-8 秒以内最省分。")

    compiled_video_prompt = " ".join(video_parts)
    cli_video_parts = [p for p in video_parts[1:]] if video_parts and video_parts[0] == video_header else list(video_parts)
    cli_video_prompt = " ".join(cli_video_parts)

    # 单镜局部精修快捷公式
    quick_edit_prompts: list[dict[str, str]] = [
        {
            "label": "去幻觉乱说话（纯环境音）",
            "prompt": "Keep the exact character, framing, camera move, and lighting. Use ambient sound only with realistic environmental SFX, no spoken dialogue and no background music.",
        },
        {
            "label": "加轻配乐衬底（不压原声）",
            "prompt": "Keep the exact scene, visuals, and pacing, add a subtle cinematic film score softly underneath the ambient sound.",
        },
    ]
    if dialogue:
        quick_edit_prompts.insert(
            0,
            {
                "label": "修对白抢拍（先动作后开口）",
                "prompt": f'Keep the framing, character, and visual motion. Let the main visual action finish first. After a brief pause, have the character say exactly "{dialogue}" in Mandarin Chinese in a calm, natural voice.',
            },
        )
        quick_edit_prompts.append(
            {
                "label": "加卡点定时字幕",
                "prompt": f'Keep the shot and its audio. Add one clean short subtitle at the bottom, exactly "{dialogue}", as the character speaks. Remove the subtitle before the shot ends.',
            }
        )

    ref_images = []
    for a in ref_assets:
        p = a.get("clean_image_path") or a.get("raw_image_path") or ""
        ref_images.append(
            {
                "asset_id": a["id"],
                "name": a["name"],
                "flow_tag": a.get("flow_tag") or f"@{a['name']}",
                "asset_type": a.get("asset_type"),
                "image_path": p,
                "negative_guard_en": (a.get("negative_guard_en") or "").strip(),
            }
        )

    return {
        "compiled_frame_prompt": compiled_frame_prompt,
        "compiled_end_frame_prompt": compiled_end_frame_prompt,
        "compiled_video_prompt": compiled_video_prompt,
        "cli_frame_prompt": cli_frame_prompt,
        "cli_end_frame_prompt": cli_end_frame_prompt,
        "cli_video_prompt": cli_video_prompt,
        "active_guards": active_guards,
        "image_count": img_cnt,
        "image_model": img_model,
        "video_count": vid_cnt,
        "flow_entry_guide": flow_entry_guide,
        "quick_edit_prompts": quick_edit_prompts,
        "max_refs": max_refs,
        "ref_images": ref_images,
        "warnings": warnings,
    }


def estimate_episode_credits(shots: list[dict[str, Any]], draft_takes_per_shot: int = 2) -> dict[str, Any]:
    """
    估算单集 Google Flow 积分消耗：
    - Nano Banana 首帧：0 积分（仅记每日出图张数）
    - Omni Flash 1.2：360p 抽卡 1积分/秒、720p 定稿 3积分/秒（>10s 翻倍）
    - Veo 3.1 Lite：5 积分/条（Lower Priority 可为 0）
    - 1080p 超分（≤10s）：0 积分免费；4K 超分：50 积分/条（>10s +5/s）
    """
    nb_daily_images = 0
    draft_credits = 0
    final_720p_credits = 0
    upscale_credits = 0
    four_k_count = 0

    for s in shots:
        dur = max(4, min(15, int(s.get("duration_sec") or 6)))
        mode = s.get("flow_mode") or "omni12_i2v"
        upscale = s.get("upscale_target") or "1080p_free"

        nb_daily_images += 4

        if mode == "veo31_lite":
            draft_credits += 5 * draft_takes_per_shot
            final_720p_credits += 5
        elif mode == "nb2_frame":
            pass
        else:
            mult = 2 if dur > 10 else 1
            draft_credits += dur * 1 * mult * draft_takes_per_shot
            final_720p_credits += dur * 3 * mult

        if upscale == "4k_hero":
            four_k_count += 1
            extra = max(0, dur - 10) * 5
            upscale_credits += 50 + extra
        elif dur > 10:
            upscale_credits += dur - 10

    total = draft_credits + final_720p_credits + upscale_credits
    alerts: list[str] = []
    if four_k_count > 0:
        alerts.append(
            f"省分提醒：本集有 {four_k_count} 个镜头标了 Flow 4K 超分（额外烧 {upscale_credits} 积分）。"
            "建议在 Flow 仅导出 0 积分免费 1080p，把全片 4K 超分交给 Google Colab 云端 GPU 通道统一跑，立省全部超分积分！"
        )

    return {
        "shot_count": len(shots),
        "total_duration_sec": sum(int(s.get("duration_sec") or 6) for s in shots),
        "nb_daily_images": nb_daily_images,
        "draft_credits": draft_credits,
        "final_720p_credits": final_720p_credits,
        "upscale_credits": upscale_credits,
        "total_estimated_credits": total,
        "four_k_count": four_k_count,
        "alerts": alerts,
    }
