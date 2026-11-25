"""
双模型 AI 创作引擎（默认 Gemini API 优先，DeepSeek 作为默认关闭的可选备用开关）：
- Gemini API：通过 google-genai SDK + 可选精准代理客户端调用，支持 Google Search Grounding 联网选题挖掘；
- DeepSeek API：仅当设置中显式开启 DEEPSEEK_ENABLED=true 且选中 deepseek 时调用（或用于自托管协作模式），强制直连。
"""

from __future__ import annotations

import json
import re
import ssl
import time
from typing import Any

import httpx

try:
    from google import genai
    from google.genai import types
except ImportError:  # 自托管极简协作模式无需安装 google-genai 依赖
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except ImportError:  # 极简环境可回退至纯 httpx 直连 DeepSeek
    OpenAI = None  # type: ignore[assignment]

from .config import is_cloud_collab, load_config
from .db import db_ctx, now_str
from .net import make_cn_sync_client, make_google_sync_client


def estimate_text_cost_usd(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按官方公开 Gemini API 参考费率估算单次文本调用成本（USD / 百万 Token），用于额度预算预警"""
    if provider != "gemini":
        return 0.0
    m = (model or "").lower()
    if ("gemini-3" in m or "3." in m or "3-" in m) and "pro" in m:
        # Gemini 3 Pro 系列公开估算档
        in_rate, out_rate = 2.00, 12.00
    elif "2.5-pro" in m or "pro" in m:
        # Gemini 2.5 Pro 等 Pro 系列公开估算档
        in_rate, out_rate = 1.25, 10.00
    elif "flash-lite" in m or "lite" in m:
        # Flash-Lite 轻量档
        in_rate, out_rate = 0.10, 0.40
    elif "gemini-3" in m or "3." in m or "3-" in m:
        # Gemini 3 Flash 系列公开估算档
        in_rate, out_rate = 0.50, 3.00
    else:
        # Gemini 2.5 Flash 及其他默认档
        in_rate, out_rate = 0.30, 2.50
    p = max(0, int(prompt_tokens or 0))
    c = max(0, int(completion_tokens or 0))
    return round((p * in_rate + c * out_rate) / 1_000_000.0, 6)


def _record_api_usage(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    action: str = "generate",
    est_cost_usd: float | None = None,
) -> None:
    """将每次真实 API 调用次数、Token 与估算美元花费记入本地 SQLite，供左下角额度监控展示"""
    try:
        cost = (
            round(float(est_cost_usd), 6)
            if est_cost_usd is not None
            else estimate_text_cost_usd(provider, model, prompt_tokens, completion_tokens)
        )
        with db_ctx() as conn:
            conn.execute(
                """
                INSERT INTO api_usage_logs (provider, model, action, prompt_tokens, completion_tokens, total_tokens, est_cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    model,
                    action,
                    max(0, int(prompt_tokens or 0)),
                    max(0, int(completion_tokens or 0)),
                    max(0, int(total_tokens or 0)),
                    max(0.0, cost),
                    now_str(),
                ),
            )
    except Exception:
        pass


def get_prompt_template(key: str) -> str:
    """读取指定规则模板正文；兼容旧键名自动映射"""
    with db_ctx() as conn:
        row = conn.execute("SELECT content FROM prompt_templates WHERE key = ?", (key,)).fetchone()
        if row and row["content"]:
            return str(row["content"])
        if key == "cultural_benchmark":
            fallback = conn.execute("SELECT content FROM prompt_templates WHERE key = 'benchmark_adapter'").fetchone()
            if fallback and fallback["content"]:
                return str(fallback["content"])
        return ""


def _resolve_provider(requested: str | None = None) -> tuple[str, dict[str, str]]:
    cfg = load_config()
    if is_cloud_collab(cfg):
        # 自托管协作模式仅走直连 DeepSeek，不调用本地海外模型接口
        if not cfg["DEEPSEEK_API_KEY"].strip():
            raise ValueError("协作端 AI 密钥未就绪，请联系管理员检查服务端 DeepSeek 密钥配置。")
        return "deepseek", cfg

    ds_enabled = cfg["DEEPSEEK_ENABLED"].lower() in ("1", "true", "yes", "on")
    if requested == "deepseek":
        if not ds_enabled:
            raise ValueError("DeepSeek 备用开关当前处于关闭状态。如需切换，请先在「系统设置」开启 DeepSeek 备用开关。")
        if not cfg["DEEPSEEK_API_KEY"].strip():
            raise ValueError("未填写 DeepSeek API Key，请在「系统设置」配置。")
        return "deepseek", cfg

    if requested == "gemini" or requested is None:
        default_p = cfg.get("DEFAULT_PROVIDER", "gemini")
        if requested is None and default_p == "deepseek" and ds_enabled and cfg["DEEPSEEK_API_KEY"].strip():
            return "deepseek", cfg
        if not cfg["GEMINI_API_KEY"].strip():
            raise ValueError("尚未配置 Gemini API Key。请先前往「系统设置」填入你的 Gemini API Key。")
        return "gemini", cfg

    raise ValueError(f"不支持的模型供应商: {requested}")


