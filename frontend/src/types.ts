export type AppMode = 'local_producer' | 'cloud_collab';

export interface AuthUser {
  id?: number;
  username: string;
  display_name: string;
  role: 'admin' | 'partner' | string;
}

export interface AuthMeResponse {
  authenticated: boolean;
  app_mode: AppMode;
  brand_name?: string;
  user: AuthUser | null;
  public_config?: Record<string, unknown>;
}

export interface SeasonOutlineItem {
  ep: number;
  title: string;
  hook: string;
  status: string;
}

export interface ContinuityLedger {
  main_characters?: string[];
  recurring_locations?: string[];
  visited_landmarks?: string[];
  open_cliffhangers?: string[];
  notes?: string;
}

export interface Project {
  id: number;
  name: string;
  slug: string;
  track: string;
  visual_mode: string;
  aspect_ratio: string;
  target_duration_sec: number;
  global_style_prompt: string;
  description: string;
  season_outline: SeasonOutlineItem[];
  continuity_ledger: ContinuityLedger;
  episode_count?: number;
  asset_count?: number;
  created_at: string;
  updated_at: string;
}

export interface HotItem {
  rank: number;
  title: string;
  hot_value: number;
  video_count: number;
  label: string;
  summary: string;
  url: string;
  source: string;
}

export interface Topic {
  id: number;
  title: string;
  source: string;
  source_url: string;
  hot_value: number;
  video_count: number;
  summary: string;
  scores: Record<string, number>;
  score_theme?: number;
  total_score: number;
  grade: 'S' | 'A' | 'DROP' | 'UNSCORED';
  content_tier?: string;
  veto: number;
  veto_reason: string;
  ai_pitch: string;
  recommended_project_id: number | null;
  status: string;
  created_at: string;
}

export interface Asset {
  id: number;
  project_id: number;
  asset_type: 'character' | 'landmark' | 'prop' | 'style';
  name: string;
  flow_tag: string;
  raw_image_path: string;
  clean_image_path: string;
  en_anchor_prompt: string;
  negative_guard_en?: string;
  voice_mode: string;
  voice_en_desc: string;
  voice_clone_sample_path: string;
  notes: string;
}

export interface CoverCandidate {
  index: number;
  ratio: '9:16' | '3:4' | string;
  platform?: string;
  label?: string;
  path: string;
  filename?: string;
  width?: number;
  height?: number;
}

export interface Episode {
  id: number;
  project_id: number;
  ep_number: number;
  title: string;
  topic_id: number | null;
  hook_0_3s: string;
  ending_cta: string;
  next_ep_hook: string;
  script_text: string;
  continuity_summary: string;
  bgm_prompt: string;
  bgm_file_path: string;
  bgm_volume: number;
  vo_mode?: string;
  selected_cover_path?: string;
  cover_candidates_json?: string;
  cover_candidates?: CoverCandidate[];
  web_preview_path?: string;
  frames_sheet_path?: string;
  covers_sheet_path?: string;
  qc_overview_path?: string;
  ai_proof_path?: string;
  sync_updated_at?: string;
  status: string;
  latest_output_path: string;
  shot_count?: number;
  ready_shot_count?: number;
  updated_at?: string;
}

export interface Take {
  id: number;
  shot_id: number;
  media_type: 'video' | 'image' | 'audio';
  file_path: string;
  original_filename: string;
  width: number;
  height: number;
  duration_sec: number;
  resolution_label: string;
  rating: number;
  is_final: number;
  notes: string;
  created_at: string;
}

export interface QuickEditPrompt {
  label: string;
  prompt: string;
}

export interface CompiledPrompt {
  compiled_frame_prompt: string;
  compiled_end_frame_prompt?: string;
  compiled_video_prompt: string;
  cli_frame_prompt?: string;
  cli_end_frame_prompt?: string;
  cli_video_prompt?: string;
  image_count?: number;
  image_model?: string;
  video_count?: number;
  flow_entry_guide?: string;
  quick_edit_prompts?: QuickEditPrompt[];
  active_guards?: string[];
  max_refs: number;
  ref_images: Array<{
    asset_id: number;
    name: string;
    flow_tag: string;
    asset_type: string;
    image_path: string;
  }>;
  warnings: string[];
}

