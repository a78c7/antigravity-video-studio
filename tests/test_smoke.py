"""
反重力视频工坊（Antigravity Video Studio）v1.0.1 自动化冒烟与核心回归测试套件。

设计原则：
- 零副作用：所有测试严格使用 pytest `tmp_path` 作为隔离的 `STUDIO_DATA_DIR`、`.env`、`.secrets.env` 与下载目录，
  绝不读写仓库真实 `data/`、项目根 `.env`/`.secrets.env`、系统钥匙串或用户真实下载目录；
- 覆盖 v1.0.1 全部核心契约与历史回归缺陷，可在 CI（Linux / macOS）与本地快速稳定执行。
"""

from __future__ import annotations

import inspect
import json
import math
import shutil
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image


def _write_test_png(path: Path, size: tuple[int, int] = (64, 114), color: tuple[int, int, int] = (32, 96, 168)) -> Path:
    """生成轻量合法测试 PNG 图片"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return path


def _write_test_wav(path: Path, duration_sec: float, *, sample_rate: int = 24000, freq_hz: float = 440.0) -> Path:
    """使用标准库 wave 生成指定精确时长的 16-bit 单声道 WAV 测试音频（零外部依赖）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(round(duration_sec * sample_rate)))
    amplitude = 8000
    frames = bytearray()
    for i in range(n_frames):
        val = int(amplitude * math.sin(2.0 * math.pi * freq_hz * (i / sample_rate)))
        frames.extend(struct.pack("<h", val))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return path


def _write_test_mp4(path: Path, duration_sec: float = 2.0, *, width: int = 360, height: int = 640) -> Path:
    """使用本机 ffmpeg 生成轻量静音竖屏测试 MP4"""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x1e293b:s={width}x{height}:d={duration_sec:.2f}:r=30",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=stereo:d={duration_sec:.2f}",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0)
    assert res.returncode == 0 and path.exists(), f"生成测试视频失败: {res.stderr}"
    return path