def _make_gemini_client(cfg: dict[str, str], use_socks: bool = False) -> tuple[Any, httpx.Client]:
    if is_cloud_collab(cfg):
        raise RuntimeError("协作模式已禁用本地海外模型接口。")
    if genai is None or types is None:
        raise RuntimeError("当前运行环境未安装 google-genai 依赖。")
    hc = make_google_sync_client(timeout=120.0, use_socks=use_socks)
    http_opts = types.HttpOptions(api_version="v1beta", httpx_client=hc)
    return genai.Client(api_key=cfg["GEMINI_API_KEY"].strip(), http_options=http_opts), hc


def _is_transient_ssl_or_proxy_err(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout, ssl.SSLError, ConnectionResetError)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("unexpected_eof", "eof occurred", "connection reset", "broken pipe", "remoteprotocolerror"))


def _extract_gemini_text(resp: Any) -> str:
    """优先提取非 thought 正文分片，过滤思考过程杂讯"""
    cands = getattr(resp, "candidates", None) or []
    if cands:
        content = getattr(cands[0], "content", None)
        parts = getattr(content, "parts", None) or []
        normal = [getattr(p, "text", "") or "" for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
        joined = "".join(normal).strip()
        if joined:
            return joined
    return (getattr(resp, "text", None) or "").strip()


def list_available_gemini_models() -> list[dict[str, str]]:
    """一键探测当前 Gemini API Key 真实可用模型列表（带代理断连自动重试）"""
    cfg = load_config()
    if is_cloud_collab(cfg):
        raise ValueError("协作模式已禁用海外模型探测。")
    if not cfg["GEMINI_API_KEY"].strip():
        raise ValueError("请先填写并保存 Gemini API Key 后再探测模型。")

    has_proxy = bool((cfg.get("GOOGLE_PROXY_URL") or "").strip())
    last_err: Exception | None = None
    for attempt in range(3):
        client, hc = _make_gemini_client(cfg, use_socks=(has_proxy and attempt >= 1))
        try:
            result: list[dict[str, str]] = []
            for m in client.models.list():
                name = getattr(m, "name", "") or ""
                short_name = name.replace("models/", "")
                actions = getattr(m, "supported_actions", None) or []
                if "gemini" not in short_name.lower():
                    continue
                if actions and "generateContent" not in actions:
                    continue
                result.append(
                    {
                        "id": short_name,
                        "display_name": getattr(m, "display_name", short_name) or short_name,
                    }
                )
            result.sort(key=lambda x: x["id"], reverse=True)
            return result[:40]
        except Exception as exc:
            last_err = exc
            if _is_transient_ssl_or_proxy_err(exc) and attempt < 2:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
        finally:
            hc.close()
    raise last_err or RuntimeError("探测 Gemini 模型失败")


def generate_text(
    system_prompt: str,
    user_prompt: str,
    *,
    provider: str | None = None,
    expect_json: bool = False,
    use_web_search: bool = False,
    temperature: float = 0.7,
) -> tuple[str, str]:
    """统一文本/结构化生成入口，返回 (文本内容, 实际使用的模型标识)"""
    chosen, cfg = _resolve_provider(provider)

    if chosen == "gemini":
        model_id = cfg.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
        base_cfg_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "temperature": temperature,
            "thinking_config": types.ThinkingConfig(include_thoughts=False),
        }
        if use_web_search:
            base_cfg_kwargs["tools"] = [{"google_search": {}}]
        elif expect_json:
            base_cfg_kwargs["response_mime_type"] = "application/json"

        has_proxy = bool((cfg.get("GOOGLE_PROXY_URL") or "").strip())
        last_err: Exception | None = None
        for attempt in range(3):
            client, hc = _make_gemini_client(cfg, use_socks=(has_proxy and attempt >= 1))
            try:
                try:
                    resp = client.models.generate_content(
                        model=model_id,
                        contents=user_prompt,
                        config=types.GenerateContentConfig(**base_cfg_kwargs),
                    )
                except Exception as inner_exc:
                    # 兼容不支持 thinking_config 的模型，去掉后平滑重试
                    if "thinking" in str(inner_exc).lower() and "thinking_config" in base_cfg_kwargs:
                        fallback_kwargs = dict(base_cfg_kwargs)
                        fallback_kwargs.pop("thinking_config", None)
                        resp = client.models.generate_content(
                            model=model_id,
                            contents=user_prompt,
                            config=types.GenerateContentConfig(**fallback_kwargs),
                        )
                    else:
                        raise

                text = _extract_gemini_text(resp)
                if not text:
                    raise RuntimeError("Gemini 返回了空内容，请检查提示词或重试。")
                um = getattr(resp, "usage_metadata", None)
                p_tok = getattr(um, "prompt_token_count", 0) or 0
                c_tok = getattr(um, "candidates_token_count", 0) or 0
                t_tok = getattr(um, "total_token_count", 0) or (p_tok + c_tok)
                _record_api_usage("gemini", model_id, p_tok, c_tok, t_tok, action="search" if use_web_search else "generate")
                return text, f"gemini/{model_id}"
            except Exception as exc:
                last_err = exc
                if _is_transient_ssl_or_proxy_err(exc) and attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise
            finally:
                hc.close()
        raise last_err or RuntimeError("调用 Gemini 失败，请检查网络连接或代理配置")

    # DeepSeek 直连路线（强制 trust_env=False，兼容无 openai SDK 的极简环境）
    model_id = cfg.get("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    api_key = cfg["DEEPSEEK_API_KEY"].strip()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    if expect_json:
        kwargs["response_format"] = {"type": "json_object"}

    http_client = make_cn_sync_client(timeout=120.0)
    try:
        if OpenAI is not None:
            ds_client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com",
                http_client=http_client,
            )
            resp = ds_client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            u = getattr(resp, "usage", None)
            p_tok = getattr(u, "prompt_tokens", 0) or 0
            c_tok = getattr(u, "completion_tokens", 0) or 0
            t_tok = getattr(u, "total_tokens", 0) or (p_tok + c_tok)
        else:
            r = http_client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=kwargs,
            )
            r.raise_for_status()
            payload = r.json()
            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError("DeepSeek 返回了空响应，请稍后重试。")
            text = str((choices[0].get("message") or {}).get("content") or "").strip()
            u = payload.get("usage") or {}
            p_tok = int(u.get("prompt_tokens") or 0)
            c_tok = int(u.get("completion_tokens") or 0)
            t_tok = int(u.get("total_tokens") or (p_tok + c_tok))
    finally:
        http_client.close()

    if not text:
        raise RuntimeError("DeepSeek 返回了空内容，请检查提示词或稍后重试。")
    _record_api_usage("deepseek", model_id, p_tok, c_tok, t_tok)
    return text, f"deepseek/{model_id}"