export interface CliImageCandidate {
  index: number;
  path: string;
  filename: string;
  width: number;
  height: number;
  resolution_label: string;
}

export interface EditorialReport {
  score_before: number;
  verdict: string;
  weaknesses: string[];
  revision_orders: string[];
  revision_summary: string;
}

export interface PlatformCopyItem {
  platform: string;
  angle: string;
  title: string;
  body: string;
  hashtags: string[];
  pinned_comment: string;
}

export interface MultiPlatformCopyPack {
  cover_headline: string;
  platforms: PlatformCopyItem[];
}

export interface Shot {
  id: number;
  episode_id: number;
  shot_no: number;
  shot_title: string;
  camera_type: string;
  duration_sec: number;
  flow_mode: string;
  ref_asset_ids: number[];
  cn_action_desc: string;
  cn_dialogue: string;
  subtitle_text: string;
  frame_prompt_en: string;
  end_frame_prompt_en?: string;
  video_prompt_en: string;
  start_frame_path: string;
  end_frame_path: string;
  vo_file_path?: string;
  vo_source?: string;
  qc_strip_path?: string;
  qc_report_json?: string;
  audio_strategy: string;
  upscale_target: '1080p_free' | '4k_hero';
  upscale_done: number;
  trim_start: number;
  trim_end: number;
  sort_order: number;
  compiled: CompiledPrompt;
  takes: Take[];
}

export interface BudgetSummary {
  shot_count: number;
  total_duration_sec: number;
  nb_daily_images: number;
  draft_credits: number;
  final_720p_credits: number;
  upscale_credits: number;
  total_estimated_credits: number;
  four_k_count: number;
  alerts: string[];
}

export interface InboxItem {
  id: string;
  filename: string;
  path: string;
  media_type: 'video' | 'image' | 'audio';
  origin_label?: string;
  batch_prefix?: string;
  size_bytes: number;
  width: number;
  height: number;
  duration_sec: number;
  resolution_label: string;
  detected_at: string;
}

export interface InboxState {
  status: string;
  watch_dir: string;
  dock_drag_dir?: string;
  active_context_label?: string;
  auto_rename_flow_downloads?: boolean;
  auto_trash_rejected_batch?: boolean;
  filter_mode?: 'flow_only' | 'all';
  is_dedicated_dir?: boolean;
  filtered_skipped?: number;
  error: string;
  items: InboxItem[];
}

export interface TimelineItem {
  shot_id: number;
  shot_no: number;
  shot_title: string;
  subtitle_text: string;
  cn_dialogue: string;
  audio_strategy: string;
  upscale_target: string;
  upscale_done: number;
  trim_start: number;
  trim_end: number;
  file_path: string;
  width: number;
  height: number;
  resolution_label: string;
  source_duration: number;
  effective_duration: number;
  vo_file_path?: string;
  vo_source?: string;
  qc_strip_path?: string;
}

export interface RenderJob {
  id: number;
  episode_id: number;
  shot_id?: number | null;
  job_type: string;
  target_host: string;
  status: string;
  progress_pct?: number;
  stage_msg?: string;
  provider_task_id?: string;
  cancel_requested?: number;
  output_path: string;
  log_text: string;
  result_json?: string;
  result?: Record<string, any> | null;
  error_msg?: string;
  created_at: string;
  updated_at?: string;
}

export interface AsyncJob {
  id: number;
  episode_id: number;
  shot_id?: number | null;
  job_type: string;
  target_host: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'cancelled' | 'interrupted' | string;
  progress_pct?: number;
  stage_msg?: string;
  provider_task_id?: string;
  cancel_requested?: number;
  output_path?: string;
  log_text?: string;
  result_json?: string;
  result?: Record<string, any> | null;
  error_msg?: string;
  created_at: string;
  updated_at?: string;
}

export interface HealthStatus {
  status: string;
  app?: string;
  brand_name?: string;
  app_mode?: AppMode;
  version?: string;
  ffmpeg_ok?: boolean;
  ffprobe_ok?: boolean;
  google_genai_sdk_ok?: boolean;
  gemini_cli_ok?: boolean;
  colab_cli_ok?: boolean;
  disk_free_gb?: number | null;
  path_warning?: string;
}