@pytest.fixture()
def studio_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """构造完全隔离于主仓库与宿主系统的临时制片台运行环境"""
    data_dir = (tmp_path / "data").resolve()
    media_dir = data_dir / "media"
    exports_dir = data_dir / "exports"
    downloads_dir = (tmp_path / "downloads").resolve()
    dock_dir = (tmp_path / "dock").resolve()
    env_path = (tmp_path / ".env").resolve()
    secrets_path = (tmp_path / ".secrets.env").resolve()

    for d in (data_dir, media_dir, exports_dir, downloads_dir, dock_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 清除可能污染测试结果的宿主环境变量
    for key in (
        "APP_MODE",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "SYNC_API_TOKEN",
        "SESSION_SECRET",
        "MEDIA_ENGINE_MODE",
        "SUBSCRIPTION_TIER",
        "CLOUD_STUDIO_URL",
        "GOOGLE_PROXY_URL",
        "ENABLE_HOT_FEEDS",
        "ENABLE_CLOUD_COLLAB",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("STUDIO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("APP_MODE", "local_producer")
    monkeypatch.setenv("WATCH_DOWNLOADS_DIR", str(downloads_dir))
    monkeypatch.setenv("DOCK_DRAG_DIR", str(dock_dir))

    # 延迟导入后端模块，确保首次加载即落向临时 STUDIO_DATA_DIR
    import backend.auth as auth_mod
    import backend.config as config_mod
    import backend.db as db_mod
    import backend.flow_compiler as fc_mod
    import backend.jobs as jobs_mod
    import backend.main as main_mod
    import backend.media_ops as media_ops_mod
    import backend.routers.analytics as analytics_mod
    import backend.routers.collab as collab_mod
    import backend.routers.delivery as delivery_mod
    import backend.routers.studio as studio_mod
    import backend.routers.topics as topics_mod
    import backend.watcher as watcher_mod

    db_path = data_dir / "studio.db"

    # 统一将所有模块级路径绑定重指向 tmp_path，确保 0 副作用
    monkeypatch.setattr(config_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(config_mod, "SECRETS_ENV_PATH", secrets_path)
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(config_mod, "DB_PATH", db_path)
    monkeypatch.setattr(config_mod, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(config_mod, "EXPORTS_DIR", exports_dir)
    # 严禁测试读写宿主机 macOS Keychain
    monkeypatch.setattr(config_mod, "_keychain_get", lambda _account: "")
    monkeypatch.setattr(config_mod, "_keychain_set", lambda _account, _value: False)

    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(auth_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(media_ops_mod, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(media_ops_mod, "EXPORTS_DIR", exports_dir)
    monkeypatch.setattr(main_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(watcher_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(studio_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(studio_mod, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(delivery_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(delivery_mod, "MEDIA_DIR", media_dir)
    monkeypatch.setattr(topics_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(topics_mod, "CAMPAIGNS_INBOX_FILE", data_dir / "campaigns_inbox.json")
    monkeypatch.setattr(collab_mod, "DATA_DIR", data_dir)

    # 重置健康检查缓存，避免跨测试残留
    if hasattr(main_mod, "_health_env_cache") and isinstance(main_mod._health_env_cache, dict):
        main_mod._health_env_cache["ts"] = 0.0
        main_mod._health_env_cache["data_dir"] = ""
        main_mod._health_env_cache["values"] = None

    # 避免测试启动真实 watchdog 监听宿主下载目录
    if main_mod.downloads_inbox is not None:
        monkeypatch.setattr(main_mod.downloads_inbox, "start", lambda: None)
        monkeypatch.setattr(main_mod.downloads_inbox, "stop", lambda: None)
        monkeypatch.setattr(main_mod.downloads_inbox, "reload_filter", lambda: None)

    db_mod.init_db()

    with TestClient(main_mod.app) as client:
        yield SimpleNamespace(
            client=client,
            tmp_path=tmp_path,
            data_dir=data_dir,
            media_dir=media_dir,
            exports_dir=exports_dir,
            downloads_dir=downloads_dir,
            env_path=env_path,
            secrets_path=secrets_path,
            config=config_mod,
            db=db_mod,
            flow_compiler=fc_mod,
            jobs=jobs_mod,
            main=main_mod,
            media_ops=media_ops_mod,
            studio=studio_mod,
            delivery=delivery_mod,
            analytics=analytics_mod,
        )


def test_health_version_and_env_checks(studio_env) -> None:
    """校验 /api/health 返回 1.0.1 版本号与完整环境红绿灯体检字段"""
    resp = studio_env.client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert data["app"] == "antigravity-video-studio"
    assert data["version"] == "1.0.1"
    assert data["app_mode"] == "local_producer"
    assert studio_env.main.app.version == "1.0.1"

    for bool_key in ("ffmpeg_ok", "ffprobe_ok", "google_genai_sdk_ok", "gemini_cli_ok", "colab_cli_ok"):
        assert bool_key in data, f"/api/health 缺少字段: {bool_key}"
        assert isinstance(data[bool_key], bool)

    assert isinstance(data["disk_free_gb"], (int, float))
    assert data["disk_free_gb"] >= 0
    assert isinstance(data["path_warning"], str)


def test_empty_db_seeds_demo_project_assets_episode_and_shots(studio_env) -> None:
    """空库首次初始化自动生成示范剧目 + 3 个带护盾资产 + 1 示范集 + 6 示范分镜（且幂等）"""
    client = studio_env.client

    projs = client.get("/api/projects").json()["projects"]
    assert len(projs) == 1
    proj = projs[0]
    assert proj["slug"] == "demo-companion-drama"

    assets = client.get(f"/api/projects/{proj['id']}/assets").json()["assets"]
    assert len(assets) == 3
    for asset in assets:
        assert (asset.get("en_anchor_prompt") or "").strip(), f"示范资产缺少英文锚点: {asset['name']}"
        assert (asset.get("negative_guard_en") or "").strip(), f"示范资产缺少永久护盾: {asset['name']}"

    episodes = client.get(f"/api/projects/{proj['id']}/episodes").json()["episodes"]
    assert len(episodes) == 1
    ep = episodes[0]
    assert (ep.get("script_text") or "").strip()
    assert (ep.get("hook_0_3s") or "").strip()
    assert (ep.get("bgm_prompt") or "").strip()

    shots_payload = client.get(f"/api/episodes/{ep['id']}/shots").json()
    shots = shots_payload["shots"]
    assert len(shots) == 6
    assert sum(int(s.get("duration_sec") or 0) for s in shots) >= 60
    assert any(s.get("flow_mode") == "frames_to_video" for s in shots)
    assert any(s.get("ref_asset_ids") for s in shots)
    assert any((s.get("compiled") or {}).get("active_guards") for s in shots)

    # 重复调用 init_db 必须保持幂等，不重复插入示范记录
    studio_env.db.init_db()
    assert len(client.get("/api/projects").json()["projects"]) == 1
    assert len(client.get(f"/api/projects/{proj['id']}/assets").json()["assets"]) == 3
    assert len(client.get(f"/api/projects/{proj['id']}/episodes").json()["episodes"]) == 1
    assert len(client.get(f"/api/episodes/{ep['id']}/shots").json()["shots"]) == 6


def test_config_win32_secret_storage_and_media_engine_mode(studio_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证模拟 Windows(win32) 跨平台密钥写入 .secrets.env 以及 MEDIA_ENGINE_MODE 读写与校验"""
    cfg_mod = studio_env.config
    monkeypatch.setattr(cfg_mod.sys, "platform", "win32")

    demo_token = "placeholder-local-test-secret-0123456789"
    backend_used = cfg_mod.set_secret("GEMINI_API_KEY", demo_token)
    assert backend_used == "secrets_env"
    assert studio_env.secrets_path.exists()
    assert cfg_mod.get_secret("GEMINI_API_KEY") == demo_token
    assert cfg_mod.load_config()["GEMINI_API_KEY"] == demo_token

    pub = cfg_mod.get_public_config()
    assert pub["secret_storage_backend"] == "secrets_env"
    assert pub["gemini_key_set"] is True
    assert demo_token not in pub["gemini_key_masked"]
    assert pub["media_engine_mode"] == "auto"
    assert isinstance(pub["google_genai_sdk_ok"], bool)
    assert isinstance(pub["gemini_cli_ok"], bool)

    # 通过设置接口切换 MEDIA_ENGINE_MODE
    r_sdk = studio_env.client.patch("/api/settings", json={"MEDIA_ENGINE_MODE": "sdk"})
    assert r_sdk.status_code == 200
    assert r_sdk.json()["settings"]["media_engine_mode"] == "sdk"

    r_cli = studio_env.client.patch("/api/settings", json={"MEDIA_ENGINE_MODE": "cli"})
    assert r_cli.status_code == 200
    assert r_cli.json()["settings"]["media_engine_mode"] == "cli"

    r_bad = studio_env.client.patch("/api/settings", json={"MEDIA_ENGINE_MODE": "unknown_mode"})
    assert r_bad.status_code == 400

    # 清空密钥后不应残留旧值
    cfg_mod.set_secret("GEMINI_API_KEY", "")
    assert cfg_mod.get_secret("GEMINI_API_KEY") == ""


def test_subscription_presets_and_apply_preset(studio_env) -> None:
    """验证 /api/subscription-presets 与 /api/quota/apply-preset 校准三口油箱"""
    client = studio_env.client

    presets_resp = client.get("/api/subscription-presets")
    assert presets_resp.status_code == 200
    presets_data = presets_resp.json()
    preset_ids = {p["id"] for p in presets_data["presets"]}
    assert {"free", "ai_pro", "ai_ultra", "ai_ultra_dev", "custom"}.issubset(preset_ids)

    apply_resp = client.post("/api/quota/apply-preset", json={"tier": "ai_ultra_dev"})
    assert apply_resp.status_code == 200
    applied = apply_resp.json()
    assert applied["ok"] is True
    assert applied["tier"] == "ai_ultra_dev"
    assert applied["quota"]["subscription_tier"] == "ai_ultra_dev"
    assert applied["quota"]["gemini"]["monthly_credit_usd"] == 100.0
    assert applied["quota"]["flow"]["monthly_total"] == 25000
    assert applied["quota"]["flow"]["calibrated_remaining"] == 25000

    summary = client.get("/api/quota/summary").json()
    assert summary["subscription_tier"] == "ai_ultra_dev"
    assert summary["gemini"]["monthly_credit_usd"] == 100.0
    assert summary["flow"]["monthly_total"] == 25000

    bad_resp = client.post("/api/quota/apply-preset", json={"tier": "nonexistent_tier"})
    assert bad_resp.status_code == 400


def test_flow_compiler_negative_guard_deduplication(studio_env) -> None:
    """验证 flow_compiler 永久防穿帮护盾按顺序去重（忽略大小写与多余空白）并拼入全部 Prompt"""
    project = {
        "aspect_ratio": "9:16",
        "global_style_prompt": "Cinematic 35mm film look, crisp rim light",
    }
    assets_map = {
        1: {
            "id": 1,
            "name": "角色_星光小猫01",
            "flow_tag": "@StarCat",
            "asset_type": "character",
            "en_anchor_prompt": "miniature white ceramic robotic cat",
            "negative_guard_en": "strictly NO organic fleshy paws; keep brass bell collar intact.",
        },
        2: {
            "id": 2,
            "name": "道具_发光星图01",
            "flow_tag": "@StarMap",
            "asset_type": "prop",
            "en_anchor_prompt": "brass pocket star projector",
            "negative_guard_en": "STRICTLY   no organic fleshy paws; keep brass bell collar intact",
        },
        3: {
            "id": 3,
            "name": "场景_霓虹天台01",
            "flow_tag": "@NeonRoof",
            "asset_type": "landmark",
            "en_anchor_prompt": "rain-slicked rooftop under twilight sky",
            "negative_guard_en": "no garbled neon signage",
        },
    }
    shot = {
        "flow_mode": "frames_to_video",
        "ref_asset_ids": [1, 2, 3],
        "duration_sec": 6,
        "camera_type": "特写",
        "frame_prompt_en": "Close-up of @StarCat looking at @StarMap on @NeonRoof",
        "end_frame_prompt_en": "Wide pullback as golden holograms bloom above @NeonRoof",
        "video_prompt_en": "Slow dolly out revealing holographic constellation",
        "audio_strategy": "keep_flow_sfx",
        "cn_dialogue": "",
    }

    compiled = studio_env.flow_compiler.compile_shot_prompts(shot, project, assets_map)
    guards = compiled["active_guards"]
    assert len(guards) == 2
    assert guards[0] == "strictly NO organic fleshy paws; keep brass bell collar intact"
    assert guards[1] == "no garbled neon signage"

    for key in (
        "compiled_frame_prompt",
        "cli_frame_prompt",
        "compiled_end_frame_prompt",
        "cli_end_frame_prompt",
        "compiled_video_prompt",
        "cli_video_prompt",
    ):
        text = compiled[key]
        assert "MANDATORY VISUAL CONSTRAINTS:" in text
        assert text.lower().count("strictly no organic fleshy paws") == 1
        assert "no garbled neon signage" in text


def test_inbox_claim_asset_raw_and_normal_shot_no_end_frame_lock(studio_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    回归测试两项 P0-5 路由修复：
    1. `POST /api/inbox/claim` 支持 `target_type="asset_raw"` 并回写 `assets.raw_image_path`；
    2. `POST /api/shots/{shot_id}/cli-generate-video` 当普通镜头存在 `end_frame_path` 但
       `flow_mode != "frames_to_video"` 时绝不误锁尾帧；仅 `frames_to_video` 才传 `end_frame`。
    """
    client = studio_env.client
    projs = client.get("/api/projects").json()["projects"]
    proj_id = projs[0]["id"]
    assets = client.get(f"/api/projects/{proj_id}/assets").json()["assets"]
    asset_id = assets[0]["id"]

    # 1. 测试 inbox/claim asset_raw
    raw_src = _write_test_png(studio_env.downloads_dir / "flow_raw_capture_01.png")

    class _StubInbox:
        def __init__(self, file_path: Path) -> None:
            self._items = {
                "item_raw_01": {
                    "id": "item_raw_01",
                    "filename": file_path.name,
                    "path": str(file_path),
                    "batch_prefix": "",
                }
            }

        def get_item(self, item_id: str) -> dict[str, Any] | None:
            return self._items.get(item_id)

        def claim_file(self, item_id: str, dest_dir: Path, stem: str) -> dict[str, Any]:
            item = self._items.pop(item_id, None)
            if not item:
                raise FileNotFoundError(item_id)
            src = Path(item["path"])
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{stem}{src.suffix.lower() or '.png'}"
            shutil.copy2(src, dest)
            return {
                "path": str(dest.resolve()),
                "filename": src.name,
                "media_type": "image",
                "width": 64,
                "height": 114,
                "duration_sec": 0.0,
                "resolution_label": "64x114",
            }

        def copy_to_dock_downloads(self, src: Path, stem: str) -> Path:
            dest = studio_env.tmp_path / "dock" / f"{stem}{src.suffix}"
            shutil.copy2(src, dest)
            return dest

        def trash_batch_siblings(self, _prefix: str, exclude_item_id: str = "") -> list[str]:
            return []

        def list_items(self) -> list[dict[str, Any]]:
            return list(self._items.values())

    monkeypatch.setattr(studio_env.studio, "downloads_inbox", _StubInbox(raw_src))

    claim_resp = client.post(
        "/api/inbox/claim",
        json={
            "inbox_item_id": "item_raw_01",
            "target_type": "asset_raw",
            "target_id": asset_id,
            "keep_chinese_copy": False,
        },
    )
    assert claim_resp.status_code == 200, claim_resp.text
    claim_data = claim_resp.json()
    claimed_path = Path(claim_data["claimed"]["path"])
    assert claimed_path.exists()
    assert claimed_path.is_relative_to(studio_env.media_dir)

    refreshed_assets = client.get(f"/api/projects/{proj_id}/assets").json()["assets"]
    target_asset = next(a for a in refreshed_assets if a["id"] == asset_id)
    assert target_asset["raw_image_path"] == str(claimed_path.resolve())

    # 2. 测试普通镜头含 end_frame_path 但 flow_mode != "frames_to_video" 不锁尾帧
    episodes = client.get(f"/api/projects/{proj_id}/episodes").json()["episodes"]
    ep_id = episodes[0]["id"]
    shots = client.get(f"/api/episodes/{ep_id}/shots").json()["shots"]
    normal_shot = next(s for s in shots if s["flow_mode"] != "frames_to_video")
    shot_id = normal_shot["id"]

    sf_path = _write_test_png(studio_env.media_dir / "test_frames" / "start.png")
    ef_path = _write_test_png(studio_env.media_dir / "test_frames" / "end.png")
    with studio_env.db.db_ctx() as conn:
        conn.execute(
            "UPDATE shots SET start_frame_path = ?, end_frame_path = ? WHERE id = ?",
            (str(sf_path), str(ef_path), shot_id),
        )

    captured_calls: list[dict[str, Any]] = []

    def _fake_cli_generate_video(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_calls.append(dict(kwargs))
        out_dir = Path(kwargs["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        dummy_mp4 = out_dir / f"{kwargs.get('prefix', 'shot')}_stub_{len(captured_calls)}.mp4"
        dummy_mp4.write_bytes(b"stub-mp4-bytes")
        return {
            "model": "gemini-omni-1.2-flash",
            "resolution": kwargs.get("resolution", "360p"),
            "est_cost_usd": 0.14,
            "videos": [
                {
                    "path": str(dummy_mp4.resolve()),
                    "filename": dummy_mp4.name,
                    "requested_resolution": kwargs.get("resolution", "360p"),
                    "width": 360,
                    "height": 640,
                    "duration_sec": float(kwargs.get("duration_sec") or 6),
                    "resolution_label": "360p",
                    "est_cost_usd": 0.14,
                }
            ],
        }

    monkeypatch.setattr(studio_env.studio, "cli_generate_video", _fake_cli_generate_video)

    r_normal = client.post(f"/api/shots/{shot_id}/cli-generate-video", json={"resolution": "360p", "count": 1})
    assert r_normal.status_code == 200, r_normal.text
    assert len(captured_calls) == 1
    assert captured_calls[0]["start_frame"] == sf_path
    assert captured_calls[0]["end_frame"] is None, "普通镜头即使存在 end_frame_path 也绝不应误锁尾帧"

    # 切为 frames_to_video 后，应正确传入 end_frame
    patch_r = client.patch(f"/api/shots/{shot_id}", json={"flow_mode": "frames_to_video"})
    assert patch_r.status_code == 200
    r_f2v = client.post(f"/api/shots/{shot_id}/cli-generate-video", json={"resolution": "360p", "count": 1})
    assert r_f2v.status_code == 200, r_f2v.text
    assert len(captured_calls) == 2
    assert captured_calls[1]["end_frame"] == ef_path


def test_jobs_submit_progress_cancel_and_zombie_recovery(studio_env) -> None:
    """验证 jobs.py 异步任务提交、进度/日志更新、协作取消、HTTP 查询与启动僵尸收敛"""
    client = studio_env.client
    jobs_mod = studio_env.jobs

    projs = client.get("/api/projects").json()["projects"]
    ep_id = client.get(f"/api/projects/{projs[0]['id']}/episodes").json()["episodes"][0]["id"]

    # 1. 提交任务 -> 更新进度 -> 成功完成
    def _success_worker(ctx) -> dict[str, Any]:
        ctx.update(progress_pct=45, stage_msg="处理中间步骤", log_line="stage-1 ok")
        ctx.check_cancelled()
        return {"output_path": str(studio_env.exports_dir / "fake_out.mp4"), "ok": True, "detail": "done"}

    created = jobs_mod.submit_job("smoke_ok", _success_worker, episode_id=ep_id, stage_msg="任务已入队")
    job_id = created["id"]

    final_job: dict[str, Any] = {}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        final_job = resp.json()
        if final_job["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)

    assert final_job["status"] == "done"
    assert final_job["progress_pct"] == 100
    assert final_job["result"]["ok"] is True
    assert "stage-1 ok" in final_job["log_text"]

    list_resp = client.get(f"/api/jobs?episode_id={ep_id}")
    assert list_resp.status_code == 200
    listed_ids = [item["id"] for item in list_resp.json()["items"]]
    assert job_id in listed_ids

    # 2. 协作式取消运行中任务
    started = threading.Event()

    def _cancellable_worker(ctx) -> dict[str, Any]:
        started.set()
        for _ in range(100):
            ctx.check_cancelled()
            time.sleep(0.02)
        return {"ok": True}

    c_job = jobs_mod.submit_job("smoke_cancel", _cancellable_worker, episode_id=ep_id)
    assert started.wait(timeout=3.0), "后台测试任务未按期启动"

    cancel_resp = client.post(f"/api/jobs/{c_job['id']}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["cancel_requested"] is True

    cancelled_state: dict[str, Any] = {}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        cancelled_state = client.get(f"/api/jobs/{c_job['id']}").json()
        if cancelled_state["status"] in ("cancelled", "done", "failed"):
            break
        time.sleep(0.05)
    assert cancelled_state["status"] == "cancelled"

    # 3. 启动时收敛残留 running / pending 僵尸任务
    ts = studio_env.db.now_str()
    with studio_env.db.db_ctx() as conn:
        c1 = conn.execute(
            """
            INSERT INTO render_jobs (episode_id, job_type, target_host, status, progress_pct, stage_msg, created_at, updated_at)
            VALUES (?, 'zombie_run', 'local', 'running', 55, '正在运行', ?, ?)
            """,
            (ep_id, ts, ts),
        ).lastrowid
        c2 = conn.execute(
            """
            INSERT INTO render_jobs (episode_id, job_type, target_host, status, progress_pct, stage_msg, created_at, updated_at)
            VALUES (?, 'zombie_wait', 'local', 'pending', 0, '排队中', ?, ?)
            """,
            (ep_id, ts, ts),
        ).lastrowid

    recovered_cnt = jobs_mod.recover_stale_jobs_on_startup()
    assert recovered_cnt >= 2

    for zid in (c1, c2):
        zjob = jobs_mod.get_job(int(zid))
        assert zjob is not None
        assert zjob["status"] == "interrupted"
        assert "重启" in zjob["stage_msg"]
        assert "重启" in zjob["error_msg"]


def test_media_ops_vo_sync_pad_speed_and_srt_alignment(studio_env) -> None:
    """验证 media_ops 在 VO 超长时的 pad / speed 时长计算与 export_srt 时间轴精准对齐"""
    media_ops = studio_env.media_ops

    vo_long = _write_test_wav(studio_env.tmp_path / "vo_long.wav", duration_sec=3.5)
    vo_short = _write_test_wav(studio_env.tmp_path / "vo_short.wav", duration_sec=1.2)

    timeline_items = [
        {
            "shot_id": 1,
            "shot_no": 1,
            "shot_title": "超长旁白镜",
            "effective_duration": 2.0,
            "source_duration": 2.0,
            "trim_start": 0.0,
            "audio_strategy": "keep_flow_sfx",
            "vo_file_path": str(vo_long),
            "vo_duration_sec": 3.5,
            "subtitle_text": "第一镜旁白超过两秒，需要自动对齐避免截断。",
            "cn_dialogue": "第一镜旁白超过两秒，需要自动对齐避免截断。",
        },
        {
            "shot_id": 2,
            "shot_no": 2,
            "shot_title": "短旁白镜",
            "effective_duration": 3.0,
            "source_duration": 3.0,
            "trim_start": 0.0,
            "audio_strategy": "keep_flow_sfx",
            "vo_file_path": str(vo_short),
            "vo_duration_sec": 1.2,
            "subtitle_text": "第二镜简短旁白。",
            "cn_dialogue": "第二镜简短旁白。",
        },
    ]

    # 显式验证 plan_timeline_with_vo_sync 的 pad / speed 数学规则（含 ≤1.20x 纯微加速与 >1.20x 限速+兜底定格）
    if hasattr(media_ops, "plan_timeline_with_vo_sync"):
        plan_pad = media_ops.plan_timeline_with_vo_sync(timeline_items, vo_sync_mode="pad")
        assert plan_pad["items"][0]["pad_sec"] == pytest.approx(1.7, abs=0.05)
        assert plan_pad["items"][0]["effective_duration"] == pytest.approx(3.7, abs=0.05)
        assert plan_pad["items"][1]["cursor_start"] == pytest.approx(3.7, abs=0.05)

        plan_speed = media_ops.plan_timeline_with_vo_sync(timeline_items, vo_sync_mode="speed")
        assert plan_speed["items"][0]["vo_tempo"] == pytest.approx(1.20, abs=0.01)
        assert plan_speed["items"][0]["pad_sec"] > 0.0
        assert plan_speed["items"][0]["effective_duration"] < plan_pad["items"][0]["effective_duration"]

        # 轻微超长（≤1.20x）在 speed 模式下仅微加速、不做尾帧定格
        mild_item = [{**timeline_items[0], "effective_duration": 3.2, "vo_duration_sec": 3.5, "vo_file_path": ""}]
        plan_mild = media_ops.plan_timeline_with_vo_sync(mild_item, vo_sync_mode="speed")
        assert 1.0 < plan_mild["items"][0]["vo_tempo"] <= 1.20
        assert plan_mild["items"][0]["pad_sec"] == 0.0

    srt_params = inspect.signature(media_ops.export_srt).parameters
    srt_pad_path = studio_env.exports_dir / "sync_pad.srt"
    if "vo_sync_mode" in srt_params:
        media_ops.export_srt(timeline_items, srt_pad_path, vo_sync_mode="pad")
    else:
        media_ops.export_srt(timeline_items, srt_pad_path)

    pad_entries = media_ops._parse_srt(srt_pad_path)
    assert len(pad_entries) == 2
    s1_start, s1_end, _ = pad_entries[0]
    s2_start, s2_end, _ = pad_entries[1]

    # pad 模式：第 1 镜应延展包住 3.5s VO（eff_dur ≈ 3.7s），且第 2 镜顺延在其后、不重叠
    assert s1_end >= 3.45, f"pad 模式第 1 镜字幕未随超长 VO 延展: {pad_entries}"
    assert s2_start >= s1_end - 0.05, f"第 2 镜字幕与第 1 镜发生时间重叠: {pad_entries}"
    assert s2_start >= 3.55, f"第 2 镜起点未按 pad 延长量顺延: {pad_entries}"
    # 第 2 镜 VO 仅 1.2s，字幕结束时间应紧贴 VO 而非干晾满 3.0s
    assert (s2_end - s2_start) <= 2.2, f"短旁白字幕未紧贴 VO 真实时长: {pad_entries}"

    if "vo_sync_mode" in srt_params:
        srt_speed_path = studio_env.exports_dir / "sync_speed.srt"
        media_ops.export_srt(timeline_items, srt_speed_path, vo_sync_mode="speed")
        speed_entries = media_ops._parse_srt(srt_speed_path)
        assert len(speed_entries) == 2
        sp1_start, sp1_end, _ = speed_entries[0]
        sp2_start, _, _ = speed_entries[1]
        # speed 模式上限 1.20x（3.5 / 1.2 = 2.917s），仍需少量 pad 兜底，但总时长明显短于纯 pad
        assert 2.8 <= sp1_end < s1_end
        assert sp2_start >= sp1_end - 0.05
        assert sp2_start < s2_start

    # 若环境具备真实 ffmpeg/ffprobe，追加端到端真视频 pad/speed 合成与流时长验证
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        clip_2s = _write_test_mp4(studio_env.tmp_path / "shot_2s.mp4", duration_sec=2.0)
        single_item = [
            {
                **timeline_items[0],
                "file_path": str(clip_2s),
                "width": 360,
                "height": 640,
            }
        ]

        out_pad = studio_env.exports_dir / "master_pad.mp4"
        res_pad = media_ops.build_master_base_mp4(
            single_item,
            out_pad,
            vo_sync_mode="pad",
            burn_srt_path=srt_pad_path,
        )
        assert out_pad.exists()
        assert res_pad["total_duration_sec"] >= 3.5
        probed_pad = media_ops.probe_media(out_pad)
        assert probed_pad["duration_sec"] >= 3.45

        out_speed = studio_env.exports_dir / "master_speed.mp4"
        res_speed = media_ops.build_master_base_mp4(
            single_item,
            out_speed,
            vo_sync_mode="speed",
        )
        assert out_speed.exists()
        assert 2.8 <= res_speed["total_duration_sec"] < res_pad["total_duration_sec"]


def test_cloud_collab_mode_404_and_401_isolation(studio_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 cloud_collab 自托管协作模式下的 404 本地算力封禁、401 登录/同步鉴权隔离与健康脱敏"""
    client = studio_env.client
    monkeypatch.setenv("APP_MODE", "cloud_collab")
    if hasattr(studio_env.main, "_health_env_cache") and isinstance(studio_env.main._health_env_cache, dict):
        studio_env.main._health_env_cache["ts"] = 0.0
        studio_env.main._health_env_cache["values"] = None

    # 1. /api/health 免登录且彻底剥离 Google/Colab 字段
    health_resp = client.get("/api/health")
    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert health_data["app_mode"] == "cloud_collab"
    assert "google_genai_sdk_ok" not in health_data
    assert "gemini_cli_ok" not in health_data
    assert "colab_cli_ok" not in health_data

    # 2. 本地生成、油箱与算力调度接口在协作模式下必须返回 404
    blocked_calls = [
        ("GET", "/api/quota/summary"),
        ("GET", "/api/inbox"),
        ("GET", "/api/settings/probe-colab"),
        ("POST", "/api/episodes/1/render-local"),
        ("POST", "/api/episodes/1/dispatch-colab"),
        ("POST", "/api/episodes/1/qc-frames-sheet"),
        ("POST", "/api/episodes/1/qc-inspect-videos"),
    ]
    for method, path in blocked_calls:
        r = client.request(method, path)
        assert r.status_code == 404, f"cloud_collab 模式下 {method} {path} 应返回 404，实际 {r.status_code}"

    # 3. 常规业务接口未登录必须返回 401
    for path in ("/api/projects", "/api/settings"):
        r = client.get(path)
        assert r.status_code == 401, f"cloud_collab 模式下未登录访问 {path} 应返回 401，实际 {r.status_code}"

    # 4. 同步接口未携带有效 X-Sync-Token 必须返回 401
    r_sync = client.get("/api/sync/pull")
    assert r_sync.status_code == 401