def extract_json(raw: str) -> Any:
    """容错提取模型输出的 JSON（兼容 markdown 代码块或前后夹杂说明）"""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = cleaned.find(open_ch)
        end = cleaned.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"AI 返回内容无法解析为 JSON，请重试。片段：{cleaned[:220]}")


# ==================== 核心业务 AI 能力（全题材通用） ====================


def _compose_cached_system_prompt(base_template: str, stable_context: str = "") -> str:
    """
    将稳定的系统规则模板与剧目/资产圣经固定在 system_instruction 前缀位置，
    避免混入逐次变化的动态指令，最大化 Gemini 隐式上下文缓存（Implicit Caching）命中率。
    """
    base = (base_template or "").strip()
    ctx = (stable_context or "").strip()
    if base and ctx:
        return f"{base}\n\n{ctx}"
    return base or ctx


def _format_projects_brief(projects: list[dict[str, Any]]) -> str:
    if not projects:
        return "- （当前暂无剧目，请按通用精品 AI 微短剧标准构思）"
    ordered = sorted(projects, key=lambda p: int(p.get("id") or 0))
    return "\n".join(
        [
            f"- ID={p['id']} 《{p['name']}》（赛道：{p.get('track') or '通用短剧'}）：{p.get('description') or ''}"
            for p in ordered
        ]
    )


def _format_project_bible_prefix(
    project: dict[str, Any],
    existing_assets: list[dict[str, Any]] | None = None,
) -> str:
    """构造字节级稳定的剧目档案与跨集资产圣经前缀（按 ID 排序、确定性序列化）"""
    lines = [
        "【固定剧目档案（稳定前缀）】",
        f"- 剧目名称：《{project.get('name', '')}》",
        f"- 赛道与视觉路线：{project.get('track', '')} / {project.get('visual_mode', '')} / 画幅 {project.get('aspect_ratio', '9:16')}",
        f"- 目标时长：约 {project.get('target_duration_sec', 75)} 秒",
    ]
    global_style = (project.get("global_style_prompt") or "").strip()
    if global_style:
        lines.append(f"- 全局视觉锁词：{global_style}")
    season_outline = project.get("season_outline", "[]")
    if season_outline and str(season_outline).strip() not in ("", "[]"):
        lines.append(f"- 本剧季大纲：{season_outline}")
    continuity = project.get("continuity_ledger", "{}")
    if continuity and str(continuity).strip() not in ("", "{}"):
        lines.append(f"- 跨集连续性台账（严禁吃书）：{continuity}")

    if existing_assets is not None:
        ordered_assets = sorted(existing_assets, key=lambda a: int(a.get("id") or 0))
        assets_brief = [
            {
                "id": a["id"],
                "type": a["asset_type"],
                "name": a["name"],
                "flow_tag": a["flow_tag"],
                "en": a["en_anchor_prompt"],
                **({"guard": a["negative_guard_en"]} if (a.get("negative_guard_en") or "").strip() else {}),
            }
            for a in ordered_assets
        ]
        lines.append("【跨集资产圣经（优先复用已有资产 ID，严禁违背护盾锁词）】")
        lines.append(json.dumps(assets_brief, ensure_ascii=False, sort_keys=True))

    return "\n".join(lines)