export interface QcShotReport {
  video_path?: string;
  strip_path?: string;
  width?: number;
  height?: number;
  duration_sec?: number;
  has_audio?: boolean;
  mean_volume_db?: number | null;
  max_volume_db?: number | null;
  voice_band_mean_db?: number | null;
  voice_band_max_db?: number | null;
  silence_duration_sec?: number;
  silence_ratio?: number;
  frame_lumas?: number[];
  expect_dialogue?: boolean;
  status?: 'ok' | 'warn' | string;
  warnings?: string[];
  summary?: string;
}

export interface QcInspectVideoItem {
  shot_id: number;
  shot_no: number;
  shot_title: string;
  take_id?: number;
  resolution_label?: string;
  video_path?: string;
  qc_strip_path?: string;
  expect_dialogue?: boolean;
  has_warning: boolean;
  warnings: string[];
  report: QcShotReport;
}

export interface QcInspectVideosResponse {
  episode_id: number;
  resolution_filter: string;
  inspected_count: number;
  passed_count: number;
  warning_count: number;
  missing_shots: number[];
  overview_sheet_path: string;
  shots: QcInspectVideoItem[];
}

export interface QcFramesSheetResponse {
  episode_id: number;
  sheet_path: string;
  output_path: string;
  image_count: number;
  cols: number;
  title: string;
  width?: number;
  height?: number;
}

export interface PublishLog {
  id: number;
  project_id: number | null;
  episode_id: number | null;
  platform: string;
  post_title: string;
  published_at: string;
  views: number;
  likes: number;
  comments: number;
  shares: number;
  favorites: number;
  completion_rate: number;
  moments_feedback: string;
  ai_review: string;
}

export interface CommentDraft {
  id: number;
  platform: string;
  fan_comment: string;
  tone: string;
  drafts: Array<{ style: string; text: string }>;
  topic_idea: string;
  converted_topic_id: number | null;
  status?: 'draft' | 'replied' | 'promoted' | string;
  replied_text?: string;
  replied_by?: string;
  replied_at?: string;
  created_at: string;
}

export interface CliPricing {
  image: Record<string, number>;
  video_per_sec: Record<string, number>;
  music: number;
  tts: number;
}

export interface SubscriptionPreset {
  id: string;
  name: string;
  description: string;
  gemini_daily_limit: number;
  gemini_monthly_credit_usd: number;
  flow_monthly_total: number;
  default_mode: string;
  media_strategy: string;
  recommended_gpu: string;
}

export interface PublicSettings {
  app_mode?: AppMode;
  brand_name?: string;
  subscription_tier?: string;
  enable_hot_feeds?: boolean;
  enable_cloud_collab?: boolean;
  cloud_studio_url?: string;
  sync_token_set?: boolean;
  ntfy_enabled?: boolean;
  public_studio_url?: string;
  gemini_key_set: boolean;
  gemini_key_masked: string;
  gemini_model: string;
  google_proxy_url: string;
  deepseek_enabled: boolean;
  deepseek_key_set: boolean;
  deepseek_key_masked: string;
  deepseek_model: string;
  default_provider: 'gemini' | 'deepseek';
  media_engine_mode?: 'auto' | 'sdk' | 'cli' | string;
  google_genai_sdk_ok?: boolean;
  gemini_cli_ok?: boolean;
  cli_image_model?: string;
  cli_video_model?: string;
  cli_music_model?: string;
  cli_tts_model?: string;
  cli_pricing_json?: string;
  cli_pricing?: CliPricing;
  secret_storage_backend?: string;
  watch_downloads_dir: string;
  watch_downloads_resolved: string;
  dock_drag_dir?: string;
  dock_drag_resolved?: string;
  keep_chinese_copy_on_claim?: boolean;
  auto_rename_flow_downloads?: boolean;
  auto_trash_rejected_batch?: boolean;
  inbox_filter_mode?: string;
  colab_api_base: string;
  colab_mcp_endpoint: string;
  colab_default_gpu: string;
  colab_cli_path: string;
  colab_gdrive_root: string;
  colab_mcp_configured: boolean;
}

export interface PromptTemplate {
  key: string;
  title: string;
  description: string;
  content: string;
  updated_at: string;
}

export interface ColabActiveSession {
  endpoint: string;
  accelerator: string;
  variant: string;
  machine_shape: string;
}

export interface ProbeColab {
  cli_installed: boolean;
  cli_path: string;
  cli_version: string;
  api_base: string;
  mcp_endpoint: string;
  mcp_configured: boolean;
  authorized: boolean;
  email: string;
  ccu_balance: number | null;
  ccu_rate_hourly: number | null;
  assignments_count: number;
  active_sessions: ColabActiveSession[];
  available_specs: string[];
  public_api_ok: boolean;
  public_api_note: string;
  message: string;
}

export interface PlatformCampaign {
  id: number;
  platform: string;
  title: string;
  hashtags: string[];
  deadline: string;
  incentive: string;
  angle_pitch: string;
  source_url: string;
  status: 'active' | 'expired' | 'archived';
  batch_date: string;
  converted_topic_id: number | null;
  created_at: string;
  updated_at: string;
}

/* ==================== 可选自托管多人协作、留痕、投票与规范建议类型 ==================== */

export interface RevisionItem {
  id: number;
  entity_type: string;
  entity_id: number;
  episode_id: number | null;
  project_id?: number | null;
  ep_number?: number | null;
  episode_title?: string;
  project_name?: string;
  field_name: string;
  field_label?: string;
  old_value: string;
  new_value: string;
  actor_username: string;
  actor_name: string;
  actor_role: string;
  note: string;
  created_at: string;
}

export interface ReviewVote {
  id: number;
  episode_id: number;
  target_type: 'frame' | 'video_360p' | 'cover' | string;
  target_key: string;
  choice: string;
  verdict: 'approve' | 'change' | 'comment' | string;
  comment: string;
  actor_username: string;
  actor_name: string;
  actor_role: string;
  created_at: string;
}

export interface RuleSuggestion {
  id: number;
  template_key: string;
  rule_title: string;
  suggestion_text: string;
  actor_username: string;
  actor_name: string;
  actor_role: string;
  status: 'open' | 'adopted' | 'dismissed' | string;
  admin_reply: string;
  handled_by: string;
  handled_at: string;
  created_at: string;
}

export interface SanitizedRule {
  key: string;
  title: string;
  description: string;
  content: string;
  updated_at?: string;
  suggestions: RuleSuggestion[];
}

export interface PendingEpisodeCard {
  id?: number;
  episode_id?: number;
  project_id: number;
  project_name?: string;
  project_slug?: string;
  ep_number: number;
  title: string;
  status?: string;
  shot_count?: number;
  frames_ready_count?: number;
  video_ready_count?: number;
  frame_count?: number;
  ready_shot_count?: number;
  cover_count?: number;
  votes_count?: number;
  my_vote?: { verdict?: string; choice?: string; comment?: string } | null;
  votes_summary?: { total?: number; approve?: number; change?: number; comment?: number };
  frames_sheet_path?: string;
  covers_sheet_path?: string;
  qc_overview_path?: string;
  ai_proof_path?: string;
  web_preview_path?: string;
  latest_output_path?: string;
  selected_cover_path?: string;
  updated_at?: string;
}

export interface CollabInboxResponse {
  pending_frames_episodes: PendingEpisodeCard[];
  pending_videos_episodes: PendingEpisodeCard[];
  pending_covers_episodes: PendingEpisodeCard[];
  recent_revisions: RevisionItem[];
  open_rule_suggestions: RuleSuggestion[];
  pending_comments: CommentDraft[];
  counts?: {
    total_todos?: number;
    total?: number;
    pending_frames?: number;
    pending_videos?: number;
    pending_covers?: number;
    open_rule_suggestions?: number;
    open_suggestions?: number;
    pending_comments?: number;
    recent_revisions?: number;
    other_party_revisions?: number;
    [k: string]: number | undefined;
  };
}

export interface LocalSyncStatus {
  configured?: boolean;
  online?: boolean;
  cloud_url?: string;
  latency_ms?: number;
  message?: string;
  error?: string;
}