def score_topics_with_ai(
    candidates: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    theme: str = "",
    provider: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """批量给热点/灵感按「主题契合 + 钩子反转 + 合规体面 + 破圈势能 + AI 可拍性」5 维标准打分"""
    proj_brief = _format_projects_brief(projects)
    sys_prompt = _compose_cached_system_prompt(
        get_prompt_template("topic_scorer"),
        f"【当前工作室剧目列表（稳定上下文）】\n{proj_brief}",
    )
    theme_hint = (theme or "").strip() or "紧密结合现有剧目定位与赛道风格"
    user_prompt = f"""本次选题目标主题/赛道倾向：{theme_hint}

请对以下 {len(candidates)} 个候选热点/灵感逐一评估「能否巧妙嫁接/借壳拍成高质量 AI 微短剧」并给出真实 5 维打分：
{json.dumps(candidates, ensure_ascii=False, indent=2)}

【本次批审实战规则】
1. **借壳转译不生搬**：全网热点主要是参考其爆款情绪、反差结构或视觉壳子，结合目标剧目与原创角色自然转译，并在 `summary` 开头注明 `原热点：「原标题」｜...`。
2. **严控 AI 可拍性**：优先设计适合 AI 稳稳出片的视觉方案（3D 虚拟搭档/动物/器物微距/机械体/单人背影与手部动作/大场面，单集 ≤3 场景）；若必须靠多人正脸长对话才能成立，把 `serial` 压到 ≤6 并判 DROP。
3. **只看有没有戏**：正能量、科普、生活、科技或娱乐热梗一视同仁，核心看有没有具体主角困境与反转，绝不做空洞画面拼盘；无关联或涉肖像侵权直接低分或 `veto=true`。
4. **严格拉大分差**：本批 {len(candidates)} 条中，**最多只允许 1-2 条达到 S 级（85-92 分）**，其余老实拉开到 A 级（75-84）和 DROP（<75）。
5. `ai_pitch` 必须写清四段（不写 `#标签`）：
   `【主角】谁｜【0-3s钩子】开局画面与冲突｜【冲突与反转】中段较劲与反转｜【AI可拍性】为什么好拍或哪里易翻车`
6. `total` 必须严格等于 `theme + hook + safe + viral + serial` 五项精确之和；grade 规则：veto=true 或 total<75 标 "DROP"，75-84 标 "A"，>=85 标 "S"。

必须严格输出 JSON 对象（不要附带额外解释）：
{{
  "results": [
    {{
      "title": "有电影感/悬念感的短剧标题（淘汰项保留原标题或注明硬伤）",
      "source": "沿用原source",
      "source_url": "沿用原url或留空",
      "hot_value": 0,
      "video_count": 0,
      "content_tier": "【精品口碑级】或【破圈玩梗级】",
      "summary": "原热点：「...」｜一句话交代嫁接思路或淘汰/降级短板",
      "scores": {{"theme": 0, "hook": 0, "safe": 0, "viral": 0, "serial": 0}},
      "total": 0,
      "grade": "DROP",
      "veto": false,
      "veto_reason": "",
      "ai_pitch": "【主角】...｜【0-3s钩子】...｜【冲突与反转】...｜【AI可拍性】...",
      "recommended_project_id": 1
    }}
  ]
}}"""

    raw, used = generate_text(sys_prompt, user_prompt, provider=provider, expect_json=True, temperature=0.45)
    parsed = extract_json(raw)
    results = parsed.get("results") or []
    for idx, item in enumerate(results):
        if idx < len(candidates):
            src = candidates[idx]
            if not item.get("source"):
                item["source"] = src.get("source") or "hot_feed"
            if not item.get("source_url"):
                item["source_url"] = src.get("url") or src.get("source_url") or ""
            if not item.get("hot_value"):
                item["hot_value"] = int(src.get("hot_value") or 0)
            if not item.get("video_count"):
                item["video_count"] = int(src.get("video_count") or 0)
    return results, used


def search_topic_ideas(
    focus_keyword: str,
    projects: list[dict[str, Any]],
    theme: str = "",
    provider: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """用 Gemini Google Search Grounding 联网广泛挖掘指定主题/关键词的真实素材，并按 5 维标准打分"""
    proj_brief = _format_projects_brief(projects)
    sys_prompt = _compose_cached_system_prompt(
        get_prompt_template("topic_scorer"),
        f"【工作室现有剧目（稳定上下文）】\n{proj_brief}",
    )
    theme_str = (theme or "").strip()
    kw = (focus_keyword or "").strip()
    if not kw and theme_str:
        kw = f"{theme_str} 近期高热度故事、反差趣闻与视觉奇观切口"
    elif not kw:
        kw = "近期高热度电影感微短剧灵感（覆盖科幻脑洞、萌宠治愈、美食手作、职场生活、人文风物与硬核科普等方向）"

    theme_clause = f"目标赛道/主题定位：{theme_str}" if theme_str else "目标赛道/主题定位：结合现有剧目灵活匹配"
    user_prompt = f"""请联网搜索近期关于「{kw}」的真实新闻、行业进展、生活趣闻、人文细节或视觉热点，挑出 **5 个题材尽量不重复的切口**，分别构思 1 个 60–85 秒电影级 AI 微短剧选题，并用总制片人视角苛刻打分。

{theme_clause}

【实战要求】
1. 搜到的真实信息仅作为「事实底座（写在 summary）」，绝不写成流水账汇报片，必须落到**具体主角（优先 3D 虚拟搭档/动物/器物/机械体/单人动作特写）+ 开局困境冲突 + 中后段反转**。
2. 必须实打实评估 **AI 视频工具能不能稳稳做出来**：若某题材必须靠多人正脸长对话或高难群像动作、易出恐怖谷，请老实压低 `serial`（≤6）并放进 DROP 帮制片人避坑。
3. 严禁复用陈旧套路，每次搜索必须根据真实素材原创不同的人物与剧情。
4. 严格拉开 5 条选题的分差梯队：**最多 1-2 条 S 级（85–92 分）**，1–2 条 A 级（75–84 分），至少 1–2 条 DROP（<75 分）。
5. `ai_pitch` 必须按四段写透（不写 `#标签`）：
   `【主角】谁｜【0-3s钩子】开局画面与冲突｜【冲突与反转】中段较劲与反转｜【AI可拍性】核心镜头为什么好拍或哪里易翻车`
6. `total` 必须严格等于 `theme + hook + safe + viral + serial` 五项之和；grade 规则：veto=true 或 total<75 标 "DROP"，75-84 标 "A"，>=85 标 "S"。

严格只输出 JSON（不要附带额外解释）：
{{
  "results": [
    {{
      "title": "电影感/悬念感短剧标题（18字内）",
      "source": "topic_search",
      "source_url": "参考真实链接或留空",
      "content_tier": "【精品口碑级】或【破圈玩梗级】",
      "summary": "简述搜到的真实素材底座，以及该选题的亮点或扣分短板",
      "scores": {{"theme": 0, "hook": 0, "safe": 0, "viral": 0, "serial": 0}},
      "total": 0,
      "grade": "A",
      "veto": false,
      "veto_reason": "",
      "ai_pitch": "【主角】...｜【0-3s钩子】...｜【冲突与反转】...｜【AI可拍性】...",
      "recommended_project_id": 1
    }}
  ]
}}"""

    raw, used = generate_text(
        sys_prompt,
        user_prompt,
        provider=provider,
        expect_json=False,
        use_web_search=(provider != "deepseek"),
        temperature=0.55,
    )
    parsed = extract_json(raw)
    return parsed.get("results") or [], used


def search_benchmarks_with_ai(
    focus: str,
    projects: list[dict[str, Any]],
    *,
    theme: str = "",
    keyword: str = "",
    count: int = 6,
    provider: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """全网爆款结构对标与主题化转译，读取 `benchmark_adapter` 规则模板"""
    proj_brief = _format_projects_brief(projects)
    sys_prompt = _compose_cached_system_prompt(
        get_prompt_template("benchmark_adapter"),
        f"【工作室现有剧目（稳定上下文）】\n{proj_brief}",
    )
    n = max(3, min(8, int(count or 6)))
    focus_str = (focus or keyword or "").strip() or "近期全网刷屏的电影感 AI 短片、强视觉奇观与高完播微短剧"
    theme_str = (theme or "").strip() or "结合现有剧目定位自由匹配最契合的赛道"

    user_prompt = f"""请检索近期全网爆火的短视频/AI 短片典型结构，拆解其爆火公式，并转译为 **{n} 个差异化升级版 AI 微短剧选题**。

参考对标方向：{focus_str}
目标转译主题：{theme_str}

要求：
1. 每条选题明确写清参考的爆款手法（`benchmark_case`）与我方差异化升级点；
2. 每条必须输出 `content_tier`（`【精品口碑级】` 或 `【破圈玩梗级】`）与四段式 `ai_pitch`；
3. `total` 严格等于 `theme + hook + safe + viral + serial` 之和，且必须拉开 S / A / DROP 分差梯队。

严格只输出 JSON 对象：
{{
  "results": [
    {{
      "title": "升级版短剧标题（18字内）",
      "source": "benchmark_adapter",
      "source_url": "",
      "benchmark_case": "参考爆款类型与底层结构手法",
      "content_tier": "【精品口碑级】",
      "summary": "对标拆解与升级亮点简述",
      "scores": {{"theme": 0, "hook": 0, "safe": 0, "viral": 0, "serial": 0}},
      "total": 0,
      "grade": "A",
      "veto": false,
      "veto_reason": "",
      "ai_pitch": "【主角】...｜【0-3s钩子】...｜【冲突与反转】...｜【AI可拍性】...",
      "recommended_project_id": 1
    }}
  ]
}}"""

    raw, used = generate_text(
        sys_prompt,
        user_prompt,
        provider=provider,
        expect_json=False,
        use_web_search=(provider != "deepseek"),
        temperature=0.6,
    )
    parsed = extract_json(raw)
    return parsed.get("results") or [], used


def generate_episode_script_with_ai(
    project: dict[str, Any],
    ep_number: int,
    topic_title: str,
    topic_pitch: str,
    extra_notes: str = "",
    provider: str | None = None,
) -> tuple[dict[str, Any], str]:
    """生成 50-85 秒黄金节奏分集剧本与双尾钩"""
    sys_prompt = _compose_cached_system_prompt(
        get_prompt_template("script_writer"),
        _format_project_bible_prefix(project),
    )
    user_prompt = f"""本次创作第 {ep_number} 集：
选题标题：{topic_title}
核心卖点：{topic_pitch}
创作者补充要求：{extra_notes or '无特殊补充，保证前3秒极强视觉冲击、克制高级、规避写实人脸长对话恐怖谷'}

请严格返回如下 JSON 结构：
{{
  "title": "本集最终爆款标题（18字内）",
  "hook_0_3s": "0-3秒生死线视觉奇观与钩子描述",
  "script_text": "完整分段落拍摄剧本（明确标出 0-3s / 3-15s / 15-45s / 45-65s / 65-80s 画面、动作、精炼金句台词与音效节奏）",
  "ending_cta": "结尾评论区互动提问诱饵（自然激发观众讨论、站队或猜彩蛋）",
  "next_ep_hook": "最后3秒下集预告悬念钩子",
  "bgm_prompt": "给 AI 配乐引擎（如 Lyria 3.5）用的英文纯音乐配乐提示词（明确节奏起伏、核心乐器与情绪，无歌词）",
  "continuity_summary": "本集结束后需更新进跨集连续性台账的一句话（记录出场角色状态、重要场景与埋下的伏笔）"
}}"""

    raw, used = generate_text(sys_prompt, user_prompt, provider=provider, expect_json=True, temperature=0.75)
    return extract_json(raw), used


def critique_and_revise_script_with_ai(
    project: dict[str, Any],
    ep_number: int,
    draft_script: dict[str, Any],
    extra_notes: str = "",
    provider: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """
    多角色编辑部审稿返工引擎（编剧初稿 → 毒舌主编挑刺 → 定向改稿出终稿）：
    返回 (终版剧本 dict, 编辑部审稿报告 dict, 使用模型名)。
    """
    project_prefix = _format_project_bible_prefix(project)
    critic_sys = _compose_cached_system_prompt(get_prompt_template("script_critic"), project_prefix)
    critic_user = f"""创作者补充要求：{extra_notes or '无'}

以下是编剧交上来的第 {ep_number} 集初稿剧本 JSON：
{json.dumps(draft_script, ensure_ascii=False, indent=2)}

请用毒舌审稿主编的眼光逐条挑刺，严格返回如下 JSON（不要客套）：
{{
  "score_before": 72,
  "verdict": "一句话毒舌总评（直指初稿最大问题）",
  "weaknesses": [
    "具体硬伤 1（指出到具体秒数/句子/镜头，说明为什么会掉完播或 AI 难拍）",
    "具体硬伤 2",
    "具体硬伤 3"
  ],
  "revision_orders": [
    "必改指令 1（写明具体怎么改，如把 0-3s 改成什么视觉冲突/动作）",
    "必改指令 2",
    "必改指令 3"
  ]
}}"""
    critic_raw, used_critic = generate_text(critic_sys, critic_user, provider=provider, expect_json=True, temperature=0.45)
    critique = extract_json(critic_raw)

    writer_sys = _compose_cached_system_prompt(get_prompt_template("script_writer"), project_prefix)
    revise_user = f"""你是本剧总编剧，现在请严格按照【毒舌审稿主编的必改指令】对第 {ep_number} 集初稿进行定向重写升级！

创作者补充要求：{extra_notes or '无'}

【初稿剧本】：
{json.dumps(draft_script, ensure_ascii=False, indent=2)}

【毒舌审稿主编批注与必改指令（必须逐条落实解决）】：
{json.dumps(critique, ensure_ascii=False, indent=2)}

请输出打磨升级后的终版 JSON（字段与原结构完全一致，并附带一句 `revision_summary` 总结改了哪几个关键点）：
{{
  "title": "升级后的本集爆款标题（18字内）",
  "hook_0_3s": "升级后的 0-3 秒生死线视觉奇观与钩子",
  "script_text": "升级后的完整分段落拍摄剧本（明确 0-3s / 3-15s / 15-45s / 45-65s / 65-80s 画面、单主运镜、动作、<=18字精炼台词与音效节奏）",
  "ending_cta": "升级后的结尾评论区互动诱饵",
  "next_ep_hook": "升级后的最后 3 秒下集预告悬念钩子",
  "bgm_prompt": "给 AI 配乐引擎用的英文纯音乐配乐提示词",
  "continuity_summary": "本集结束后需更新进跨集连续性台账的一句话",
  "revision_summary": "一句话说明本轮根据主编意见重点强化了哪几处"
}}"""
    rev_raw, used_rev = generate_text(writer_sys, revise_user, provider=provider, expect_json=True, temperature=0.7)
    revised = extract_json(rev_raw)
    report = {
        "score_before": critique.get("score_before", 0),
        "verdict": critique.get("verdict", ""),
        "weaknesses": critique.get("weaknesses") or [],
        "revision_orders": critique.get("revision_orders") or [],
        "revision_summary": revised.get("revision_summary", ""),
    }
    return revised, report, used_rev or used_critic


def generate_episode_script_editorial_with_ai(
    project: dict[str, Any],
    ep_number: int,
    topic_title: str,
    topic_pitch: str,
    extra_notes: str = "",
    provider: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """一键运行完整双轮编辑部流水线：编剧写初稿 → 毒舌主编挑刺 → 定向改稿出终稿"""
    draft, _ = generate_episode_script_with_ai(
        project=project,
        ep_number=ep_number,
        topic_title=topic_title,
        topic_pitch=topic_pitch,
        extra_notes=extra_notes,
        provider=provider,
    )
    return critique_and_revise_script_with_ai(
        project=project,
        ep_number=ep_number,
        draft_script=draft,
        extra_notes=extra_notes,
        provider=provider,
    )


def break_script_to_shots_with_ai(
    project: dict[str, Any],
    episode: dict[str, Any],
    existing_assets: list[dict[str, Any]],
    provider: str | None = None,
) -> tuple[dict[str, Any], str]:
    """自动从剧本提取缺失资产（角色/场景/道具）并拆解为标准分镜表与英文提示词"""
    sys_prompt = _compose_cached_system_prompt(
        get_prompt_template("shot_breaker"),
        _format_project_bible_prefix(project, existing_assets=existing_assets),
    )
    user_prompt = f"""本集（第 {episode['ep_number']} 集《{episode['title']}》）完整剧本：
{episode.get('script_text', '')}

请完成两件事，并严格按以下 JSON 格式返回：
1. `new_assets`：列出本集出现、但现有资产库还没有的关键资产（区分 character / landmark / prop），提供中文名、简短英文 `@flow_tag` 和精准英文不变锁词 `en_anchor_prompt`。
2. `shots`：拆解为 10-14 个电影分镜（总时长约 {project['target_duration_sec']} 秒，每镜 4-8 秒），每镜必须给：
   - `shot_no`（从1递增）、`shot_title`（6字内镜名）、`camera_type`（大远景/特写/微距/推镜/跟拍等）、`duration_sec`（4/6/8）
   - `flow_mode`：无固定声线对白的空镜/动作/微距镜头优先填 **`veo31_lite`**（大幅省分），只有含角色固定声线对白或 >3 图参考的镜头才填 **`omni12_i2v`**，首尾双帧过渡填 `frames_to_video`
   - `ref_asset_names`：本镜引用的资产中文名数组（控制 ≤3 个最稳）
   - `cn_action_desc`：中文画面与单一主运镜说明
   - `cn_dialogue`：若本镜有人物开口说短句则填（严格 ≤18 字），否则留空
   - `subtitle_text`：本镜压在屏幕上的精炼字幕/旁白金句
   - `frame_prompt_en`：给静帧模型生成首帧的纯英文视觉提示词（不含中文，明确画面构图与主次关系）
   - `video_prompt_en`：给视频模型驱动首帧动起来的纯英文动态运镜提示词（每镜只写 1 个主运镜；不要重复写 Audio 或 no background music，系统编译器会自动补全防幻觉语音尾段）
   - `audio_strategy`：`keep_flow_sfx` / `keep_flow_dialogue` / `mute_for_local_vo`
   - `upscale_target`：一律默认 `1080p_free`（先导免费 1080p，全片 4K 超分交给云端 GPU 或后期统一跑）

JSON 模板：
{{
  "new_assets": [
    {{"asset_type": "character", "name": "资产名", "flow_tag": "@Tag", "en_anchor_prompt": "detailed consistent English visual description"}}
  ],
  "shots": [
    {{
      "shot_no": 1,
      "shot_title": "开场视觉钩子",
      "camera_type": "微距推镜",
      "duration_sec": 6,
      "flow_mode": "veo31_lite",
      "ref_asset_names": ["资产名"],
      "cn_action_desc": "...",
      "cn_dialogue": "",
      "subtitle_text": "...",
      "frame_prompt_en": "...",
      "video_prompt_en": "...",
      "audio_strategy": "keep_flow_sfx",
      "upscale_target": "1080p_free"
    }}
  ]
}}"""

    raw, used = generate_text(sys_prompt, user_prompt, provider=provider, expect_json=True, temperature=0.55)
    return extract_json(raw), used


def draft_comment_replies_with_ai(
    fan_comment: str,
    platform: str,
    episode_context: str = "",
    tone_pref: str = "balanced",
    provider: str | None = None,
) -> tuple[dict[str, Any], str]:
    """半自动评论拟稿（仅生成草稿供人工审核一键复制 + 提炼潜在新选题，绝不自动发评）"""
    sys_prompt = get_prompt_template("comment_reply")
    user_prompt = f"""发布平台：{platform}
关联视频背景：{episode_context or '电影级原创 AI 微短剧'}
期望侧重语气：{tone_pref}

观众真实评论原文：
「{fan_comment}」

请严格输出以下 JSON：
{{
  "drafts": [
    {{"style": "大方走心（温暖体面）", "text": "..."}},
    {{"style": "幽默接梗（轻松自然）", "text": "..."}},
    {{"style": "追更引导（评论区共创）", "text": "..."}}
  ],
  "has_topic_potential": true,
  "topic_idea": "如果这条评论里藏着下集好选题（如观众点名某剧情分支/道具/彩蛋），写一句可直接转入选题库的短剧选题标题；没有则留空字符串"
}}"""

    raw, used = generate_text(sys_prompt, user_prompt, provider=provider, expect_json=True, temperature=0.75)
    return extract_json(raw), used


def review_analytics_with_ai(
    logs: list[dict[str, Any]],
    provider: str | None = None,
) -> tuple[str, str]:
    """基于多平台发布数据输出简洁犀利的复盘诊断与下集优化建议"""
    sys_prompt = get_prompt_template("analytics_review")
    user_prompt = f"""以下是近期各集在各短视频平台的真实数据与观众反馈记录：
{json.dumps(logs, ensure_ascii=False, indent=2)}

请输出一份简洁、直击要害、可直接执行的中文复盘报告（用 Markdown 分 3 点呈现：数据诊断 / 保留与砍掉项 / 下一集前3秒与评论区互动改写建议）。"""
    return generate_text(sys_prompt, user_prompt, provider=provider, expect_json=False, temperature=0.6)


def generate_multi_platform_copy_with_ai(
    *,
    project_name: str,
    episode_title: str,
    hook_0_3s: str = "",
    script_excerpt: str = "",
    ending_cta: str = "",
    extra_notes: str = "",
    active_campaigns: list[dict[str, Any]] | None = None,
    provider: str | None = None,
) -> tuple[dict[str, Any], str]:
    """
    纯本地零风险四平台发布文案适配器：
    一次性输出微信视频号 / 抖音 / 小红书 / B站四套差异化标题、正文、话题标签、封面花字与置顶评论；
    自动联动进行中的官方流量扶持活动，把匹配的必带 #话题标签 纳入对应平台文案。
    """
    sys_prompt = get_prompt_template("multi_platform_copy")
    campaign_block = "暂无登记中的官方扶持活动。"
    if active_campaigns:
        lines = []
        for c in active_campaigns[:8]:
            tags_txt = " ".join(c.get("hashtags") or [])
            lines.append(
                f"- [{c.get('platform', '全平台')}] 《{c.get('title', '')}》｜必带标签：{tags_txt or '无'}｜截止：{c.get('deadline') or '待核对'}"
            )
        campaign_block = "\n".join(lines)

    user_prompt = f"""剧目名称：{project_name or '原创 AI 微短剧'}
分集标题：{episode_title}
0-3 秒开场钩子：{hook_0_3s or '无'}
结尾互动诱饵：{ending_cta or '无'}
补充说明：{extra_notes or '突出题材特色、视觉质感与剧情反转'}
剧本/剧情摘要：
{(script_excerpt or '暂无剧本全文，请围绕剧目与分集标题定位发挥')[:1600]}

【当下进行中的官方流量扶持活动（若匹配对应平台，请自然纳入必带 #话题标签）】：
{campaign_block}

请严格按以下 JSON 格式返回四平台专属发布文案包：
{{
  "cover_headline": "通用爆款封面大字（6-10字，视觉冲击强、不土味）",
  "platforms": [
    {{
      "platform": "微信视频号",
      "angle": "社交圈克制高级转发向",
      "title": "克制高级的短片标题",
      "body": "40-80字正文（不含标签，适合熟人社交转发配文）",
      "hashtags": ["#AI短剧", "#原创微电影", "#示例标签3"],
      "pinned_comment": "适合发在自己评论区置顶的自然互动话头"
    }},
    {{
      "platform": "抖音",
      "angle": "前3秒冲突悬念 + 评论区互动向",
      "title": "15-24字反差/悬念标题",
      "body": "简短有网感正文",
      "hashtags": ["#AI短剧", "#反转剧情", "#示例标签3", "#示例标签4"],
      "pinned_comment": "能引发观众认领彩蛋或友好讨论的置顶评论"
    }},
    {{
      "platform": "小红书",
      "angle": "高颜值视觉审美 + 细节彩蛋收藏向",
      "title": "20字内带克制Emoji审美标题",
      "body": "分段清晰、带1个幕后巧思或小知识彩蛋的正文",
      "hashtags": ["#AI短片", "#视觉美学", "#短剧推荐", "#示例标签4", "#示例标签5"],
      "pinned_comment": "引导收藏与留言想看下一集内容的置顶评论"
    }},
    {{
      "platform": "B站",
      "angle": "故事感 + 有梗诚意制作向",
      "title": "有故事感与反差的B站标题",
      "body": "简介：交代剧情看点、制作巧思与三连互动暗号",
      "hashtags": ["#AI电影", "#原创短片", "#剧情", "#示例标签4"],
      "pinned_comment": "和观众聊制作细节或下集投票的置顶评论"
    }}
  ]
}}"""
    raw, used = generate_text(sys_prompt, user_prompt, provider=provider, expect_json=True, temperature=0.72)
    return extract_json(raw), used
