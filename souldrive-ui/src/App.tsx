import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { TransformComponent, TransformWrapper } from "react-zoom-pan-pinch";
import {
  Activity,
  BrainCircuit,
  CalendarDays,
  Cloud,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Check,
  ClipboardList,
  Download,
  Trash2,
  FileText,
  FolderOpen,
  GitBranch,
  History,
  KeyRound,
  Layers3,
  Image as ImageIcon,
  LocateFixed,
  Mail,
  Mic,
  Network,
  Paperclip,
  Pencil,
  Plus,
  Presentation,
  RefreshCw,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  Upload,
  UserRoundPlus,
  Volume2,
  VolumeX,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { EvidenceMarkdown } from "./EvidenceMarkdown";
import "./App.css";
import {
  canUseSelectedRegionWithoutClarification,
  captureDisplayFramePng,
  hasMediaPerceptionInput,
  hasUsableVisualEvidence,
  imageRegionFromPointer,
  recordedAudioToWavFile,
  shouldRunMultimodalDecision,
  shouldUseUiAutomationContext,
  shouldPrewarmVisualModel,
  type NormalizedImageRegion,
} from "./mediaCapture";
import {
  CANVAS_PADDING,
  NODE_HEIGHT,
  NODE_WIDTH,
  buildMindmapLayout,
  edgePath,
  graphFitScale,
  formatMindmapBranch,
  parseMindmap,
  type MindmapArtifact,
  type MindmapLayout,
  type PositionedNode,
} from "./mindmap";
import {
  formatSecurityActionError,
  readSecurityActionError,
  securityPanelMode,
} from "./securityErrors";
import { formatChatHttpFailure, formatChatTransportFailure } from "./chatErrors";
import { ExperimentMetricImportPanel } from "./ExperimentMetricImportPanel";

interface Message {
  role: "user" | "ai";
  content: string;
  timestampMs?: number;
}

type BrowserSpeechRecognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};

type BrowserSpeechRecognitionConstructor = new () => BrowserSpeechRecognition;

interface RuntimeStatus {
  locked: boolean;
  reason: string;
  auth_level: string;
  hardware_sn?: string | null;
  active_drive?: string | null;
  workspace_path?: string | null;
  indexing?: {
    status: string;
    run_id?: string | null;
    total_files: number;
    discovered_files?: number;
    skipped_files?: number;
    succeeded_files?: number;
    processed_files: number;
    chunk_count?: number;
    current_file?: string | null;
    failures?: Array<{ source_filename: string; reason: string; error_code?: string }>;
    failure_summary?: Record<string, number>;
    disk?: {
      ready: boolean;
      free_bytes?: number;
      minimum_free_bytes?: number;
    };
  };
  updated_at?: number;
}

interface RuntimeConfig {
  base_url: string;
  api_token: string;
}

interface CloudModelConfig {
  configured: boolean;
  enabled: boolean;
  base_url: string;
  model: string;
  models: string[];
  api_key_configured: boolean;
}

interface SkillApiResponse {
  status: number;
  body: string;
}

async function requestSkillApi<T>(
  method: "GET" | "POST" | "DELETE",
  path: string,
  body?: unknown,
  idempotencyKey?: string,
): Promise<T> {
  try {
    const response = await invoke<SkillApiResponse>("skill_api_request", {
      method,
      path,
      body: body === undefined ? null : JSON.stringify(body),
      idempotencyKey: idempotencyKey || null,
    });
    const payload = response.body ? JSON.parse(response.body) as T & { error?: string } : {} as T & { error?: string };
    if (response.status < 200 || response.status >= 300) {
      throw new Error(payload.error || `Skill 操作失败（${response.status}）`);
    }
    return payload;
  } catch (error) {
    if (error instanceof Error && error.message !== "Failed to fetch") throw error;
    throw new Error("本地 Skill 服务不可用，请确认状态为 READY 后重试");
  }
}

interface EvidenceItem {
  id: string;
  source_filename: string;
  source_filenames?: string[];
  document_title?: string | null;
  modality?: "image" | "document" | "text" | "audio" | "ui" | "experiment";
  evidence_type?: string;
  page_label?: string;
  chunk_index?: number | null;
  section?: string | null;
  score?: number;
  confidence?: number;
  bbox?: NormalizedImageRegion;
  snippet?: string;
  visual_assets?: Array<{
    asset_id?: string;
    page?: string;
    caption: string;
    source_locator?: string;
    vision_status?: string;
  }>;
  visual_asset_captions?: string[];
  breakdown?: Record<string, number>;
  support_score?: number | null;
  support_summary?: string;
  support_terms?: string[];
  support_method?: string;
  metric?: string;
  unit?: string;
  split?: string;
  definition_version?: string;
  baseline_value?: number | null;
  current_value?: number | null;
  absolute_delta?: number | null;
  absolute_delta_unit?: string;
  relative_delta?: number | null;
  assessment?: string;
  baseline_source?: { file_ref?: string; source_locator?: string };
  current_source?: { file_ref?: string; source_locator?: string };
}

interface AuditEvent {
  event_id: string;
  trace_id: string;
  event_type: string;
  timestamp: number;
  previous_hash: string;
  event_hash: string;
  payload: Record<string, unknown>;
}

interface HealthStatus {
  sidecar: "BOOTING" | "OK" | "OFFLINE";
  ready: boolean;
  reason?: string;
}

interface StorageStatus {
  degraded?: boolean;
  file_archive?: { status?: string };
  redis?: { status?: string; vector_index_status?: string; session_cache_count?: number; cached_message_count?: number };
  postgres?: { status?: string; session_count?: number; message_count?: number; summary_count?: number; memory_count?: number };
  outbox?: { pending?: number };
}

interface RuntimeMetrics {
  uptime_seconds: number;
  total_requests: number;
  failed_requests: number;
  chat_requests: number;
  chat_failures: number;
  chat_abandoned?: number;
  chat_completed?: number;
  chat_under_5s_rate?: number;
  last_chat_first_token_ms?: number;
  last_chat_total_ms?: number;
  average_latency_ms: number;
  last_error?: string | null;
}

interface DocumentItem {
  name: string;
  document_id?: string;
  relative_path: string;
  size_bytes: number;
  modified_at: number;
  indexed: boolean;
  index_status?: "indexed" | "failed" | "processing" | "pending" | "changed";
  index_present?: boolean;
  source_changed?: boolean;
  index_failure?: {
    error_code?: string;
    category?: string;
    phase?: "parse" | "read" | "runtime" | "index" | string;
    reason?: string;
  } | null;
  index_observation?: {
    elapsed_ms?: number;
    parse_ms?: number;
    ingest_ms?: number;
    graph_ms?: number;
    chunk_count?: number;
    modality?: string;
    pages?: number;
    ocr_ms?: number;
    failed_page_count?: number;
  } | null;
  media_type?: string;
  scope?: "global" | "project" | "ambiguous";
  project_id?: string | null;
  project_ids?: string[];
}

interface ActiveDocumentRef {
  name: string;
  document_id?: string;
}

function shouldPollPresentationTask(task: PresentationTask, resumeLease: number | undefined, polls: number) {
  return ["pending", "running"].includes(task.status) || (
    resumeLease !== undefined && task.lease_epoch === resumeLease && polls < 10 && ["failed", "interrupted"].includes(task.status)
  );
}

function isPresentationDocumentInProject(document: DocumentItem, projectId: string | null) {
  if (!projectId) return false;
  if (document.scope === "global") return true;
  return document.scope === "project" && (document.project_id === projectId || Boolean(document.project_ids?.includes(projectId)));
}

interface DocumentLibrary {
  ready: boolean;
  document_count: number;
  indexed_count: number;
  workspace: string;
  documents: DocumentItem[];
}

interface IndexPreviewItem {
  name: string;
  relative_path?: string;
  index_status?: string;
  index_freshness?: string;
  index_present?: boolean;
  source_changed?: boolean;
}

interface IndexPreview {
  schema_version: string;
  read_only: boolean;
  indexing_status?: string;
  counts: {
    added: number;
    modified: number;
    deleted: number;
    unchanged: number;
    unverified: number;
    blocked: number;
  };
  added: IndexPreviewItem[];
  modified: IndexPreviewItem[];
  deleted: IndexPreviewItem[];
  unchanged: IndexPreviewItem[];
  unverified: IndexPreviewItem[];
  blocked: IndexPreviewItem[];
}

interface PresentationStyleSkill {
  skill_id: string;
  title: string;
  category: string;
  scope?: "global" | "project";
  project_id?: string | null;
  source_path?: string;
  presentation_style?: Record<string, string>;
  presentation_style_source?: string;
  resource_manifest?: SkillResource[];
  applicability?: string;
  content?: string;
  triggers?: string[];
  tags?: string[];
  target?: "conversation" | "presentation";
  compatibility_status?: string;
}

interface SkillResource {
  path: string;
  kind: "asset" | "instruction" | "resource" | "script" | "style" | string;
  size_bytes?: number;
  execution?: string;
}

interface LearningSkill {
  skill_id: string;
  title: string;
  category: string;
  scope?: "global" | "project";
  project_id?: string | null;
  content: string;
  presentation_style?: Record<string, string>;
  presentation_style_source?: string;
  resource_manifest?: SkillResource[];
  required_inputs?: string[];
  success_criteria?: string[];
  source_path?: string;
  applicability?: string;
  target?: "conversation" | "presentation";
  compatibility_status?: string;
  adapter_path?: string;
}

interface SkillInboxItem {
  skill_name: string;
  skill_title: string;
  description: string;
  source: string;
  files: string[];
  target: "conversation" | "presentation";
  replayed?: boolean;
  warning?: string;
}

interface SkillAdapterReview {
  skill_name: string;
  skill_title?: string;
  target?: "conversation" | "presentation";
  compatibility_status?: string;
  adapter?: Record<string, unknown>;
  path?: string;
  replayed?: boolean;
  warning?: string;
}

function uniqueSkillsById<T extends { skill_id: string; title: string; content?: string }>(skills: T[]) {
  const seen = new Set<string>();
  return skills.filter((skill) => {
    const content = skill.content?.trim();
    const key = skill.skill_id.trim() || `title:${skill.title.trim().toLowerCase()}`;
    const contentKey = content ? `content:${content}` : key;
    if (seen.has(key) || seen.has(contentKey)) return false;
    seen.add(key);
    seen.add(contentKey);
    return true;
  });
}

function isLearningStorageSyncDegraded(payload: { storage_sync?: { status?: string } } | null | undefined) {
  return payload?.storage_sync?.status === "degraded";
}

function isPresentationSkill(skill: PresentationStyleSkill) {
  return skill.target === "presentation" && skill.compatibility_status === "verified";
}

function compactSkillDescription(skill: LearningSkill) {
  const title = skill.title.trim().toLowerCase();
  if (title === "pptx") return "处理 PPTX/POTX 演示文稿。";
  if (title === "test-driven-development") return "按测试驱动方式实现和修复功能。";
  const source = (skill.applicability || "").replace(/[#*_`>]/g, "").replace(/\s+/g, " ").trim();
  return source ? `${source.slice(0, 96)}${source.length > 96 ? "…" : ""}` : "已安装的项目 Skill，可在主对话框手动调用。";
}

function compactSkillResources(skill: LearningSkill) {
  const resources = skill.resource_manifest || [];
  const styleCount = resources.filter((resource) => resource.kind === "style").length;
  const labels = [];
  labels.push(skill.target === "presentation" ? "PPT Skill" : "对话 Skill");
  if (skill.compatibility_status === "verified") labels.push("已自动适配并验证");
  if (styleCount || skill.presentation_style_source) labels.push("已接入声明式样式");
  return labels.join(" · ");
}

function resolveLocalSkillFile(rootPath: string, sourcePath: string) {
  const source = sourcePath.trim().replace(/\\/g, "/");
  if (!source) return "";
  if (/^[A-Za-z]:\//.test(source) || source.startsWith("/")) return source;
  return `${rootPath.replace(/[\\/]+$/, "")}/${source.replace(/^\/+/, "")}`;
}

function compactPresentationSkillDescription(skill: PresentationStyleSkill) {
  const title = skill.title.trim().toLowerCase();
  if (title.includes("ppt")) return "用于 PPT 版式与表达风格。";
  if (title.includes("template")) return "作为汇报生成的默认风格模板。";
  return `演示风格模板 · ${skill.category}`;
}

interface DocumentPreview {
  name: string;
  document_id?: string;
  media_type?: string;
  text: string;
  chunks?: DocumentPreviewChunk[];
  preview_source?: "indexed_clean_text" | "source_extraction";
  truncated?: boolean;
}

interface DocumentPreviewChunk {
  id: string;
  index: number;
  page?: number | string | null;
  section: string;
  content_kind: string;
  child_count: number;
  text: string;
}

interface RetrievalTestChunk {
  id: string;
  candidate_id?: string | null;
  source_filename: string;
  document_title?: string | null;
  page?: number | string | null;
  section: string;
  section_path?: string[];
  chunk_index?: number | null;
  content_kind: string;
  score?: number | null;
  breakdown?: Record<string, number>;
  support_score?: number | null;
  support_summary?: string;
  support_terms?: string[];
  section_route_mode?: string | null;
  section_route_coverage?: number | null;
  section_route_source?: string | null;
  section_node_id?: string | null;
  section_parent_node_id?: string | null;
  section_node_depth?: number | null;
  section_node_title?: string | null;
  section_node_summary?: string | null;
  section_node_child_count?: number | null;
  content: string;
  truncated?: boolean;
}

interface RetrievalFeedback {
  feedback_id: string;
  scope: "global" | "project";
  project_id?: string | null;
  workflow_id?: MultimodalWorkflowId | null;
  corrected_query: string;
  created_at?: number;
  updated_at?: number;
}

interface RetrievalTestResult {
  schema_version: string;
  query: string;
  project_id?: string | null;
  workflow_id?: MultimodalWorkflowId | null;
  top_k: number;
  elapsed_ms: number;
  retrieval_mode?: string | null;
  project_route?: string | null;
  query_variants: string[];
  feedback?: RetrievalFeedback[];
  chunks: RetrievalTestChunk[];
  observability?: {
    stage_counts?: Record<string, unknown>;
    document_scope?: {
      mode?: string;
      requested_name_count?: number;
      requested_id_count?: number;
      graph_blocked?: boolean;
    };
    ranking?: Record<string, unknown>;
    section_route?: Array<Record<string, unknown>>;
    project_focus?: Record<string, unknown>;
    quality_signals?: Record<string, unknown>;
    sufficiency?: Record<string, unknown>;
    timings_ms?: Record<string, unknown>;
  };
}

interface PresentationSourceRef {
  name: string;
  document_id?: string;
}

type ProjectMode = "research" | "business";

type MultimodalWorkflowId = "paper_review" | "experiment_compare" | "meeting_actions";

interface MultimodalWorkflowSpec {
  workflow_id: MultimodalWorkflowId;
  playbook_version: string;
  title: string;
  audience: string;
  description: string;
  input_contract: string[];
  input_labels: string[];
  output: string;
  response_contract?: {
    required_sections: string[];
    evidence_rules: string[];
    missing_evidence_marker: string;
  };
  evidence_plan: string[];
  side_effects: string[];
  confirmation_points: string[];
  suggested_query: string;
  approval: {
    status: "approved" | "revoked" | "stale" | "unreviewed";
    playbook_version: string;
    reviewed_at?: string;
  };
}

interface MultimodalWorkflowPlan {
  workflow_id: MultimodalWorkflowId;
  playbook_id: MultimodalWorkflowId;
  playbook_version: string;
  title: string;
  ready: boolean;
  missing_inputs: string[];
  input_contract: string[];
  input_state: {
    project_selected: boolean;
    document_count: number;
    has_audio: boolean;
    has_image: boolean;
    has_transcript: boolean;
    has_text_note?: boolean;
    has_selected_text: boolean;
    has_experiment_comparison: boolean;
  };
  routing: {
    visual_mode: "none" | "auto" | "fast" | "slow";
    needs_rag: boolean;
    needs_confirmation: boolean;
  };
  evidence_scope: string[];
  output: string;
  response_contract?: {
    required_sections: string[];
    evidence_rules: string[];
    missing_evidence_marker: string;
  };
  evidence_plan: string[];
  side_effects: string[];
  confirmation_points: string[];
  suggested_query: string;
  next_action: string;
  approval: {
    status: "approved" | "revoked" | "stale" | "unreviewed";
    playbook_version: string;
    reviewed_at?: string;
  };
}

interface ProjectSummary {
  project_id: string;
  name: string;
  status: string;
  project_mode: ProjectMode;
  asset_count: number;
  experiment_count: number;
  decision_count: number;
  task_count: number;
  root_path?: string | null;
  instructions?: string;
  memory_policy?: "project_plus_global" | "project_only";
}

interface ProjectOverview {
  project: ProjectSummary;
  recent_runs: Array<{ experiment_name: string; performed_at: string; dataset_version: string; evidence_title?: string | null }>;
  tasks: Array<{ title: string; due_at?: string | null; status: string }>;
  decisions: Array<{ statement: string; decided_at: string }>;
}

interface ExperimentRun {
  run_id: string;
  experiment_name: string;
  performed_at: string;
  dataset_version: string;
  config_ref?: string;
  evaluation_script_ref?: string;
  measurements?: Array<{ name: string; value: number; unit: string; split: string }>;
}

interface ProjectSession {
  session_id: string;
  project_id: string;
  title: string;
  workflow_id?: MultimodalWorkflowId | null;
  created_at: number;
  updated_at: number;
  latest_sequence: number;
}

interface DocumentImportResponse {
  imported_count: number;
  scope?: "global" | "project";
  project_id?: string | null;
  reindex_required?: boolean;
  items: Array<{
    name: string;
    status: "imported" | "already_present" | "rejected";
    error_code?: string;
    media_type?: string;
    scope?: "global" | "project";
    project_id?: string | null;
  }>;
}

interface SecurityStatus {
  crypto_initialized: boolean;
  software_unlocked: boolean;
  hardware_mounted: boolean;
  reason?: string;
  no_recovery: boolean;
  development_bypass?: boolean;
}

interface ProductDiagnostics {
  ready: boolean;
  checks: Record<string, boolean>;
  models?: {
    ready: boolean;
    missing: string[];
    runtime?: {
      chat_model?: {
        selected?: string;
        provider?: string;
        mode?: string;
        ready?: boolean;
        missing_parts?: string[];
      };
      reranker?: {
        selected?: string | null;
        mode?: string;
      };
      config?: Record<string, unknown>;
    };
  };
  policy?: {
    organization: string;
    require_signed_license: boolean;
    allow_lite_mode: boolean;
  };
  license?: {
    valid: boolean;
    level: string;
    reason: string;
  };
  audit?: {
    ready: boolean;
    event_count: number;
    broken_at?: { line: number; reason: string } | null;
  };
  workspace?: {
    ready: boolean;
    disk?: {
      ready: boolean;
      free_bytes?: number;
      minimum_free_bytes?: number;
    };
  };
}

interface GraphRagDiagnostics {
  ready: boolean;
  status?: "not_built" | "building" | "failed" | "degraded" | "disabled" | "ready" | "locked";
  reason?: string;
  error?: string;
  project_id?: string | null;
  indexing_status?: string;
  observation_stale?: boolean;
  algorithm?: string;
  document_count?: number;
  entity_count?: number;
  relationship_count?: number;
  community_count?: number;
  target_range_ok?: boolean;
  memory_policy?: string;
  communities?: Array<{
    community_id?: string;
    title?: string;
    entity_count?: number;
    source_count?: number;
    entity_names?: string[];
    source_filenames?: string[];
    report?: string;
    report_method?: string;
  }>;
  built_at?: number;
}

interface BackupPreflight {
  schema_version: string;
  read_only: boolean;
  ready: boolean;
  decision: "allow" | "block";
  indexing_status?: string;
  indexer_worker_status?: "active" | "inactive" | "unverified";
  blockers: Array<{ code: string; message: string }>;
  task_journal?: { status?: string; active_count?: number };
}

interface DiagnosticSummary {
  readyCount: number;
  totalCount: number;
  warnings: string[];
  primaryHint: string;
}

interface ParsedMessage {
  cleanContent: string;
  artifacts: MindmapArtifact[];
  evidence: EvidenceItem[];
}

interface ReflectionCandidate {
  candidate_id: string;
  asset_type: "memory" | "skill";
  subtype: string;
  title: string;
  content: string;
  evidence: string[];
  rationale: string;
  confidence: number;
  selected_by_default: boolean;
}

interface ReflectionReview {
  review_id: string;
  status: "pending" | "completed" | "discarded";
  candidates: ReflectionCandidate[];
  warning?: string;
}

interface RuntimeMemoryNotice {
  memory_id: string;
  text: string;
  project_id?: string | null;
}

interface LearningMemory {
  memory_id: string;
  subtype: string;
  status: "active" | "archived" | "deleted";
  scope: "global" | "project";
  project_id?: string | null;
  title: string;
  content: string;
  source_excerpt?: string;
  source_trace?: {
    candidate_id?: string;
    message_indexes?: number[];
    excerpt?: string;
  };
  confirmation_status?: "human_reviewed" | "explicit_signal" | "legacy" | string;
  confirmed_at?: number | null;
  last_used_at?: number | null;
  valid_from?: number | null;
  invalid_at?: number | null;
  validity_status?: "active_now" | "scheduled" | "expired" | string;
  record_version?: number;
  created_at?: number;
  updated_at?: number;
}

interface MemoryHealth {
  status: "ok" | "degraded" | "error" | string;
  counts: {
    active_memories: number;
    active_memory_chunks: number;
    active_memory_versions: number;
    pending_reviews: number;
    pending_syncs: number;
    retrying_syncs: number;
    retry_blocked_syncs: number;
  };
  issue_count: number;
  truncated?: boolean;
}

interface PresentationTask {
  task_id: string;
  kind?: "presentation.plan" | "presentation.generate" | string;
  phase?: "outline_review" | "generation" | string;
  request_id?: string | null;
  project_id?: string | null;
  source_filenames?: string[];
  source_document_ids?: string[];
  plan_task_id?: string | null;
  plan_digest?: string | null;
  status: "pending" | "running" | "interrupted" | "completed" | "failed" | "cancelled";
  replayed?: boolean;
  worker_started?: boolean;
  lease_epoch?: number;
  current_step?: string | null;
  error?: string | null;
  recoverable?: boolean;
  cancellation_pending?: boolean;
  repetition_guard?: {
    decision: "allow" | "warn" | string;
    matches?: Array<{
      task_id: string;
      status: string;
      current_step?: string | null;
      attempt_count?: number;
    }>;
  };
  progress: { completed: number; total: number };
  steps?: Array<{
    step_name: string;
    status: string;
    attempt_count?: number;
    error?: string | null;
    attempt_history?: Array<{
      attempt: number;
      status: string;
      started_at?: number | null;
      completed_at?: number | null;
      error?: string | null;
    }>;
  }>;
  result?: {
    phase?: "outline_review" | string;
    request_id?: string | null;
    plan_task_id?: string | null;
    plan_digest?: string | null;
    outline?: {
      title?: string;
      slides?: Array<{
        kind?: string;
        title?: string;
        subtitle?: string;
        points?: string[];
        sources?: string[];
      }>;
    };
    output_path?: string;
    slide_count?: number;
    renderer?: string;
    opened_in_wps?: boolean;
    source_count?: number;
    source_version_ids?: string[];
    artifact_id?: string | null;
    outline_source?: "local_ollama" | "extractive" | "custom_generator" | string;
    evidence_validation?: { status?: string; notice?: string };
    skill_application?: {
      skill_id?: string | null;
      skill_title?: string | null;
      instructions_applied?: boolean;
      style_applied?: boolean;
      style_source?: string | null;
      script_count?: number;
    };
    evidence_metrics?: PresentationEvidenceMetrics;
    elapsed_ms?: number;
    evidence_snapshot_available?: boolean;
  } | null;
}

interface PresentationEvidenceMetrics {
  evidence_slide_coverage: number;
  selected_source_coverage: number;
  selected_label_coverage?: number;
  selected_identity_count?: number;
  cited_identity_count?: number;
  source_identity_coverage_status?: "exact_object_ids" | "ambiguous_display_labels" | "display_name_only" | string;
  evidence_bound_point_count: number;
}

function formatPresentationEvidenceCoverage(metrics: PresentationEvidenceMetrics) {
  if (metrics.source_identity_coverage_status === "display_name_only") {
    return `来源标签 ${Math.round((metrics.selected_label_coverage ?? metrics.selected_source_coverage) * 100)}%`;
  }
  if (metrics.selected_identity_count != null && metrics.cited_identity_count != null) {
    const warning = metrics.source_identity_coverage_status === "ambiguous_display_labels"
      ? "（同名展示标签无法精确归因）"
      : "";
    return `来源对象 ${metrics.cited_identity_count}/${metrics.selected_identity_count}${warning}`;
  }
  return `来源 ${Math.round(metrics.selected_source_coverage * 100)}%`;
}

interface MultimodalDecision {
  intent: string;
  target_id?: string | null;
  conflict_type: string;
  clarification_required: boolean;
  action: string;
  confidence: number;
  latency_ms: { total: number };
  clarification?: {
    required: boolean;
    reason_codes: string[];
    prompt: string;
    target_candidates: string[];
  };
}

interface UiAutomationSnapshot {
  schema_version: string;
  status: "pass" | "unavailable" | "redacted";
  source: "windows_uia" | string;
  captured_at_ms: number;
  element?: {
    target_id: string;
    target_kind: string;
    control_type_id: number;
    name?: string | null;
    selected_text?: string | null;
    automation_id?: string | null;
    class_name?: string | null;
    framework_id?: string | null;
    process_id: number;
    native_window_handle?: number | null;
    is_focused: boolean;
    is_password: boolean;
    is_enabled: boolean;
    is_offscreen: boolean;
    screen_bbox?: { left: number; top: number; right: number; bottom: number } | null;
  } | null;
  reason?: string | null;
}

interface UiAutomationEvidenceEvent {
  event_id: string;
  modality: "ui_automation";
  timestamp_ms: number;
  start_ms?: number;
  end_ms?: number;
  project_id?: string;
  event_time_basis?: "capture_relative_ms" | "unix_epoch_ms";
  event_time_origin_ms?: number | null;
  text?: string;
  selected_text?: string;
  source: "windows_uia";
  confidence: number;
  target_id: string;
  target_kind: string;
  window_id?: string;
  is_focused: boolean;
  explicit: true;
  target_candidates: Array<{
    target_id: string;
    target_kind: string;
    text?: string;
    confidence: number;
    source: "windows_uia";
    is_focused: boolean;
    selected: true;
    explicit: true;
  }>;
}

interface MultimodalRuntimeResult {
  status: "pass" | "partial" | "blocked" | "invalid";
  speech_text: string;
  target_grounding?: boolean;
  stages: Record<string, { status: string; latency_ms?: number; text?: string; code?: string; detail?: string; route?: string; reason?: string }>;
  decision?: MultimodalDecision | null;
  diagnostics?: Array<{ stage: string; code: string; detail: string }>;
  latency_ms?: { total: number };
  event_time_basis?: "capture_relative_ms" | "unix_epoch_ms";
  event_time_origin_ms?: number | null;
  perception_events?: Array<{
    event_id: string;
    modality: "image" | "audio" | "image_region" | "ui" | "screen" | "ui_automation" | "ui_selection" | "ocr_region" | "text" | "document";
    timestamp_ms: number;
    start_ms?: number;
    end_ms?: number;
    text?: string;
    selected_text?: string;
    project_id?: string;
    source_name?: string;
    source?: string;
    event_time_basis?: "capture_relative_ms" | "unix_epoch_ms";
    event_time_origin_ms?: number | null;
    confidence?: number;
    bbox?: NormalizedImageRegion;
    focus_point?: { x: number; y: number };
    target_id?: string;
    target_kind?: string;
    window_id?: string;
    window_title?: string;
    is_focused?: boolean;
    explicit?: boolean;
    target_candidates?: Array<{
      target_id: string;
      target_kind?: string;
      text?: string;
      confidence?: number;
      bbox?: NormalizedImageRegion;
      source?: string;
      focus_point?: { x: number; y: number };
      is_focused?: boolean;
      selected?: boolean;
      explicit?: boolean;
    }>;
  }>;
  visual_context?: {
    input_scope?: "full_image" | "selected_region";
    selected_region?: NormalizedImageRegion;
    visual_route?: string;
    visual_reason?: string;
    original_size?: { width: number; height: number };
    model_input_size?: { width: number; height: number };
    crop_scale?: number;
  } | null;
}

interface MultimodalWarmupResult {
  status: "pass" | "blocked" | "failed";
  already_loaded?: boolean;
  latency_ms?: { total: number };
  code?: string;
  detail?: string;
}

interface MultimodalPerceptionWarmupResult extends MultimodalWarmupResult {
  stages?: Record<string, {
    status: string;
    loaded?: boolean;
    already_loaded?: boolean;
    detail?: string;
  }>;
}

interface ReviewDraftState {
  review: ReflectionReview | null;
  selectedIds: Set<string>;
  edits: Record<string, { title: string; content: string }>;
  mode: "idle" | "drafting" | "reviewing" | "submitting" | "done" | "error";
  message: string;
}

interface DraftArtifact {
  artifact_id: string;
  project_id: string | null;
  artifact_type: "email_draft" | "schedule_draft";
  confirmation_status: "draft" | "confirmed" | "cancelled";
  plan: {
    recipient?: { display_name: string; email: string };
    subject?: string;
    body?: string;
    attachment_refs?: string[];
    title?: string;
    start_at?: string;
    duration_minutes?: number;
    participant?: { display_name: string; email: string } | null;
    notes?: string;
    reminder_sound_enabled?: boolean;
    side_effect: "none";
    requires_user_confirmation: boolean;
  };
}

interface ProjectArtifactSummary extends DraftArtifact {
  created_at?: string;
  change_summary?: string;
}

interface LocalContact {
  contact_id: string;
  display_name: string;
  aliases: string[];
  email: string;
  project_ids: string[];
}

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const GRAPH_WHEEL_STEP = 0.006;
const GRAPH_BUTTON_ZOOM_STEP = 0.12;
const WORKBENCH_LAYOUT_STORAGE_KEY = "souldrive.workbench.layout.v1";
const SMTP_PREFERENCE_STORAGE_KEY = "souldrive.smtp.preference.v1";
const SMTP_ACCOUNTS_STORAGE_KEY = "souldrive.smtp.accounts.v1";
const SMTP_DEFAULT_SENDER_STORAGE_KEY = "souldrive.smtp.default-sender.v1";
const DEFAULT_ARTIFACT_WEIGHT = 1.25;
const MIN_ARTIFACT_WEIGHT = 0.72;
const MAX_ARTIFACT_WEIGHT = 2.8;
const GRAPH_PRIORITY_ARTIFACT_WEIGHT = 2.25;
const MAX_AUDIO_RECORDING_MS = 15_000;
const CHAT_RESPONSE_IDLE_TIMEOUT_MS = 90_000;
const UI_AUTOMATION_CONTEXT_TTL_MS = 30_000;

class ChatHttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail?: string,
  ) {
    super(`chat request failed with status ${status}`);
  }
}

async function sidecarIsReachable(apiBaseUrl: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 3_000);
  try {
    return (await fetch(`${apiBaseUrl}/health`, { signal: controller.signal })).ok;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timeout);
  }
}

interface WorkbenchLayoutPreference {
  leftCollapsed: boolean;
  artifactWeight: number;
}

interface SmtpPreference {
  provider: "qq" | "netease163";
  sender: string;
}

const stageLabels: Record<string, string> = {
  asr: "语音识别",
  ocr: "文字识别",
  visual_route: "视觉路径",
  vlm: "视觉理解",
  soulalign: "意图融合",
};

const statusLabels: Record<string, string> = {
  pass: "完成",
  partial: "部分完成",
  blocked: "未执行",
  skipped: "跳过",
  failed: "失败",
};

const taskStepStatusLabels: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
};

function formatTaskStepStatus(status: string) {
  return taskStepStatusLabels[status] || status;
}

const multimodalIntentLabels: Record<string, string> = {
  selection_summary: "选区摘要",
  direct_answer: "直接回答",
  clarify_target: "确认目标",
  multimodal_grounding: "多模态对齐",
};

function formatMultimodalIntent(intent: string) {
  return multimodalIntentLabels[intent] || intent.replace(/_/g, " ");
}

function freshUiAutomationEvidence(
  snapshot: UiAutomationSnapshot | null,
  nowMs: number,
): UiAutomationEvidenceEvent | null {
  const element = snapshot?.element;
  if (!snapshot || !element || !["pass", "redacted"].includes(snapshot.status)) return null;
  const ageMs = nowMs - snapshot.captured_at_ms;
  if (ageMs < -5_000 || ageMs > UI_AUTOMATION_CONTEXT_TTL_MS) return null;
  const confidence = snapshot.status === "pass" ? 0.99 : 0.92;
  const candidate = {
    target_id: element.target_id,
    target_kind: element.target_kind || "unknown",
    ...(element.name ? { text: element.name } : {}),
    confidence,
    source: "windows_uia" as const,
    is_focused: element.is_focused,
    selected: true as const,
    explicit: true as const,
  };
  return {
    event_id: `uia-${snapshot.captured_at_ms}`,
    modality: "ui_automation",
    timestamp_ms: snapshot.captured_at_ms,
    start_ms: snapshot.captured_at_ms,
    end_ms: snapshot.captured_at_ms,
    event_time_basis: "unix_epoch_ms",
    ...(element.name ? { text: element.name } : {}),
    ...(element.selected_text ? { selected_text: element.selected_text } : {}),
    source: "windows_uia",
    confidence,
    target_id: element.target_id,
    target_kind: element.target_kind || "unknown",
    ...(element.native_window_handle ? { window_id: String(element.native_window_handle) } : {}),
    is_focused: element.is_focused,
    explicit: true,
    target_candidates: [candidate],
  };
}

function clampArtifactWeight(value: number) {
  if (!Number.isFinite(value)) return DEFAULT_ARTIFACT_WEIGHT;
  return Math.min(MAX_ARTIFACT_WEIGHT, Math.max(MIN_ARTIFACT_WEIGHT, value));
}

function readWorkbenchLayoutPreference(): WorkbenchLayoutPreference {
  if (typeof window === "undefined") {
    return { leftCollapsed: false, artifactWeight: DEFAULT_ARTIFACT_WEIGHT };
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(WORKBENCH_LAYOUT_STORAGE_KEY) || "{}");
    return {
      leftCollapsed: Boolean(parsed.leftCollapsed),
      artifactWeight: clampArtifactWeight(Number(parsed.artifactWeight) || DEFAULT_ARTIFACT_WEIGHT),
    };
  } catch {
    return { leftCollapsed: false, artifactWeight: DEFAULT_ARTIFACT_WEIGHT };
  }
}

function saveWorkbenchLayoutPreference(preference: WorkbenchLayoutPreference) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    WORKBENCH_LAYOUT_STORAGE_KEY,
    JSON.stringify({
      leftCollapsed: preference.leftCollapsed,
      artifactWeight: clampArtifactWeight(preference.artifactWeight),
    }),
  );
}

function readSmtpPreference(): SmtpPreference {
  if (typeof window === "undefined") return { provider: "qq", sender: "" };
  try {
    const parsed = JSON.parse(window.localStorage.getItem(SMTP_PREFERENCE_STORAGE_KEY) || "{}");
    return {
      provider: parsed.provider === "netease163" ? "netease163" : "qq",
      sender: typeof parsed.sender === "string" ? parsed.sender : "",
    };
  } catch {
    return { provider: "qq", sender: "" };
  }
}

function saveSmtpPreference(preference: SmtpPreference) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(SMTP_PREFERENCE_STORAGE_KEY, JSON.stringify(preference));
}

function readSmtpAccounts(): SmtpPreference[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(SMTP_ACCOUNTS_STORAGE_KEY) || "[]");
    const accounts = Array.isArray(parsed) ? parsed : [];
    const legacy = readSmtpPreference();
    const merged = [...accounts, ...(legacy.sender ? [legacy] : [])];
    return merged.filter((item, index, all) => item?.sender && all.findIndex((candidate) => candidate.sender === item.sender) === index);
  } catch {
    return [];
  }
}

function parseEvidence(raw: string): EvidenceItem[] {
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function messageArtifactBlocks(content: string) {
  const blocks: Array<{ kind: string; body: string; start: number; end: number }> = [];
  let open: { marker: string; kind: string; start: number; bodyStart: number } | null = null;
  let offset = 0;
  // Only column-zero protocol fences are artifacts. Quotes/indented examples
  // remain source text; an outer literal fence consumes its nested fences.
  for (const line of content.match(/[^\n]*\n|[^\n]+$/g) || []) {
    const fence = /^([ ]{0,3})(`{3,}|~{3,})([^\r\n]*)\r?\n?$/.exec(line);
    if (fence && open) {
      if (fence[2][0] === open.marker[0] && fence[2].length >= open.marker.length && !fence[3].trim()) {
        if (open.kind === "souldrive-evidence" || open.kind === "souldrive-mindmap") {
          blocks.push({ kind: open.kind, body: content.slice(open.bodyStart, offset).trim(), start: open.start, end: offset + line.length });
        }
        open = null;
      }
    } else if (fence && !(fence[2][0] === "`" && fence[3].includes("`"))) {
      open = { marker: fence[2], kind: fence[1] ? "literal" : fence[3].trim(), start: offset, bodyStart: offset + line.length };
    }
    offset += line.length;
  }
  return blocks;
}

function parseMessageArtifacts(content: string): ParsedMessage {
  const blocks = messageArtifactBlocks(content);
  const mindmaps = blocks.filter((block) => block.kind === "souldrive-mindmap");
  // The backend appends its evidence snapshot last. Earlier model/source
  // examples must not be merged into the snapshot's citation namespace.
  const terminalEvidence = blocks.filter((block) => block.kind === "souldrive-evidence" && !content.slice(block.end).trim()).slice(-1);
  let cleanContent = content;
  for (const block of [...mindmaps, ...terminalEvidence].sort((left, right) => right.start - left.start)) {
    cleanContent = cleanContent.slice(0, block.start) + cleanContent.slice(block.end);
  }
  return { cleanContent: cleanContent.trim(), artifacts: mindmaps.map((block) => parseMindmap(block.body)),
           evidence: terminalEvidence.flatMap((block) => parseEvidence(block.body)) };
}

function formatAuditTime(timestamp?: number) {
  if (!timestamp) return "--:--:--";
  return new Date(timestamp * 1000).toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatBytes(value?: number) {
  if (!Number.isFinite(value)) return "UNKNOWN";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, Number(value));
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size >= 10 || unitIndex === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unitIndex]}`;
}

function formatDocumentTime(timestamp?: number) {
  if (!timestamp) return "--";
  return new Date(timestamp * 1000).toLocaleDateString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
  });
}

function documentIndexStatusLabel(document: DocumentItem) {
  switch (document.index_status) {
    case "failed": return "索引失败";
    case "processing": return "索引中";
    case "changed": return "原文已变化";
    case "indexed": return "已索引";
    default: return document.indexed ? "已索引" : "待索引";
  }
}

function documentIndexStatusClass(document: DocumentItem) {
  switch (document.index_status) {
    case "failed": return "failed";
    case "processing": return "processing";
    case "changed": return "changed";
    case "indexed": return "indexed";
    default: return document.indexed ? "indexed" : "pending";
  }
}

function documentIndexDiagnostic(document: DocumentItem) {
  if (document.index_status === "failed") {
    const phase = document.index_failure?.phase;
    const phaseLabel = phase === "parse" ? "解析" : phase === "read" ? "读取" : phase === "runtime" ? "运行" : "入库";
    return `${phaseLabel}：${document.index_failure?.reason || "索引失败"}`;
  }
  if (document.index_status === "processing") return "正在处理当前文档…";
  if (document.index_status === "changed") return "原文件内容已变化，请重新启动索引";
  if (document.index_status === "pending") return "等待手动启动索引";
  const observation = document.index_observation;
  if (observation?.chunk_count !== undefined) return `已生成 ${observation.chunk_count} 个父切片`;
  return "";
}

function formatScheduleStartTime(value?: string) {
  if (!value) return "未设置";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "时间无效";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatScheduleCreatedAt(value?: string) {
  if (!value) return "创建时间未知";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "创建时间无效";
  return `创建于 ${date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}`;
}

function scheduleInputValue(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function isImageDocumentName(name?: string | null) {
  return Boolean(name && /\.(png|jpe?g|webp)$/i.test(name));
}

function summarizeDiagnostics(diagnostics: ProductDiagnostics | null, health: HealthStatus): DiagnosticSummary {
  const checks = [
    health.sidecar === "OK",
    Boolean(diagnostics?.models?.ready),
    Boolean(diagnostics?.audit?.ready),
    Boolean(diagnostics?.workspace?.ready),
    diagnostics?.policy?.require_signed_license ? Boolean(diagnostics?.license?.valid) : true,
  ];
  const warnings: string[] = [];
  if (health.sidecar !== "OK") warnings.push("SIDECAR_OFFLINE");
  if (diagnostics?.models && !diagnostics.models.ready) warnings.push(`MODEL_MISSING:${diagnostics.models.missing.length}`);
  if (diagnostics?.audit && !diagnostics.audit.ready) warnings.push("AUDIT_CHAIN");
  if (diagnostics?.workspace?.disk && !diagnostics.workspace.disk.ready) warnings.push("LOW_DISK");
  if (diagnostics?.policy?.require_signed_license && !diagnostics?.license?.valid) warnings.push("LICENSE_REQUIRED");

  const readyCount = checks.filter(Boolean).length;
  const primaryHint = warnings[0] || (health.ready ? "PRODUCT_READY" : "WAITING_FOR_AUTHORIZED_STORAGE");
  return {
    readyCount,
    totalCount: checks.length,
    warnings,
    primaryHint,
  };
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [healthStatus, setHealthStatus] = useState<HealthStatus>({ sidecar: "BOOTING", ready: false });
  const [storageStatus, setStorageStatus] = useState<StorageStatus | null>(null);
  const [runtimeMetrics, setRuntimeMetrics] = useState<RuntimeMetrics | null>(null);
  const [productDiagnostics, setProductDiagnostics] = useState<ProductDiagnostics | null>(null);
  const [graphRagDiagnostics, setGraphRagDiagnostics] = useState<GraphRagDiagnostics | null>(null);
  const [backupPreflight, setBackupPreflight] = useState<BackupPreflight | null>(null);
  const [documentLibrary, setDocumentLibrary] = useState<DocumentLibrary | null>(null);
  const [indexPreview, setIndexPreview] = useState<IndexPreview | null>(null);
  const [indexPreviewBusy, setIndexPreviewBusy] = useState(false);
  const [indexPreviewMessage, setIndexPreviewMessage] = useState("");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectListUnavailable, setProjectListUnavailable] = useState(false);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [projectSessions, setProjectSessions] = useState<ProjectSession[]>([]);
  const [showAllProjectSessions, setShowAllProjectSessions] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionMessage, setSessionMessage] = useState("");
  const [projectCreateOpen, setProjectCreateOpen] = useState(false);
  const [projectCreateName, setProjectCreateName] = useState("");
  const [projectCreateMode, setProjectCreateMode] = useState<ProjectMode>("research");
  const [projectCreateRoot, setProjectCreateRoot] = useState("");
  const [projectCreateBusy, setProjectCreateBusy] = useState(false);
  const [projectCreateMessage, setProjectCreateMessage] = useState("");
  const [projectDeleteBusy, setProjectDeleteBusy] = useState(false);
  const [projectMessage, setProjectMessage] = useState("");
  const [projectOverview, setProjectOverview] = useState<ProjectOverview | null>(null);
  const [experimentRuns, setExperimentRuns] = useState<ExperimentRun[]>([]);
  const [experimentRunsRefresh, setExperimentRunsRefresh] = useState(0);
  const [baselineExperimentRunId, setBaselineExperimentRunId] = useState("");
  const [currentExperimentRunId, setCurrentExperimentRunId] = useState("");
  const [securityStatus, setSecurityStatus] = useState<SecurityStatus | null>(null);
  const [securityStatusError, setSecurityStatusError] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [confirmPassphrase, setConfirmPassphrase] = useState("");
  const [acknowledgeNoRecovery, setAcknowledgeNoRecovery] = useState(false);
  const [securityMessage, setSecurityMessage] = useState("");
  const [securityBusy, setSecurityBusy] = useState(false);
  const [activeDocumentRefs, setActiveDocumentRefs] = useState<ActiveDocumentRef[]>([]);
  const [documentPreview, setDocumentPreview] = useState<DocumentPreview | null>(null);
  const [documentPreviewMode, setDocumentPreviewMode] = useState<"text" | "chunks">("text");
  const [documentViewerOpen, setDocumentViewerOpen] = useState(false);
  const [documentLibraryOpen, setDocumentLibraryOpen] = useState(false);
  const [retrievalTestOpen, setRetrievalTestOpen] = useState(false);
  const [retrievalTestQuery, setRetrievalTestQuery] = useState("");
  const [retrievalTestBaseQuery, setRetrievalTestBaseQuery] = useState("");
  const [retrievalTestResult, setRetrievalTestResult] = useState<RetrievalTestResult | null>(null);
  const [retrievalTestBusy, setRetrievalTestBusy] = useState(false);
  const [retrievalFeedbackBusy, setRetrievalFeedbackBusy] = useState(false);
  const [retrievalTestMessage, setRetrievalTestMessage] = useState("");
  const [documentDeleteAuthorization, setDocumentDeleteAuthorization] = useState<string | null>(null);
  const [documentDeletePassphrase, setDocumentDeletePassphrase] = useState("");
  const [documentDeleteTarget, setDocumentDeleteTarget] = useState<DocumentItem | null>(null);
  const [documentDeleteIdempotencyKey, setDocumentDeleteIdempotencyKey] = useState<string | null>(null);
  const [documentDeleteBusy, setDocumentDeleteBusy] = useState(false);
  const [documentDeleteMessage, setDocumentDeleteMessage] = useState("");
  const [apiToken, setApiToken] = useState<string | null>(null);
  const [apiBaseUrl, setApiBaseUrl] = useState(DEFAULT_API_BASE_URL);
  const [runtimeConfigLoaded, setRuntimeConfigLoaded] = useState(false);
  const [cloudModelConfig, setCloudModelConfig] = useState<CloudModelConfig>({
    configured: false,
    enabled: false,
    base_url: "",
    model: "",
    models: [],
    api_key_configured: false,
  });
  const [cloudModelBaseUrl, setCloudModelBaseUrl] = useState("");
  const [cloudModelApiKey, setCloudModelApiKey] = useState("");
  const [cloudModelName, setCloudModelName] = useState("");
  const [cloudModelEnabled, setCloudModelEnabled] = useState(false);
  const [cloudModelBusy, setCloudModelBusy] = useState(false);
  const [cloudModelMessage, setCloudModelMessage] = useState("");
  const [indexStartStatus, setIndexStartStatus] = useState<"idle" | "starting" | "error">("idle");
  const [indexStartMessage, setIndexStartMessage] = useState("");
  const [documentImportStatus, setDocumentImportStatus] = useState<"idle" | "selecting" | "importing" | "error">("idle");
  const [documentImportMessage, setDocumentImportMessage] = useState("");
  const [documentImportScope, setDocumentImportScope] = useState<"global" | "project">("global");
  const [documentScopeBusy, setDocumentScopeBusy] = useState(false);
  const [documentScopeMessage, setDocumentScopeMessage] = useState("");
  const [artifactCollapsed, setArtifactCollapsed] = useState(false);
  const [artifactTab, setArtifactTab] = useState<"evidence" | "graph" | "audit">("evidence");
  const [collapsedNodeIds, setCollapsedNodeIds] = useState<Set<string>>(new Set());
  const [workbenchLayout, setWorkbenchLayout] = useState<WorkbenchLayoutPreference>(() => readWorkbenchLayoutPreference());
  const [isWorkbenchDragging, setIsWorkbenchDragging] = useState(false);
  const [reviewState, setReviewState] = useState<ReviewDraftState>({
    review: null,
    selectedIds: new Set(),
    edits: {},
    mode: "idle",
    message: "",
  });
  const [runtimeMemoryNotice, setRuntimeMemoryNotice] = useState<RuntimeMemoryNotice | null>(null);
  const [memoryManagerOpen, setMemoryManagerOpen] = useState(false);
  const [learningMemories, setLearningMemories] = useState<LearningMemory[]>([]);
  const [memoryHealth, setMemoryHealth] = useState<MemoryHealth | null>(null);
  const [memoryEdits, setMemoryEdits] = useState<Record<string, { title: string; content: string }>>({});
  const [memoryManagerBusy, setMemoryManagerBusy] = useState(false);
  const [memoryManagerMessage, setMemoryManagerMessage] = useState("");
  const [presentationOpen, setPresentationOpen] = useState(false);
  const [presentationSources, setPresentationSources] = useState<PresentationSourceRef[]>([]);
  const [presentationTitle, setPresentationTitle] = useState("");
  const [presentationAudience, setPresentationAudience] = useState<"research" | "technical" | "government">("research");
  const [presentationMaxSlides, setPresentationMaxSlides] = useState(10);
  const [presentationOpenInWps, setPresentationOpenInWps] = useState(true);
  const [presentationStyleSkills, setPresentationStyleSkills] = useState<PresentationStyleSkill[]>([]);
  const [presentationStyleSkillId, setPresentationStyleSkillId] = useState("");
  const [presentationRequestId, setPresentationRequestId] = useState("");
  const [presentationTask, setPresentationTask] = useState<PresentationTask | null>(null);
  const [presentationTasks, setPresentationTasks] = useState<PresentationTask[]>([]);
  const [presentationTasksRefresh, setPresentationTasksRefresh] = useState(0);
  const [presentationTasksMessage, setPresentationTasksMessage] = useState("");
  const [presentationPollRefresh, setPresentationPollRefresh] = useState(0);
  const [presentationMessage, setPresentationMessage] = useState("");
  const [skillOpen, setSkillOpen] = useState(false);
  const [learningSkills, setLearningSkills] = useState<LearningSkill[]>([]);
  const [skillInbox, setSkillInbox] = useState<SkillInboxItem[]>([]);
  const [skillSource, setSkillSource] = useState("");
  const [skillInstallTarget, setSkillInstallTarget] = useState<"conversation" | "presentation">("conversation");
  const [skillRootPath, setSkillRootPath] = useState("");
  const [skillMessage, setSkillMessage] = useState("");
  const [skillBusy, setSkillBusy] = useState(false);
  const [skillDeletingId, setSkillDeletingId] = useState("");
  const [pendingSkillDelete, setPendingSkillDelete] = useState<LearningSkill | null>(null);
  const [skillAdapterReview, setSkillAdapterReview] = useState<SkillAdapterReview | null>(null);
  const [selectedSkillId, setSelectedSkillId] = useState("");
  const [draftOpen, setDraftOpen] = useState(false);
  const [draftKind, setDraftKind] = useState<"email" | "schedule">("email");
  const [draftBusy, setDraftBusy] = useState(false);
  const [draftMessage, setDraftMessage] = useState("");
  const [draftArtifact, setDraftArtifact] = useState<DraftArtifact | null>(null);
  const [editingScheduleArtifactId, setEditingScheduleArtifactId] = useState<string | null>(null);
  const [projectScheduleArtifacts, setProjectScheduleArtifacts] = useState<ProjectArtifactSummary[]>([]);
  const [scheduleRailExpanded, setScheduleRailExpanded] = useState(false);
  const [selectedScheduleArtifactId, setSelectedScheduleArtifactId] = useState<string | null>(null);
  const [scheduleHistoryOpen, setScheduleHistoryOpen] = useState(false);
  const [scheduleHistoryBusy, setScheduleHistoryBusy] = useState(false);
  const [scheduleHistoryMessage, setScheduleHistoryMessage] = useState("");
  const [scheduleHistory, setScheduleHistory] = useState<ProjectArtifactSummary[]>([]);
  const [selectedScheduleHistoryArtifactId, setSelectedScheduleHistoryArtifactId] = useState<string | null>(null);
  const [scheduleReminderArtifactId, setScheduleReminderArtifactId] = useState<string | null>(null);
  const [scheduleReminderOpen, setScheduleReminderOpen] = useState(false);
  const [scheduleReminderPhase, setScheduleReminderPhase] = useState<"upcoming" | "started" | null>(null);
  const [scheduleExportPath, setScheduleExportPath] = useState("");
  const [scheduleReminderMessage, setScheduleReminderMessage] = useState("");
  const [scheduleNowMs, setScheduleNowMs] = useState(() => Date.now());
  const [contacts, setContacts] = useState<LocalContact[]>([]);
  const [contactDialogOpen, setContactDialogOpen] = useState(false);
  const [contactMessage, setContactMessage] = useState("");
  const [emailAttachmentRefs, setEmailAttachmentRefs] = useState<string[]>([]);
  const [emailAttachmentBusy, setEmailAttachmentBusy] = useState(false);
  const [smtpPreference, setSmtpPreference] = useState<SmtpPreference>(() => {
    const current = readSmtpPreference();
    if (typeof window === "undefined") return current;
    const defaultSender = window.localStorage.getItem(SMTP_DEFAULT_SENDER_STORAGE_KEY);
    return readSmtpAccounts().find((account) => account.sender === defaultSender) || current;
  });
  const [smtpAccounts, setSmtpAccounts] = useState<SmtpPreference[]>(() => readSmtpAccounts());
  const [smtpConfigOpen, setSmtpConfigOpen] = useState(() => readSmtpAccounts().length === 0);
  const [smtpDefaultSender, setSmtpDefaultSender] = useState(() => typeof window === "undefined" ? "" : window.localStorage.getItem(SMTP_DEFAULT_SENDER_STORAGE_KEY) || readSmtpPreference().sender);
  const [smtpConfigured, setSmtpConfigured] = useState<"checking" | "configured" | "unconfigured">("unconfigured");
  const [smtpStatusMessage, setSmtpStatusMessage] = useState("");
  const smtpProvider = smtpPreference.provider;
  const smtpSender = smtpPreference.sender;
  const [smtpAuthorizationCode, setSmtpAuthorizationCode] = useState("");
  const [contactName, setContactName] = useState("");
  const [contactEmail, setContactEmail] = useState("");
  const [contactAliases, setContactAliases] = useState("");
  const [emailRecipient, setEmailRecipient] = useState("");
  const [emailSubject, setEmailSubject] = useState("");
  const [emailBody, setEmailBody] = useState("");
  const [scheduleTitle, setScheduleTitle] = useState("");
  const [scheduleStartAt, setScheduleStartAt] = useState("");
  const [scheduleDuration, setScheduleDuration] = useState(60);
  const [scheduleParticipant, setScheduleParticipant] = useState("");
  const [scheduleNotes, setScheduleNotes] = useState("");
  const [scheduleSoundEnabled, setScheduleSoundEnabled] = useState(true);
  const [multimodalDecision, setMultimodalDecision] = useState<MultimodalDecision | null>(null);
  const [multimodalRuntime, setMultimodalRuntime] = useState<MultimodalRuntimeResult | null>(null);
  const [multimodalCapturedAtMs, setMultimodalCapturedAtMs] = useState<number | null>(null);
  const [uiAutomationSnapshot, setUiAutomationSnapshot] = useState<UiAutomationSnapshot | null>(null);
  const [mediaFiles, setMediaFiles] = useState<{ audio: File | null; image: File | null }>({ audio: null, image: null });
  const [mediaBusy, setMediaBusy] = useState(false);
  const [mediaCaptureState, setMediaCaptureState] = useState<"idle" | "requesting_audio" | "recording" | "processing" | "capturing_screen">("idle");
  const [visualMode, setVisualMode] = useState<"auto" | "fast" | "slow">("auto");
  const [selectedImageRegion, setSelectedImageRegion] = useState<NormalizedImageRegion | null>(null);
  const [imageSelectionDraft, setImageSelectionDraft] = useState<NormalizedImageRegion | null>(null);
  const [mediaPreviewOpen, setMediaPreviewOpen] = useState(true);
  const [mediaMessage, setMediaMessage] = useState("");
  const [workflowSpecs, setWorkflowSpecs] = useState<MultimodalWorkflowSpec[]>([]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<MultimodalWorkflowId | "">("");
  const [workflowPlan, setWorkflowPlan] = useState<MultimodalWorkflowPlan | null>(null);
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [workflowMessage, setWorkflowMessage] = useState("");
  const workflowScopeRef = useRef("");
  const [speechConfirmationPending, setSpeechConfirmationPending] = useState(false);
  const [sessionCreateOpen, setSessionCreateOpen] = useState(false);
  const [sessionCreateName, setSessionCreateName] = useState("");
  const [sessionCreateWorkflowId, setSessionCreateWorkflowId] = useState<MultimodalWorkflowId | "">("");
  const mediaInputRef = useRef<HTMLInputElement>(null);
  const indexRequestIdempotencyKeyRef = useRef("");
  const indexRequestRunIdRef = useRef("");
  const projectCreateFingerprintRef = useRef("");
  const projectCreateIdempotencyKeyRef = useRef("");
  const scheduleDraftFingerprintRef = useRef("");
  const scheduleDraftIdempotencyKeyRef = useRef("");
  const emailDraftFingerprintRef = useRef("");
  const emailDraftIdempotencyKeyRef = useRef("");
  const reflectionDraftFingerprintRef = useRef("");
  const reflectionDraftIdempotencyKeyRef = useRef("");
  const emailDeliveryArtifactRef = useRef("");
  const emailDeliveryIdempotencyKeyRef = useRef("");
  const audioRecorderRef = useRef<MediaRecorder | null>(null);
  const audioStreamRef = useRef<MediaStream | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const audioCaptureRequestedRef = useRef(false);
  const audioStopTimerRef = useRef<number | null>(null);
  const audioStopReasonRef = useRef<"manual" | "limit">("manual");
  const speechRecognitionRef = useRef<BrowserSpeechRecognition | null>(null);
  const speechDraftRef = useRef("");
  const scheduleReminderSentRef = useRef<Set<string>>(new Set());
  const scheduleReminderDismissedRef = useRef<Set<string>>(new Set());
  const scheduleStartedEligibleRef = useRef<Set<string>>(new Set());
  const scheduleStartedNotifiedRef = useRef<Set<string>>(new Set());
  const scheduleReminderKeyRef = useRef("");
  const skillDeleteIdempotencyKeyRef = useRef<Record<string, string>>({});
  const scheduleReminderAudioContextRef = useRef<AudioContext | null>(null);
  const imageSelectionStartRef = useRef<{ x: number; y: number } | null>(null);
  const askTextAreaRef = useRef<HTMLTextAreaElement>(null);
  // 截图选区定义视觉内容范围，不是可点击控件目标；只有无图片时的明确 UIA 目标才走目标复核。
  const targetGrounding = Boolean(uiAutomationSnapshot?.element && !mediaFiles.image);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionLoadRequestRef = useRef(0);
  const sessionCreateFingerprintRef = useRef("");
  const sessionCreateIdRef = useRef("");
  const workbenchRef = useRef<HTMLElement | null>(null);
  const visualWarmupFileRef = useRef<File | null>(null);
  const perceptionWarmupPromiseRef = useRef<Promise<MultimodalPerceptionWarmupResult | null> | null>(null);

  const mediaImageUrl = useMemo(
    () => (mediaFiles.image ? URL.createObjectURL(mediaFiles.image) : null),
    [mediaFiles.image],
  );

  useEffect(() => () => {
    if (mediaImageUrl) URL.revokeObjectURL(mediaImageUrl);
  }, [mediaImageUrl]);

  const invalidateMediaPerception = () => {
    setMultimodalRuntime(null);
    setMultimodalCapturedAtMs(null);
  };

  const resetSessionScopedContext = () => {
    // 会话是研究任务与证据范围的边界；切换会话不能把上一会话的
    // 文档、实验运行、屏幕目标或媒体继续附加到下一条请求。
    setInput("");
    setActiveDocumentRefs([]);
    setMediaFiles({ audio: null, image: null });
    setUiAutomationSnapshot(null);
    setSelectedImageRegion(null);
    setImageSelectionDraft(null);
    setMediaPreviewOpen(true);
    setMediaMessage("");
    setMultimodalDecision(null);
    setSpeechConfirmationPending(false);
    invalidateMediaPerception();
    setArtifactTab("evidence");
    setCollapsedNodeIds(new Set());
    setWorkflowPlan(null);
    setWorkflowMessage("");
    setBaselineExperimentRunId("");
    setCurrentExperimentRunId("");
    setScheduleExportPath("");
  };

  const requestPerceptionWarmup = (): Promise<MultimodalPerceptionWarmupResult | null> => {
    if (locked || !runtimeConfigLoaded) return Promise.resolve(null);
    if (perceptionWarmupPromiseRef.current) return perceptionWarmupPromiseRef.current;
    const request = fetch(`${apiBaseUrl}/multimodal/runtime/warmup-perception`, {
      method: "POST",
      headers: authHeaders(),
    })
      .then(async (response) => {
        const result = await response.json() as MultimodalPerceptionWarmupResult;
        return response.ok ? result : { ...result, status: "failed" as const };
      })
      .catch(() => null);
    const tracked = request.finally(() => {
      if (perceptionWarmupPromiseRef.current === tracked) perceptionWarmupPromiseRef.current = null;
    });
    perceptionWarmupPromiseRef.current = tracked;
    return tracked;
  };

  const applyUiAutomationSnapshot = (snapshot: UiAutomationSnapshot) => {
    setMultimodalDecision(null);
    invalidateMediaPerception();
    if (snapshot.status === "unavailable" || !snapshot.element) {
      setUiAutomationSnapshot(null);
      return;
    }
    setUiAutomationSnapshot(snapshot);
    const label = snapshot.element.name || snapshot.element.target_kind || "当前控件";
    const selectedText = snapshot.element.selected_text?.trim();
    setMediaMessage(snapshot.status === "redacted"
      ? `已读取${label}，敏感字段已脱敏；可在 30 秒内提问。`
      : selectedText
        ? `已读取${label}及选中文字（${selectedText.length} 字）；可在 30 秒内直接改写或提问。`
        : `已读取${label}；可在 30 秒内直接提问。`);
  };

  const parsedMessages = useMemo(
    () => messages.map((message) => ({ message, parsed: parseMessageArtifacts(message.content) })),
    [messages],
  );

  const latestMindmap = useMemo(() => {
    for (let index = parsedMessages.length - 1; index >= 0; index -= 1) {
      const item = parsedMessages[index];
      if (item.message.role === "ai" && item.parsed.artifacts.length) {
        return item.parsed.artifacts[item.parsed.artifacts.length - 1];
      }
    }
    return null;
  }, [parsedMessages]);

  const latestEvidence = useMemo(() => {
    for (let index = parsedMessages.length - 1; index >= 0; index -= 1) {
      const item = parsedMessages[index];
      if (item.message.role === "ai" && item.parsed.evidence.length) {
        return item.parsed.evidence;
      }
    }
    return [];
  }, [parsedMessages]);

  const mindmapLayout = useMemo(() => {
    if (!latestMindmap) return null;
    return buildMindmapLayout(latestMindmap.nodes, collapsedNodeIds);
  }, [latestMindmap, collapsedNodeIds]);

  const documents = documentLibrary?.documents ?? [];
  const indexedDocuments = useMemo(() => documents.filter((document) => document.indexed), [documents]);
  const presentationDocuments = useMemo(() => indexedDocuments.filter((document) => isPresentationDocumentInProject(document, activeProjectId)), [indexedDocuments, activeProjectId]);
  const activeDocumentNames = useMemo(
    () => activeDocumentRefs.map((document) => document.name),
    [activeDocumentRefs],
  );
  const activeDocumentIds = useMemo(
    () => activeDocumentRefs.map((document) => document.document_id).filter((documentId): documentId is string => Boolean(documentId)),
    [activeDocumentRefs],
  );
  const activeDocumentName = activeDocumentRefs[0]?.name || null;
  const locked = runtimeStatus?.locked ?? true;
  workflowScopeRef.current = JSON.stringify([activeProjectId, activeSessionId, selectedWorkflowId, apiBaseUrl, apiToken, locked]);
  const indexingStatus = runtimeStatus?.indexing?.status || "idle";
  const indexBusy = ["queued", "scanning", "indexing"].includes(indexingStatus);
  useEffect(() => {
    const indexing = runtimeStatus?.indexing;
    if (!indexing || !indexRequestRunIdRef.current || indexing.run_id !== indexRequestRunIdRef.current) return;
    if (["completed", "failed", "blocked", "idle"].includes(indexing.status)) {
      indexRequestIdempotencyKeyRef.current = "";
      indexRequestRunIdRef.current = "";
    }
  }, [runtimeStatus]);
  const leftCollapsed = workbenchLayout.leftCollapsed;
  const artifactWeight = workbenchLayout.artifactWeight;
  const openArtifactStudio = (tab: typeof artifactTab) => {
    setArtifactTab(tab);
    setArtifactCollapsed(false);
  };
  const visibleProjectSessions = showAllProjectSessions ? projectSessions : projectSessions.slice(0, 3);
  const selectedWorkflow = workflowSpecs.find((workflow) => workflow.workflow_id === selectedWorkflowId) || null;
  const selectedCloudModelIsListed = cloudModelConfig.models.includes(cloudModelName);
  const visibleScheduleArtifacts = useMemo(() => {
    return [...projectScheduleArtifacts]
      .filter((artifact) => artifact.artifact_type === "schedule_draft")
      .filter((artifact) => artifact.confirmation_status !== "cancelled")
      .filter((artifact) => {
        const startMs = Date.parse(String(artifact.plan?.start_at || ""));
        return !Number.isFinite(startMs) || startMs > scheduleNowMs;
      })
      .sort((left, right) => {
        const leftStart = Date.parse(String(left.plan?.start_at || 0));
        const rightStart = Date.parse(String(right.plan?.start_at || 0));
        return leftStart - rightStart || (right.created_at ? Date.parse(String(right.created_at)) : 0) - (left.created_at ? Date.parse(String(left.created_at)) : 0);
      });
  }, [projectScheduleArtifacts, scheduleNowMs]);
  const selectedScheduleArtifact = useMemo(() => {
    if (!visibleScheduleArtifacts.length) return null;
    return visibleScheduleArtifacts.find((artifact) => artifact.artifact_id === selectedScheduleArtifactId) || visibleScheduleArtifacts[0];
  }, [selectedScheduleArtifactId, visibleScheduleArtifacts]);
  const selectedScheduleHistoryArtifact = useMemo(() => {
    if (!scheduleHistory.length) return null;
    return scheduleHistory.find((artifact) => artifact.artifact_id === selectedScheduleHistoryArtifactId) || scheduleHistory[0];
  }, [scheduleHistory, selectedScheduleHistoryArtifactId]);
  const scheduleReminderArtifact = useMemo(
    () => projectScheduleArtifacts.find((artifact) => artifact.artifact_id === scheduleReminderArtifactId && artifact.confirmation_status !== "cancelled") || null,
    [projectScheduleArtifacts, scheduleReminderArtifactId],
  );
  const scheduleReminderRemainingMinutes = (() => {
    if (scheduleReminderPhase !== "upcoming" || !scheduleReminderArtifact?.plan?.start_at) return null;
    const startMs = Date.parse(String(scheduleReminderArtifact.plan.start_at));
    if (!Number.isFinite(startMs)) return null;
    return Math.max(0, Math.ceil((startMs - scheduleNowMs) / (60 * 1000)));
  })();
  const scheduleReminderTitle = scheduleReminderArtifact?.plan?.title || scheduleReminderArtifact?.plan?.subject || "日程";
  const scheduleReminderLiveMessage = scheduleReminderRemainingMinutes === null
    ? scheduleReminderMessage
    : scheduleReminderRemainingMinutes > 0
      ? `还有 ${scheduleReminderRemainingMinutes} 分钟后开始：${scheduleReminderTitle}`
      : `小于 1 分钟后开始：${scheduleReminderTitle}`;

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | null = null;
    void listen<UiAutomationSnapshot>("souldrive-uia-capture", (event) => {
      if (!disposed && !locked) applyUiAutomationSnapshot(event.payload);
    })
      .then((stop) => {
        if (disposed) stop();
        else unlisten = stop;
      })
      .catch(() => {
        // Browser development mode has no native Tauri event bus.
      });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [locked]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    saveWorkbenchLayoutPreference(workbenchLayout);
  }, [workbenchLayout]);

  useEffect(() => {
    saveSmtpPreference(smtpPreference);
  }, [smtpPreference]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(SMTP_ACCOUNTS_STORAGE_KEY, JSON.stringify(smtpAccounts));
    window.localStorage.setItem(SMTP_DEFAULT_SENDER_STORAGE_KEY, smtpDefaultSender);
  }, [smtpAccounts, smtpDefaultSender]);

  useEffect(() => {
    const sender = smtpSender.trim();
    if (!runtimeConfigLoaded || !sender) {
      setSmtpConfigured("unconfigured");
      setSmtpStatusMessage(sender ? "正在等待本地服务就绪..." : "请填写发件邮箱后检查本机 SMTP 配置。");
      return;
    }
    let active = true;
    setSmtpConfigured("checking");
    setSmtpStatusMessage("正在核验本机 SMTP 配置...");
    void fetch(`${apiBaseUrl}/email/smtp-status?sender=${encodeURIComponent(sender)}`, {
      cache: "no-store",
      headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
    })
      .then(async (response) => response.ok ? await response.json() as { configured?: boolean; provider?: "qq" | "netease163"; reason?: string } : { configured: false, reason: "status_request_failed" })
      .then((payload) => {
        if (!active) return;
        setSmtpConfigured(payload.configured ? "configured" : "unconfigured");
        setSmtpStatusMessage(payload.configured
          ? "当前发件邮箱已配置，可在确认草稿后手动发送。"
          : payload.reason === "credential_present_but_invalid"
            ? "Windows 凭据管理器中存在不可读取的旧 SMTP 配置，请重新保存授权码。"
            : payload.reason === "credential_missing"
              ? "Windows 凭据管理器中未找到此发件邮箱的 SMTP 配置，请输入授权码并保存。"
              : "暂时无法读取本机 SMTP 配置，请稍后重试。");
        if (payload.provider === "qq" || payload.provider === "netease163") {
          setSmtpPreference((current) => current.provider === payload.provider ? current : { ...current, provider: payload.provider! });
        }
      })
      .catch(() => {
        if (!active) return;
        setSmtpConfigured("unconfigured");
        setSmtpStatusMessage("暂时无法读取本机 SMTP 配置，请确认 sidecar 已启动。");
      });
    return () => { active = false; };
  }, [apiBaseUrl, apiToken, runtimeConfigLoaded, runtimeStatus?.locked, smtpProvider, smtpSender]);

  useEffect(() => {
    if (!draftOpen || !runtimeConfigLoaded || (draftKind === "email" && !activeProjectId)) {
      setContacts([]);
      return;
    }
    let active = true;
    const contactQuery = draftKind === "email" && activeProjectId
      ? `?project_id=${encodeURIComponent(activeProjectId)}`
      : "";
    void fetch(`${apiBaseUrl}/contacts${contactQuery}`, { headers: authHeaders() })
      .then(async (response) => response.ok ? await response.json() as { contacts?: LocalContact[] } : { contacts: [] })
      .then((payload) => { if (active) setContacts(Array.isArray(payload.contacts) ? payload.contacts : []); })
      .catch(() => { if (active) setContacts([]); });
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, draftKind, draftOpen, runtimeConfigLoaded]);

  useEffect(() => {
    setCollapsedNodeIds(new Set());
  }, [latestMindmap?.raw]);

  useEffect(() => {
    let isMounted = true;

    const loadRuntimeConfig = async () => {
      try {
        const config = await invoke<RuntimeConfig>("runtime_config");
        if (!isMounted) return;
        setApiBaseUrl(config.base_url || DEFAULT_API_BASE_URL);
        setApiToken(config.api_token || null);
        setRuntimeConfigLoaded(true);
      } catch {
        if (!isMounted) return;
        setApiBaseUrl(DEFAULT_API_BASE_URL);
        setApiToken(null);
        setRuntimeConfigLoaded(true);
      }
    };

    void loadRuntimeConfig();

    return () => {
      isMounted = false;
    };
  }, []);

  useEffect(() => {
    if (!runtimeConfigLoaded) return;
    if (locked) {
      setCloudModelMessage("解锁工作区后可配置云端模型；当前继续使用本地路径。");
      return;
    }

    let isMounted = true;
    void fetch(`${apiBaseUrl}/cloud-model/config`, {
      cache: "no-store",
      headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
    })
      .then(async (response) => {
        const payload = await response.json() as Partial<CloudModelConfig> & { error?: string };
        if (!response.ok) throw new Error(payload.error || "云端模型配置读取失败");
        return payload;
      })
      .then((payload) => {
        if (!isMounted) return;
        const models = Array.isArray(payload.models) ? payload.models : [];
        const config = {
          configured: Boolean(payload.configured),
          enabled: Boolean(payload.enabled),
          base_url: typeof payload.base_url === "string" ? payload.base_url : "",
          model: typeof payload.model === "string" ? payload.model : "",
          models,
          api_key_configured: Boolean(payload.api_key_configured),
        };
        setCloudModelConfig(config);
        setCloudModelBaseUrl(config.base_url);
        setCloudModelName(config.model);
        setCloudModelEnabled(config.enabled);
        setCloudModelMessage(config.configured
          ? (config.enabled ? "云端模型已启用，后续问答优先使用云端。" : "云端模型已配置但未启用，当前使用本地路径。")
          : "尚未配置云端模型，当前使用本地路径。");
      })
      .catch((error) => {
        if (isMounted) setCloudModelMessage(error instanceof Error ? error.message : "云端模型配置读取失败");
      });
    return () => { isMounted = false; };
  }, [apiBaseUrl, apiToken, locked, runtimeConfigLoaded]);

  useEffect(() => {
    if (!runtimeConfigLoaded) return;

    let isMounted = true;

    const refreshRuntimeStatus = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/runtime/status`);
        if (!response.ok) throw new Error("runtime status unavailable");
        const data = await response.json();
        if (isMounted) {
          setRuntimeStatus(data);
          // runtime/status is intentionally public and is the authoritative
          // local readiness signal. Do not show a false password wait state
          // when the authenticated security endpoint has a transient error.
          if (data?.software_unlocked && data?.locked === false) {
            setSecurityStatus((current) => current || {
              // A local development bypass may intentionally run without a
              // keystore file; it is still an unlocked usable workspace.
              crypto_initialized: true,
              software_unlocked: true,
              hardware_mounted: Boolean(data.hardware_mounted || data.workspace_path),
              reason: data.security_reason || data.reason || "本机工作区已就绪",
              no_recovery: true,
              development_bypass: true,
            });
            setSecurityStatusError("");
          }
        }
      } catch {
        if (isMounted) {
          setRuntimeStatus({
            locked: true,
            reason: "本地运行态服务不可用",
            auth_level: "OFFLINE",
          });
        }
      }
    };

    const refreshAuditEvents = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/audit/recent?limit=16`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("audit unavailable");
        const data = await response.json();
        if (isMounted) setAuditEvents(Array.isArray(data.events) ? data.events : []);
      } catch {
        if (isMounted) setAuditEvents([]);
      }
    };

    const refreshProductHealth = async () => {
      try {
        const healthResponse = await fetch(`${apiBaseUrl}/health`);
        if (!healthResponse.ok) throw new Error("health unavailable");

        const readyResponse = await fetch(`${apiBaseUrl}/ready`);
        const readyData = await readyResponse.json().catch(() => ({}));
        if (isMounted) {
          setHealthStatus({
            sidecar: "OK",
            ready: readyResponse.ok && Boolean(readyData.ready),
            reason: readyData.reason,
          });
        }
      } catch {
        if (isMounted) setHealthStatus({ sidecar: "OFFLINE", ready: false, reason: "sidecar unavailable" });
      }
    };

    const refreshMetrics = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/metrics`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("metrics unavailable");
        const data = await response.json();
        if (isMounted) setRuntimeMetrics(data);
      } catch {
        if (isMounted) setRuntimeMetrics(null);
      }
    };

    const refreshStorageStatus = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/storage/status`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("storage unavailable");
        const data = await response.json();
        if (isMounted) setStorageStatus(data);
      } catch {
        if (isMounted) setStorageStatus(null);
      }
    };

    const refreshProductDiagnostics = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/diagnostics/product`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("diagnostics unavailable");
        const data = await response.json();
        if (isMounted) setProductDiagnostics(data);
      } catch {
        if (isMounted) setProductDiagnostics(null);
      }
    };

    const refreshGraphRagDiagnostics = async () => {
      try {
        const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
        const response = await fetch(`${apiBaseUrl}/diagnostics/graphrag${projectQuery}`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          if (response.status === 423 && payload?.status === "locked") {
            if (isMounted) {
              setGraphRagDiagnostics({
                ready: false,
                status: "locked",
                reason: String(payload.reason || "workspace passphrase required"),
              });
            }
            return;
          }
          throw new Error("graphrag diagnostics unavailable");
        }
        const data = payload as GraphRagDiagnostics;
        if (isMounted) setGraphRagDiagnostics(data);
      } catch {
        if (isMounted) setGraphRagDiagnostics(null);
      }
    };

    const refreshDocumentLibrary = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/documents/list`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("documents unavailable");
        const data = await response.json();
        if (isMounted) setDocumentLibrary(data);
      } catch {
        if (isMounted) setDocumentLibrary(null);
      }
    };

    const refreshProjects = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/projects`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("projects unavailable");
        const data = await response.json() as { projects?: ProjectSummary[] };
        if (!isMounted) return;
        const next = Array.isArray(data.projects) ? data.projects : [];
        setProjects(next);
        setProjectListUnavailable(false);
        setActiveProjectId((current) => {
          if (current && next.some((project) => project.project_id === current)) return current;
          return (next.find((project) => project.asset_count > 0) || next[0])?.project_id || null;
        });
      } catch {
        if (isMounted) {
          // Sidecar 重启或热重载时保留上次成功读取的项目，不能把暂时
          // 无法访问的接口误显示为“项目空间为空”。
          setProjectListUnavailable(true);
        }
      }
    };

    const refreshSecurityStatus = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/security/status`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error(`security endpoint ${response.status}`);
        const data = await response.json();
        if (isMounted) {
          setSecurityStatus(data);
          setSecurityStatusError("");
        }
      } catch (error) {
        if (isMounted) {
          setSecurityStatusError(error instanceof Error ? error.message : "security endpoint unavailable");
          // Keep a known-good unlocked state through a single polling glitch.
          // The next runtime/status poll can restore this synthetic status.
          setSecurityStatus((current) => current?.software_unlocked ? current : null);
        }
      }
    };

    const refreshFastStatus = () => {
      void refreshRuntimeStatus();
      void refreshProductHealth();
      void refreshSecurityStatus();
    };
    const refreshBackgroundData = () => {
      if (locked || !apiToken) return;
      void refreshAuditEvents();
      void refreshMetrics();
      void refreshStorageStatus();
      void refreshProductDiagnostics();
      void refreshGraphRagDiagnostics();
      void refreshDocumentLibrary();
      void refreshProjects();
    };
    refreshFastStatus();
    refreshBackgroundData();
    const statusTimer = window.setInterval(refreshFastStatus, 3000);
    const dataTimer = window.setInterval(refreshBackgroundData, 15_000);

    return () => {
      isMounted = false;
      window.clearInterval(statusTimer);
      window.clearInterval(dataTimer);
    };
  }, [activeProjectId, apiBaseUrl, apiToken, locked, runtimeConfigLoaded]);

  useEffect(() => {
    if (!runtimeConfigLoaded || locked) {
      setBackupPreflight(null);
      return;
    }
    let isMounted = true;
    const refreshBackupPreflight = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/diagnostics/backup`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("backup preflight unavailable");
        const data = await response.json() as BackupPreflight;
        if (isMounted) setBackupPreflight(data);
      } catch {
        if (isMounted) setBackupPreflight(null);
      }
    };
    void refreshBackupPreflight();
    const timer = window.setInterval(() => void refreshBackupPreflight(), 15_000);
    return () => {
      isMounted = false;
      window.clearInterval(timer);
    };
  }, [apiBaseUrl, apiToken, locked, runtimeConfigLoaded]);

  useEffect(() => {
    if (!runtimeConfigLoaded || locked) {
      setWorkflowSpecs([]);
      setWorkflowPlan(null);
      return;
    }
    let active = true;
    const workflowQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
    void fetch(`${apiBaseUrl}/multimodal/workflows${workflowQuery}`, {
      headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
    })
      .then(async (response) => response.ok ? await response.json() as { workflows?: MultimodalWorkflowSpec[] } : { workflows: [] })
      .then((payload) => {
        if (!active) return;
        setWorkflowSpecs(Array.isArray(payload.workflows) ? payload.workflows : []);
      })
      .catch(() => {
        if (active) setWorkflowSpecs([]);
      });
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, locked, runtimeConfigLoaded]);

  useEffect(() => {
    if (!runtimeConfigLoaded || locked || mediaCaptureState !== "idle") return;
    const timer = window.setTimeout(() => {
      void requestPerceptionWarmup();
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [locked, mediaCaptureState, runtimeConfigLoaded]);

  useEffect(() => {
    if (!activeProjectId || !runtimeConfigLoaded) {
      setProjectOverview(null);
      return;
    }
    let active = true;
    const loadOverview = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/overview`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("project overview unavailable");
        const data = await response.json() as ProjectOverview;
        if (active) setProjectOverview(data);
      } catch {
        if (active) setProjectOverview(null);
      }
    };
    void loadOverview();
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, runtimeConfigLoaded]);

  useEffect(() => {
    if (!activeProjectId || !runtimeConfigLoaded || locked) {
      setExperimentRuns([]);
      return;
    }
    let active = true;
    void fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/experiments/runs`, {
      cache: "no-store",
      headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
    })
      .then(async (response) => response.ok ? await response.json() as { runs?: ExperimentRun[] } : { runs: [] })
      .then((payload) => {
        if (!active) return;
        setExperimentRuns(Array.isArray(payload.runs) ? payload.runs : []);
      })
      .catch(() => {
        if (active) setExperimentRuns([]);
      });
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, locked, runtimeConfigLoaded, experimentRunsRefresh]);

  useEffect(() => {
    // 项目是会话与文档上下文的边界，不能残留上一项目的选择或回答导图。
    sessionLoadRequestRef.current += 1;
    resetSessionScopedContext();
    setPresentationSources([]);
    setPresentationTask(null);
    setPresentationRequestId("");
    setPresentationMessage("");
    setShowAllProjectSessions(false);
    setSelectedWorkflowId("");
    setSessionCreateWorkflowId("");
  }, [activeProjectId]);

  useEffect(() => {
    // 文件或本轮媒体变化后，旧计划不能继续冒充当前输入状态。
    setWorkflowPlan(null);
    setWorkflowMessage("");
    setWorkflowBusy(false);
  }, [activeDocumentRefs, baselineExperimentRunId, currentExperimentRunId, mediaFiles.audio, mediaFiles.image, selectedWorkflowId, activeSessionId, apiBaseUrl, apiToken, locked]);

  useEffect(() => {
    if (!activeProjectId) setDocumentImportScope("global");
  }, [activeProjectId]);

  useEffect(() => {
    if (!runtimeConfigLoaded || !apiToken || !activeProjectId || locked) {
      setProjectSessions([]);
      setActiveSessionId(null);
      setSessionLoading(false);
      setMessages([]);
      setSessionCreateOpen(false);
      setSessionCreateName("");
      setSessionMessage("");
      return;
    }
    let active = true;
    const requestId = ++sessionLoadRequestRef.current;
    setSessionLoading(true);
    resetSessionScopedContext();
    setMessages([]);
    setActiveSessionId(null);
    setSessionCreateOpen(false);
    setSessionCreateName("");
    setSessionMessage("");
    const headers = { "X-SoulDrive-Token": apiToken };
    const loadHistory = async (session: ProjectSession) => {
      const response = await fetch(`${apiBaseUrl}/sessions/${encodeURIComponent(session.session_id)}/history?limit=32&project_id=${encodeURIComponent(activeProjectId)}`, { headers });
      if (!response.ok) throw new Error("session history unavailable");
      const payload = await response.json() as { history?: Array<{ role?: string; content?: string; timestamp_ms?: number; created_at?: number }> };
      if (!active || requestId !== sessionLoadRequestRef.current) return;
      setActiveSessionId(session.session_id);
      setSelectedWorkflowId((session.workflow_id || "") as MultimodalWorkflowId | "");
      setMessages((payload.history || [])
        .filter((item) => item.content?.trim())
        .map((item) => ({
          role: item.role === "assistant" || item.role === "ai" ? "ai" : "user",
          content: String(item.content),
          timestampMs: item.timestamp_ms ?? (item.created_at ? Math.round(item.created_at * 1000) : undefined),
        })));
    };
    void (async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/sessions`, { headers });
        if (!response.ok) throw new Error("project sessions unavailable");
        const payload = await response.json() as { sessions?: ProjectSession[] };
        let sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
        if (!sessions.length) {
          const created = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/sessions`, {
            method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify({ title: "主对话" }),
          });
          if (!created.ok) throw new Error("create project session failed");
          const createdPayload = await created.json() as { session?: ProjectSession };
          if (createdPayload.session) sessions = [createdPayload.session];
        }
        if (!active || requestId !== sessionLoadRequestRef.current) return;
        setProjectSessions(sessions);
        if (sessions[0]) await loadHistory(sessions[0]);
      } catch {
        if (active && requestId === sessionLoadRequestRef.current) {
          setProjectSessions([]);
          setActiveSessionId(null);
          setMessages([]);
          setSessionMessage("项目会话暂时无法读取，请稍后重试。");
        }
      } finally {
        if (active && requestId === sessionLoadRequestRef.current) setSessionLoading(false);
      }
    })();
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, locked, runtimeConfigLoaded]);

  useEffect(() => {
    if (!runtimeConfigLoaded || !apiToken) {
      setProjectScheduleArtifacts([]);
      setScheduleHistory([]);
      setScheduleHistoryOpen(false);
      setSelectedScheduleHistoryArtifactId(null);
      setScheduleHistoryMessage("");
      setScheduleReminderArtifactId(null);
      setScheduleReminderMessage("");
      setScheduleReminderOpen(false);
      setScheduleReminderPhase(null);
      scheduleStartedEligibleRef.current.clear();
      scheduleStartedNotifiedRef.current.clear();
      setSelectedScheduleArtifactId(null);
      return;
    }
    let active = true;
    const headers = authHeaders();
    const loadSchedules = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/schedules`, { headers });
        if (!response.ok) throw new Error("global schedules unavailable");
        const payload = await response.json() as { artifacts?: ProjectArtifactSummary[] };
        if (active) setProjectScheduleArtifacts(Array.isArray(payload.artifacts) ? payload.artifacts : []);
      } catch {
        if (active) setProjectScheduleArtifacts([]);
      }
    };
    void loadSchedules();
    const timer = window.setInterval(() => { void loadSchedules(); }, 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [apiBaseUrl, apiToken, runtimeConfigLoaded]);

  useEffect(() => {
    if (!projectScheduleArtifacts.length) return;
    setScheduleNowMs(Date.now());
    const timer = window.setInterval(() => setScheduleNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [projectScheduleArtifacts.length]);

  useEffect(() => {
    if (!visibleScheduleArtifacts.length) {
      setSelectedScheduleArtifactId(null);
      setScheduleRailExpanded(false);
      return;
    }
    if (!selectedScheduleArtifactId || !visibleScheduleArtifacts.some((artifact) => artifact.artifact_id === selectedScheduleArtifactId)) {
      setSelectedScheduleArtifactId(visibleScheduleArtifacts[0].artifact_id);
    }
  }, [selectedScheduleArtifactId, visibleScheduleArtifacts]);

  useEffect(() => {
    const AudioCtx = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;
    const unlockReminderAudio = () => {
      if (!scheduleReminderAudioContextRef.current) scheduleReminderAudioContextRef.current = new AudioCtx();
      const context = scheduleReminderAudioContextRef.current;
      if (context.state === "suspended") void context.resume().catch(() => undefined);
    };
    window.addEventListener("pointerdown", unlockReminderAudio);
    window.addEventListener("keydown", unlockReminderAudio);
    return () => {
      window.removeEventListener("pointerdown", unlockReminderAudio);
      window.removeEventListener("keydown", unlockReminderAudio);
      void scheduleReminderAudioContextRef.current?.close().catch(() => undefined);
      scheduleReminderAudioContextRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!projectScheduleArtifacts.length) {
      setScheduleReminderArtifactId(null);
      setScheduleReminderMessage("");
      setScheduleReminderOpen(false);
      setScheduleReminderPhase(null);
      scheduleReminderKeyRef.current = "";
      scheduleStartedEligibleRef.current.clear();
      scheduleStartedNotifiedRef.current.clear();
      return;
    }
    const schedules = projectScheduleArtifacts
      .map((artifact) => {
        const startMs = Date.parse(String(artifact.plan?.start_at || ""));
        return Number.isFinite(startMs) ? { artifact, startMs } : null;
      })
      .filter((item): item is { artifact: ProjectArtifactSummary; startMs: number } => Boolean(item))
      .filter((item) => item.artifact.confirmation_status !== "cancelled");
    const scheduleKey = (item: { artifact: ProjectArtifactSummary; startMs: number }) => `${item.artifact.artifact_id}:${item.startMs}`;
    schedules
      .filter((item) => item.startMs > scheduleNowMs)
      .forEach((item) => scheduleStartedEligibleRef.current.add(scheduleKey(item)));
    const startedSchedule = schedules
      .filter((item) => item.startMs <= scheduleNowMs)
      .filter((item) => scheduleStartedEligibleRef.current.has(scheduleKey(item)))
      .filter((item) => !scheduleStartedNotifiedRef.current.has(scheduleKey(item)))
      .sort((left, right) => left.startMs - right.startMs)[0] || null;
    if (startedSchedule) {
      const startedKey = scheduleKey(startedSchedule);
      scheduleStartedNotifiedRef.current.add(startedKey);
      scheduleReminderKeyRef.current = `${startedKey}:started`;
      setScheduleReminderArtifactId(startedSchedule.artifact.artifact_id);
      setScheduleReminderPhase("started");
      setScheduleReminderMessage("");
      if (!scheduleReminderDismissedRef.current.has(scheduleReminderKeyRef.current)) setScheduleReminderOpen(true);
      return;
    }
    if (scheduleReminderOpen && scheduleReminderArtifactId && schedules.some((item) => item.artifact.artifact_id === scheduleReminderArtifactId)) return;
    if (scheduleReminderOpen) setScheduleReminderOpen(false);
    const now = scheduleNowMs;
    const nextReminder = schedules
      .filter((item) => item.startMs > now && item.startMs - now <= 10 * 60 * 1000)
      .sort((left, right) => left.startMs - right.startMs)[0] || null;
    if (!nextReminder) {
      setScheduleReminderArtifactId(null);
      setScheduleReminderMessage("");
      setScheduleReminderOpen(false);
      setScheduleReminderPhase(null);
      scheduleReminderKeyRef.current = "";
      return;
    }
    const reminderBaseKey = `${nextReminder.artifact.artifact_id}:${nextReminder.startMs}`;
    const reminderKey = `${reminderBaseKey}:upcoming`;
    const remainingMinutes = Math.max(1, Math.ceil((nextReminder.startMs - now) / (60 * 1000)));
    scheduleReminderKeyRef.current = reminderKey;
    setScheduleReminderArtifactId(nextReminder.artifact.artifact_id);
    setScheduleReminderPhase("upcoming");
    setScheduleReminderMessage(`还有 ${remainingMinutes} 分钟后开始：${nextReminder.artifact.plan?.title || nextReminder.artifact.plan?.subject || "日程"}`);
    if (!scheduleReminderDismissedRef.current.has(reminderKey)) setScheduleReminderOpen(true);
    if (scheduleReminderSentRef.current.has(reminderBaseKey)) return;
    scheduleReminderSentRef.current.add(reminderBaseKey);
    if (typeof window !== "undefined" && "Notification" in window && window.Notification.permission === "granted") {
      void new window.Notification("SoulDrive 日程提醒", {
        body: `${nextReminder.artifact.plan?.title || nextReminder.artifact.plan?.subject || "日程"} 将在 10 分钟后开始`,
      });
    }
    if (nextReminder.artifact.plan?.reminder_sound_enabled === false) return;
    try {
      const AudioCtx = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!AudioCtx) return;
      if (!scheduleReminderAudioContextRef.current) scheduleReminderAudioContextRef.current = new AudioCtx();
      const context = scheduleReminderAudioContextRef.current;
      const playTone = () => {
        for (let index = 0; index < 3; index += 1) {
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          const start = context.currentTime + index * 0.45;
          oscillator.type = "sine";
          oscillator.frequency.value = 880;
          gain.gain.setValueAtTime(0.0001, start);
          gain.gain.exponentialRampToValueAtTime(0.1, start + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.3);
          oscillator.connect(gain);
          gain.connect(context.destination);
          oscillator.start(start);
          oscillator.stop(start + 0.32);
        }
      };
      if (context.state === "suspended") void context.resume().then(playTone).catch(() => undefined);
      else playTone();
    } catch {
      // 声音提醒失败不影响界面提醒。
    }
  }, [projectScheduleArtifacts, scheduleNowMs, scheduleReminderArtifactId, scheduleReminderOpen]);

  useEffect(() => {
    if (latestMindmap) {
      setArtifactTab("graph");
    } else if (latestEvidence.length) {
      setArtifactTab("evidence");
    }
  }, [latestMindmap, latestEvidence.length]);

  useEffect(() => {
    const taskId = presentationTask?.task_id;
    if (!taskId) return;
    const projectQuery = presentationTask?.project_id
      ? `?project_id=${encodeURIComponent(presentationTask.project_id)}`
      : "";
    let active = true;
    let timer: number | undefined;
    const resumeLease = presentationTask?.worker_started ? presentationTask.lease_epoch : undefined;
    let polls = 0;
    const pollTask = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/presentations/tasks/${taskId}${projectQuery}`, {
          cache: "no-store",
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("presentation task unavailable");
        const task = await response.json() as PresentationTask;
        if (!active) return;
        polls += 1;
        setPresentationTask(task);
        if (task.status === "completed" && task.kind === "presentation.plan") {
          setPresentationMessage("实际大纲已生成，请逐页查看并确认后再生成 PPT");
        } else if (task.status === "completed") {
          const application = task.result?.skill_application;
          const skillLabel = application?.skill_title ? ` · 已调用 ${application.skill_title}` : "";
          const styleLabel = application?.style_applied ? "（版式已应用）" : "";
          setPresentationMessage(`PPT 已生成${skillLabel}${styleLabel}`);
        }
        if (task.status === "failed" || task.status === "interrupted") {
          setPresentationMessage(task.error || "生成中断，可从检查点继续");
        }
        if (task.status === "cancelled") {
          setPresentationMessage("任务已停止；如需重新开始，请生成新的大纲请求");
        }
        if (!shouldPollPresentationTask(task, resumeLease, polls)) {
          if (timer !== undefined) window.clearInterval(timer);
        }
      } catch {
        if (active) setPresentationMessage("暂时无法读取生成状态");
      }
    };
    void pollTask();
    timer = window.setInterval(() => void pollTask(), 1200);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [apiBaseUrl, apiToken, presentationTask?.project_id, presentationTask?.task_id, presentationPollRefresh]);

  useEffect(() => {
    if (!presentationOpen) return;
    let active = true;
    const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
    void fetch(`${apiBaseUrl}/learning/skills${projectQuery}`, { headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined })
      .then(async (response) => response.ok ? response.json() : { skills: [] })
      .then((payload: { skills?: PresentationStyleSkill[] }) => {
        if (!active) return;
        const skills = uniqueSkillsById((payload.skills || []).filter(isPresentationSkill));
        setPresentationStyleSkills(skills);
        setPresentationStyleSkillId((current) => {
          if (current && skills.some((skill) => skill.skill_id === current)) return current;
          return "";
        });
      })
      .catch(() => { if (active) setPresentationStyleSkills([]); });
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, presentationOpen]);

  useEffect(() => {
    setPresentationTasks([]);
    if (!presentationOpen || !activeProjectId || locked || !apiToken) return;
    let active = true;
    setPresentationTasksMessage("正在读取当前项目任务...");
    void fetch(`${apiBaseUrl}/presentations/tasks?project_id=${encodeURIComponent(activeProjectId)}&limit=30`, {
      cache: "no-store", headers: authHeaders(),
    }).then(async (response) => {
      if (!response.ok) throw new Error("任务列表暂时不可用，请刷新重试");
      const payload = await response.json() as { tasks: PresentationTask[] };
      if (!active) return;
      setPresentationTasks(payload.tasks.filter((task) => task.project_id === activeProjectId));
      setPresentationTasksMessage("");
    }).catch((error) => { if (active) setPresentationTasksMessage(error instanceof Error ? error.message : "任务列表暂时不可用"); });
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, locked, presentationOpen, presentationTask?.status, presentationTasksRefresh]);

  useEffect(() => {
    if (!runtimeConfigLoaded) return;
    let active = true;
    const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
    void requestSkillApi<{ skills?: LearningSkill[]; path?: string }>("GET", `/learning/skills${projectQuery}`)
      .then((payload) => {
        if (!active) return;
        const skills = payload.skills || [];
        setLearningSkills(skills);
        if (payload.path) setSkillRootPath(payload.path);
        setSelectedSkillId((current) => current && skills.some((skill) => skill.skill_id === current && skill.target === "conversation" && skill.compatibility_status === "verified") ? current : "");
        setSkillMessage("");
      })
      .catch((error) => { if (active) setSkillMessage(error instanceof Error ? error.message : "暂时无法读取本地 Skill"); });
    if (!skillOpen) return () => { active = false; };
    void requestSkillApi<{ skills?: SkillInboxItem[] }>("GET", `/learning/skills/inbox${projectQuery}`)
      .then((payload) => { if (active) setSkillInbox(payload.skills || []); })
      .catch((error) => { if (active) setSkillMessage(error instanceof Error ? error.message : "暂时无法读取待审核 Skill"); });
    return () => { active = false; };
  }, [activeProjectId, apiBaseUrl, apiToken, runtimeConfigLoaded, skillOpen]);

  useEffect(() => {
    if (!skillMessage || skillBusy) return;
    const timer = window.setTimeout(() => setSkillMessage(""), 4500);
    return () => window.clearTimeout(timer);
  }, [skillBusy, skillMessage]);

  useEffect(() => {
    if (!isWorkbenchDragging) return;

    const handlePointerMove = (event: PointerEvent) => {
      const workbench = workbenchRef.current;
      if (!workbench) return;

      const rect = workbench.getBoundingClientRect();
      const leftWidth = leftCollapsed ? 72 : 300;
      const usableWidth = Math.max(420, rect.width - leftWidth - 42);
      const artifactShare = Math.min(0.74, Math.max(0.34, (rect.right - event.clientX) / usableWidth));
      const nextWeight = artifactShare / Math.max(0.26, 1 - artifactShare);
      setWorkbenchLayout((previous) => ({
        ...previous,
        artifactWeight: clampArtifactWeight(nextWeight),
      }));
    };

    const stopDragging = () => setIsWorkbenchDragging(false);

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopDragging);
    window.addEventListener("pointercancel", stopDragging);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopDragging);
      window.removeEventListener("pointercancel", stopDragging);
    };
  }, [isWorkbenchDragging, leftCollapsed]);

  const toggleMindmapNode = (nodeId: string) => {
    setCollapsedNodeIds((previous) => {
      const next = new Set(previous);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  };

  const toggleLeftPanel = () => {
    setWorkbenchLayout((previous) => ({
      ...previous,
      leftCollapsed: !previous.leftCollapsed,
    }));
  };

  const useGraphPriorityLayout = () => {
    setArtifactTab("graph");
    setWorkbenchLayout((previous) => ({
      ...previous,
      leftCollapsed: previous.leftCollapsed && Math.abs(previous.artifactWeight - GRAPH_PRIORITY_ARTIFACT_WEIGHT) < 0.01
        ? false
        : true,
      artifactWeight: previous.leftCollapsed && Math.abs(previous.artifactWeight - GRAPH_PRIORITY_ARTIFACT_WEIGHT) < 0.01
        ? DEFAULT_ARTIFACT_WEIGHT
        : GRAPH_PRIORITY_ARTIFACT_WEIGHT,
    }));
  };

  const startWorkbenchResize = (event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    setIsWorkbenchDragging(true);
  };

  const handleSplitterKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    setWorkbenchLayout((previous) => ({
      ...previous,
      artifactWeight: clampArtifactWeight(previous.artifactWeight + (event.key === "ArrowLeft" ? 0.12 : -0.12)),
    }));
  };

  const refreshSecurityAfterChange = async () => {
    try {
      const [runtimeResponse, securityResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/runtime/status`),
        fetch(`${apiBaseUrl}/security/status`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        }),
      ]);
      if (runtimeResponse.ok) setRuntimeStatus(await runtimeResponse.json());
      if (securityResponse.ok) setSecurityStatus(await securityResponse.json());
    } catch {
      // Polling will retry shortly; keep the UI responsive after submit.
    }
  };

  const authHeaders = (): Record<string, string> => (apiToken ? { "X-SoulDrive-Token": apiToken } : {});

  const openScheduleHistory = async () => {
    if (locked || scheduleHistoryBusy) return;
    setScheduleHistoryOpen(true);
    setScheduleHistoryBusy(true);
    setScheduleHistoryMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/schedules/history?days=7&limit=100`, { headers: authHeaders() });
      const payload = await response.json() as { artifacts?: ProjectArtifactSummary[]; error?: string; detail?: string };
      if (!response.ok) throw new Error(payload.error || payload.detail || "近 7 天日程历史加载失败");
      const artifacts = Array.isArray(payload.artifacts) ? payload.artifacts : [];
      setScheduleHistory(artifacts);
      setSelectedScheduleHistoryArtifactId((current) => current && artifacts.some((item) => item.artifact_id === current) ? current : artifacts[0]?.artifact_id || null);
    } catch (error) {
      setScheduleHistory([]);
      setSelectedScheduleHistoryArtifactId(null);
      setScheduleHistoryMessage(error instanceof Error ? error.message : "近 7 天日程历史加载失败");
    } finally {
      setScheduleHistoryBusy(false);
    }
  };

  const fetchCloudModels = async () => {
    if (locked || cloudModelBusy) return;
    if (!cloudModelBaseUrl.trim()) {
      setCloudModelMessage("请先填写 API 请求地址。");
      return;
    }
    setCloudModelBusy(true);
    setCloudModelMessage("正在获取云端模型列表...");
    try {
      const response = await fetch(`${apiBaseUrl}/cloud-model/models`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ base_url: cloudModelBaseUrl.trim(), api_key: cloudModelApiKey }),
      });
      const payload = await response.json().catch(() => ({})) as { models?: unknown; error?: string; detail?: string };
      if (!response.ok) throw new Error(payload.error || payload.detail || `云端模型列表获取失败（HTTP ${response.status}）`);
      const models = Array.isArray(payload.models)
        ? payload.models.filter((model): model is string => typeof model === "string" && Boolean(model.trim()))
        : [];
      setCloudModelConfig((current) => ({ ...current, models }));
      setCloudModelName((current) => current || models[0] || "");
      setCloudModelMessage(models.length ? `已获取 ${models.length} 个模型，请选择默认模型后保存。` : "云端没有返回可用模型。");
    } catch (error) {
      setCloudModelMessage(error instanceof Error ? error.message : "云端模型列表获取失败");
    } finally {
      setCloudModelBusy(false);
    }
  };

  const saveCloudModelConfig = async () => {
    if (locked || cloudModelBusy) return;
    if (!cloudModelBaseUrl.trim()) {
      setCloudModelMessage("请填写 API 请求地址。");
      return;
    }
    if (cloudModelEnabled && !cloudModelName.trim()) {
      setCloudModelMessage("启用云端模型前请选择默认模型。");
      return;
    }
    setCloudModelBusy(true);
    setCloudModelMessage("正在保存云端模型配置...");
    try {
      const response = await fetch(`${apiBaseUrl}/cloud-model/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          base_url: cloudModelBaseUrl.trim(),
          api_key: cloudModelApiKey,
          model: cloudModelName.trim(),
          enabled: cloudModelEnabled,
          models: cloudModelConfig.models,
        }),
      });
      const payload = await response.json() as Partial<CloudModelConfig> & { error?: string };
      if (!response.ok) throw new Error(payload.error || "云端模型配置保存失败");
      const config = {
        configured: Boolean(payload.configured),
        enabled: Boolean(payload.enabled),
        base_url: typeof payload.base_url === "string" ? payload.base_url : cloudModelBaseUrl.trim(),
        model: typeof payload.model === "string" ? payload.model : cloudModelName.trim(),
        models: Array.isArray(payload.models) ? payload.models : cloudModelConfig.models,
        api_key_configured: Boolean(payload.api_key_configured),
      };
      setCloudModelConfig(config);
      setCloudModelBaseUrl(config.base_url);
      setCloudModelName(config.model);
      setCloudModelEnabled(config.enabled);
      setCloudModelApiKey("");
      setCloudModelMessage(config.enabled ? "云端模型已启用，后续问答优先使用云端。" : "配置已保存，当前继续使用本地路径。");
    } catch (error) {
      setCloudModelMessage(error instanceof Error ? error.message : "云端模型配置保存失败");
    } finally {
      setCloudModelBusy(false);
    }
  };

  const requestWorkflowPlan = async (
    workflowId: MultimodalWorkflowId,
    speechText = "",
    textNote = "",
  ): Promise<MultimodalWorkflowPlan | null> => {
    const scope = workflowScopeRef.current;
    setWorkflowBusy(true);
    setWorkflowMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/multimodal/workflows/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          workflow_id: workflowId,
          project_id: activeProjectId,
          document_names: activeDocumentNames,
          document_ids: activeDocumentIds,
          has_audio: Boolean(mediaFiles.audio),
          has_image: Boolean(mediaFiles.image),
          // 媒体预检把录音/浏览器语音预览记为转写；会议会话中的直接记录
          // 另用 text_note 传递，永远不会被伪装成语音识别结果。
          speech_text: (mediaFiles.audio || speechDraftRef.current.trim()) ? speechText.slice(0, 6000) : "",
          text_note: workflowId === "meeting_actions" && !mediaFiles.audio ? textNote.slice(0, 6000) : "",
          selected_text: uiAutomationSnapshot?.element?.selected_text?.slice(0, 4000) || "",
          ...(workflowId === "experiment_compare" ? {
            baseline_run_id: baselineExperimentRunId || undefined,
            current_run_id: currentExperimentRunId || undefined,
          } : {}),
        }),
      });
      const payload = await response.json() as MultimodalWorkflowPlan & { error?: string };
      if (scope !== workflowScopeRef.current) return null;
      if (!response.ok) throw new Error(payload.error || "业务任务检查失败");
      setWorkflowPlan(payload);
      if (!payload.ready) setWorkflowMessage(payload.next_action);
      return payload;
    } catch (error) {
      if (scope !== workflowScopeRef.current) return null;
      setWorkflowPlan(null);
      setWorkflowMessage(error instanceof Error ? error.message : "业务任务检查失败");
      return null;
    } finally {
      if (scope === workflowScopeRef.current) setWorkflowBusy(false);
    }
  };

  const approveCurrentWorkflow = async () => {
    if (!activeProjectId || !selectedWorkflow || locked || workflowBusy) return;
    const scope = workflowScopeRef.current;
    setWorkflowBusy(true);
    setWorkflowMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/playbooks/${selectedWorkflow.workflow_id}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ playbook_version: selectedWorkflow.playbook_version, approved: true }),
      });
      const payload = await response.json() as { approval: MultimodalWorkflowSpec["approval"]; error?: string };
      if (scope !== workflowScopeRef.current) return;
      if (!response.ok) throw new Error(payload.error || "模式用途确认失败");
      if (payload.approval?.status !== "approved" || payload.approval.playbook_version !== selectedWorkflow.playbook_version) {
        throw new Error("模式版本未确认，请重新加载后审阅");
      }
      setWorkflowSpecs((specs) => specs.map((spec) => spec.workflow_id === selectedWorkflow.workflow_id ? { ...spec, approval: payload.approval } : spec));
      setWorkflowPlan(null);
      setWorkflowMessage("当前项目的模式用途已确认，请再次发送问题；没有自动执行。");
    } catch (error) {
      if (scope === workflowScopeRef.current) setWorkflowMessage(error instanceof Error ? error.message : "模式用途确认失败");
    } finally {
      if (scope === workflowScopeRef.current) setWorkflowBusy(false);
    }
  };

  const ensureWorkflowReady = async (speechText = "") => {
    if (!selectedWorkflowId) return true;
    const scope = workflowScopeRef.current;
    const plan = await requestWorkflowPlan(
      selectedWorkflowId,
      speechText,
      selectedWorkflowId === "meeting_actions" && !mediaFiles.audio ? speechText : "",
    );
    if (scope !== workflowScopeRef.current) return false;
    if (!plan?.ready) {
      setWorkflowMessage(plan?.next_action || "业务任务输入尚未就绪，本次未发送");
      return false;
    }
    return true;
  };

  const isActiveDocument = (document: Pick<DocumentItem, "name" | "document_id"> | Pick<DocumentPreview, "name" | "document_id">) => (
    activeDocumentRefs.some((selected) => document.document_id
      ? selected.document_id === document.document_id
      : !selected.document_id && selected.name === document.name)
  );

  const toggleDocumentContext = (document: Pick<DocumentItem, "name" | "document_id"> | Pick<DocumentPreview, "name" | "document_id">) => {
    const next = {
      name: document.name,
      ...(document.document_id ? { document_id: document.document_id } : {}),
    };
    setActiveDocumentRefs((current) => {
      const selected = current.some((item) => document.document_id
        ? item.document_id === document.document_id
        : !item.document_id && item.name === document.name);
      return selected
        ? current.filter((item) => document.document_id
          ? item.document_id !== document.document_id
          : item.document_id || item.name !== document.name)
        : [...current, next].slice(0, 12);
    });
  };

  const openDocumentViewer = async (document: DocumentItem) => {
    closeDocumentLibrary();
    setDocumentViewerOpen(true);
    setDocumentPreviewMode("text");
    setDocumentPreview({
      name: document.name,
      document_id: document.document_id,
      media_type: document.media_type,
      text: "正在加载文档预览…",
    });
    try {
      const previewQuery = new URLSearchParams({ name: document.name });
      if (document.document_id) previewQuery.set("document_id", document.document_id);
      if (activeProjectId) previewQuery.set("project_id", activeProjectId);
      const response = await fetch(`${apiBaseUrl}/documents/preview?${previewQuery.toString()}`, {
        headers: authHeaders(),
      });
      const result = await response.json() as DocumentPreview & { error?: string };
      if (!response.ok) throw new Error(result.error || "文档预览加载失败");
      setDocumentPreview(result);
    } catch (error) {
      setDocumentPreview({
        name: document.name,
        document_id: document.document_id,
        media_type: document.media_type,
        text: error instanceof Error ? error.message : "文档预览加载失败",
      });
    }
  };

  const openDocumentLibrary = () => {
    setDocumentLibraryOpen(true);
    setDocumentViewerOpen(false);
    setIndexPreview(null);
    setIndexPreviewMessage("");
  };

  const closeDocumentLibrary = () => {
    if (documentDeleteAuthorization) {
      void fetch(`${apiBaseUrl}/documents/delete-authorize/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ authorization: documentDeleteAuthorization }),
      });
    }
    setDocumentLibraryOpen(false);
    setDocumentDeleteAuthorization(null);
    setDocumentDeletePassphrase("");
    setDocumentDeleteTarget(null);
    setDocumentDeleteIdempotencyKey(null);
    setDocumentDeleteMessage("");
    setDocumentScopeMessage("");
    setIndexPreview(null);
    setIndexPreviewMessage("");
  };

  const openDocumentDeleteConfirmation = (document: DocumentItem) => {
    setDocumentDeleteTarget(document);
    setDocumentDeleteIdempotencyKey(`document-delete-${crypto.randomUUID()}`);
    setDocumentDeleteMessage("");
  };

  const previewIndexChanges = async () => {
    if (locked || indexPreviewBusy) return;
    setIndexPreviewBusy(true);
    setIndexPreviewMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/index/preview`, { headers: authHeaders() });
      const payload = await response.json() as IndexPreview & { error?: string };
      if (!response.ok) throw new Error(payload.error || "索引变更预览失败");
      setIndexPreview(payload);
    } catch (error) {
      setIndexPreview(null);
      setIndexPreviewMessage(error instanceof Error ? error.message : "索引变更预览失败");
    } finally {
      setIndexPreviewBusy(false);
    }
  };

  const openRetrievalTest = (initialQuery = "") => {
    closeDocumentLibrary();
    setRetrievalTestOpen(true);
    const query = initialQuery.trim() || input.trim();
    setRetrievalTestQuery(query);
    setRetrievalTestBaseQuery(query);
    setRetrievalTestResult(null);
    setRetrievalTestMessage("");
  };

  const closeRetrievalTest = () => {
    setRetrievalTestOpen(false);
    setRetrievalTestBusy(false);
    setRetrievalTestMessage("");
  };

  const runRetrievalTest = async (queryOverride?: string) => {
    const query = (queryOverride ?? retrievalTestQuery).trim();
    if (!query || retrievalTestBusy || locked) return;
    if (!queryOverride) setRetrievalTestBaseQuery(query);
    setRetrievalTestQuery(query);
    setRetrievalTestBusy(true);
    setRetrievalTestMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/diagnostics/retrieval`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          query,
          top_k: 6,
          project_id: activeProjectId,
          workflow_id: selectedWorkflowId || undefined,
        }),
      });
      const payload = await response.json() as RetrievalTestResult & { error?: string };
      if (!response.ok) throw new Error(payload.error || "检索测试失败");
      setRetrievalTestResult(payload);
    } catch (error) {
      setRetrievalTestResult(null);
      setRetrievalTestMessage(error instanceof Error ? error.message : "检索测试失败");
    } finally {
      setRetrievalTestBusy(false);
    }
  };

  const saveRetrievalFeedback = async (correctedQuery: string, scope: "global" | "project") => {
    const query = retrievalTestBaseQuery.trim();
    const corrected = correctedQuery.trim();
    if (!query || !corrected || retrievalFeedbackBusy || locked) return;
    if (scope === "project" && !activeProjectId) {
      setRetrievalTestMessage("没有当前项目，不能保存项目专属检索提示。");
      return;
    }
    setRetrievalFeedbackBusy(true);
    setRetrievalTestMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/diagnostics/retrieval/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          query,
          corrected_query: corrected,
          scope,
          project_id: activeProjectId,
          workflow_id: retrievalTestResult?.workflow_id || selectedWorkflowId || undefined,
        }),
      });
      const payload = await response.json() as { feedback?: RetrievalFeedback; error?: string };
      if (!response.ok || !payload.feedback) throw new Error(payload.error || "保存检索提示失败");
      setRetrievalTestResult((current) => {
        if (!current) return current;
        const oldFeedback = current.feedback || [];
        const nextFeedback = oldFeedback.filter((item) => item.feedback_id !== payload.feedback?.feedback_id);
        return { ...current, feedback: [payload.feedback!, ...nextFeedback] };
      });
      const workflowScoped = Boolean(retrievalTestResult?.workflow_id || selectedWorkflowId);
      setRetrievalTestMessage(workflowScoped
        ? (scope === "global" ? "已记住为所有项目的当前窗口检索提示。" : "已记住为当前项目的当前窗口检索提示。")
        : (scope === "global" ? "已记住为所有项目的检索提示。" : "已记住为当前项目的检索提示。"));
    } catch (error) {
      setRetrievalTestMessage(error instanceof Error ? error.message : "保存检索提示失败");
    } finally {
      setRetrievalFeedbackBusy(false);
    }
  };

  const revokeRetrievalFeedback = async (feedback: RetrievalFeedback) => {
    if (!feedback.feedback_id || retrievalFeedbackBusy || locked) return;
    if (!window.confirm("撤销这条检索提示？撤销后它不会再参与后续问答。")) return;
    setRetrievalFeedbackBusy(true);
    setRetrievalTestMessage("");
    try {
      const projectQuery = feedback.project_id
        ? `&project_id=${encodeURIComponent(feedback.project_id)}`
        : "";
      const response = await fetch(`${apiBaseUrl}/diagnostics/retrieval/feedback/${encodeURIComponent(feedback.feedback_id)}?confirm=true${projectQuery}`, {
        method: "DELETE",
        headers: { ...authHeaders() },
      });
      const payload = await response.json() as { error?: string };
      if (!response.ok) throw new Error(payload.error || "撤销检索提示失败");
      setRetrievalTestResult((current) => current ? {
        ...current,
        feedback: (current.feedback || []).filter((item) => item.feedback_id !== feedback.feedback_id),
      } : current);
      setRetrievalTestMessage("检索提示已撤销。");
    } catch (error) {
      setRetrievalTestMessage(error instanceof Error ? error.message : "撤销检索提示失败");
    } finally {
      setRetrievalFeedbackBusy(false);
    }
  };

  const authorizeDocumentDelete = async () => {
    if (!documentDeletePassphrase || documentDeleteBusy) return;
    setDocumentDeleteBusy(true);
    setDocumentDeleteMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/documents/delete-authorize`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ passphrase: documentDeletePassphrase }),
      });
      const payload = await response.json() as { authorization?: string; error?: string; detail?: string };
      if (!response.ok || !payload.authorization) {
        throw new Error(
          response.status === 404
            ? "本地服务尚未更新，请重启 SoulDrive 后重试。"
            : payload.error || payload.detail || "密码校验失败",
        );
      }
      setDocumentDeleteAuthorization(payload.authorization);
      setDocumentDeletePassphrase("");
      setDocumentDeleteMessage("本次文档库会话已完成删除校验。");
    } catch (error) {
      setDocumentDeleteMessage(error instanceof Error ? error.message : "密码校验失败");
    } finally {
      setDocumentDeleteBusy(false);
    }
  };

  const deleteDocument = async () => {
    if (!documentDeleteTarget || !documentDeleteAuthorization || documentDeleteBusy) return;
    setDocumentDeleteBusy(true);
    setDocumentDeleteMessage("正在删除文档并清理本地索引，请勿关闭 SoulDrive…");
    try {
      const response = await fetch(`${apiBaseUrl}/documents/delete`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...authHeaders(),
          ...(documentDeleteIdempotencyKey ? { "Idempotency-Key": documentDeleteIdempotencyKey } : {}),
        },
        body: JSON.stringify({
          name: documentDeleteTarget.name,
          document_id: documentDeleteTarget.document_id,
          authorization: documentDeleteAuthorization,
        }),
      });
      const payload = await response.json() as { error?: string; warning?: string; replayed?: boolean };
      if (!response.ok) throw new Error(payload.error || "删除失败");
      setActiveDocumentRefs((current) => current.filter((selected) => documentDeleteTarget.document_id
        ? selected.document_id !== documentDeleteTarget.document_id
        : selected.document_id || selected.name !== documentDeleteTarget.name));
      setDocumentDeleteTarget(null);
      setDocumentDeleteIdempotencyKey(null);
      setDocumentDeleteMessage(`文档及其本地索引已删除${payload.replayed ? "（已回放之前的删除结果）" : ""}。本次文档库会话无需重复校验密码。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
      const listResponse = await fetch(`${apiBaseUrl}/documents/list`, { headers: authHeaders() });
      if (listResponse.ok) setDocumentLibrary(await listResponse.json());
    } catch (error) {
      setDocumentDeleteMessage(error instanceof Error ? error.message : "删除失败");
    } finally {
      setDocumentDeleteBusy(false);
    }
  };

  const clearAudioStopTimer = () => {
    if (audioStopTimerRef.current !== null) {
      window.clearTimeout(audioStopTimerRef.current);
      audioStopTimerRef.current = null;
    }
  };

  const releaseAudioStream = () => {
    audioStreamRef.current?.getTracks().forEach((track) => track.stop());
    audioStreamRef.current = null;
  };

  const startBrowserSpeechRecognition = () => {
    const SpeechRecognition = (window as Window & { SpeechRecognition?: BrowserSpeechRecognitionConstructor; webkitSpeechRecognition?: BrowserSpeechRecognitionConstructor }).SpeechRecognition
      || (window as Window & { webkitSpeechRecognition?: BrowserSpeechRecognitionConstructor }).webkitSpeechRecognition;
    if (!SpeechRecognition) return;
    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "zh-CN";
    recognition.onresult = (event) => {
      const transcript = Array.from(event.results)
        .map((result) => result[0]?.transcript || "")
        .join("")
        .trim();
      if (transcript) {
        speechDraftRef.current = transcript;
        setInput(transcript);
      }
    };
    recognition.onerror = () => undefined;
    recognition.onend = () => undefined;
    try {
      recognition.start();
      speechRecognitionRef.current = recognition;
    } catch {
      speechRecognitionRef.current = null;
    }
  };

  const stopBrowserSpeechRecognition = () => {
    try { speechRecognitionRef.current?.stop(); } catch { /* already stopped */ }
    speechRecognitionRef.current = null;
  };

  const stopAudioRecording = (reason: "manual" | "limit" = "manual") => {
    audioCaptureRequestedRef.current = false;
    stopBrowserSpeechRecognition();
    const recorder = audioRecorderRef.current;
    if (recorder?.state !== "recording") return;
    audioStopReasonRef.current = reason;
    clearAudioStopTimer();
    recorder.stop();
  };

  useEffect(() => () => {
    audioCaptureRequestedRef.current = false;
    clearAudioStopTimer();
    const recorder = audioRecorderRef.current;
    if (recorder) {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      if (recorder.state !== "inactive") recorder.stop();
    }
    releaseAudioStream();
    stopBrowserSpeechRecognition();
  }, []);

  const handleMediaSelection = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    const audio = files.find((file) => file.type === "audio/wav" || file.name.toLowerCase().endsWith(".wav")) || null;
    const image = files.find((file) => file.type.startsWith("image/") || /\.(png|jpe?g|webp)$/i.test(file.name)) || null;
    setMediaFiles({ audio, image });
    invalidateMediaPerception();
    if (image) setUiAutomationSnapshot(null);
    setSelectedImageRegion(null);
    setImageSelectionDraft(null);
    setMediaPreviewOpen(true);
    setMediaMessage([audio?.name, image?.name].filter(Boolean).join(" + ") || "未选择支持的 WAV/图片");
    event.target.value = "";
  };

  const handleAskPaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const item = Array.from(event.clipboardData.items).find((candidate) => candidate.type.startsWith("image/"));
    const blob = item?.getAsFile();
    if (!blob) return;
    const image = new File([blob], `clipboard-${Date.now()}.png`, { type: blob.type || "image/png" });
    setMediaFiles((current) => ({ ...current, image }));
    invalidateMediaPerception();
    setUiAutomationSnapshot(null);
    setSelectedImageRegion(null);
    setImageSelectionDraft(null);
    setMediaPreviewOpen(true);
    setMediaMessage("已从剪贴板粘贴图片，可直接发送或继续输入问题");
  };

  const downloadCapturedMedia = (file: File | null) => {
    if (!file) return;
    const url = URL.createObjectURL(file);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  const startAudioRecording = async () => {
    if (mediaBusy || locked || mediaCaptureState !== "idle" || audioCaptureRequestedRef.current) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      setMediaMessage("当前运行环境不支持浏览器端录音");
      return;
    }

    audioCaptureRequestedRef.current = true;
    speechDraftRef.current = "";
    setSpeechConfirmationPending(false);
    setInput("");
    audioStopReasonRef.current = "manual";
    setMediaCaptureState("requesting_audio");
    setMediaMessage("正在请求麦克风授权...");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, sampleRate: 16_000, echoCancellation: true, noiseSuppression: true },
      });
      if (!audioCaptureRequestedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        setMediaCaptureState("idle");
        setMediaMessage("已取消录音");
        return;
      }

      const preferredType = "audio/webm;codecs=opus";
      const recorder = MediaRecorder.isTypeSupported(preferredType)
        ? new MediaRecorder(stream, { mimeType: preferredType })
        : new MediaRecorder(stream);
      audioStreamRef.current = stream;
      audioRecorderRef.current = recorder;
      audioChunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) audioChunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const stoppedAtLimit = audioStopReasonRef.current === "limit";
        clearAudioStopTimer();
        const recording = new Blob(audioChunksRef.current, { type: recorder.mimeType || "audio/webm" });
        audioRecorderRef.current = null;
        releaseAudioStream();
        audioCaptureRequestedRef.current = false;
        if (!recording.size) {
          setMediaCaptureState("idle");
          setMediaMessage("录音过短，未生成语音文件");
          return;
        }

        setMediaCaptureState("processing");
        setMediaMessage("正在转换为 16 kHz 单声道 WAV...");
        void recordedAudioToWavFile(recording, `voice-${Date.now()}.wav`)
          .then((audio) => {
            setMediaFiles((previous) => ({ ...previous, audio }));
            setSpeechConfirmationPending(Boolean(speechDraftRef.current.trim()));
            setMediaMessage(`${stoppedAtLimit ? "已在 15 秒上限自动停止，" : ""}已采集 ${audio.name}`);
          })
          .catch((error: unknown) => {
            setMediaMessage(error instanceof Error ? error.message : "语音转换失败");
          })
          .finally(() => setMediaCaptureState("idle"));
      };
      recorder.start();
      void requestPerceptionWarmup();
      startBrowserSpeechRecognition();
      audioStopTimerRef.current = window.setTimeout(() => {
        setMediaMessage("已达到 15 秒录音上限，正在结束录音...");
        stopAudioRecording("limit");
      }, MAX_AUDIO_RECORDING_MS);
      setMediaCaptureState("recording");
      setMediaMessage("实时语音预览中；松开后由本地模型结合语音、选区和截图复核");
    } catch (error) {
      audioCaptureRequestedRef.current = false;
      clearAudioStopTimer();
      releaseAudioStream();
      setMediaCaptureState("idle");
      setMediaMessage(error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError") ? "未获得麦克风授权" : "无法启动录音");
    }
  };

  const toggleAudioRecording = () => {
    if (mediaCaptureState === "recording") {
      stopAudioRecording();
      return;
    }
    void startAudioRecording();
  };

  const captureScreenFrame = async () => {
    if (mediaBusy || locked || mediaCaptureState !== "idle") return;
    if (!navigator.mediaDevices?.getDisplayMedia) {
      setMediaMessage("当前运行环境不支持屏幕授权采集");
      return;
    }

    setMediaCaptureState("capturing_screen");
    setMediaMessage("请选择要授权的窗口或屏幕...");
    try {
      const image = await captureDisplayFramePng(`screen-${Date.now()}.png`);
      setMediaFiles((previous) => ({ ...previous, image }));
      invalidateMediaPerception();
      setUiAutomationSnapshot(null);
      setSelectedImageRegion(null);
      setImageSelectionDraft(null);
      setMediaPreviewOpen(true);
      setMediaMessage(`已采集 ${image.name}`);
    } catch (error) {
      setMediaMessage(error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "AbortError") ? "已取消屏幕授权" : error instanceof Error ? error.message : "屏幕采集失败");
    } finally {
      setMediaCaptureState("idle");
    }
  };

  const warmupVisualModel = async (automatic = false): Promise<boolean> => {
    if (mediaBusy || locked || mediaCaptureState !== "idle" || !mediaFiles.image) return false;
    setMediaBusy(true);
    setMediaMessage(automatic ? "已选择深度视觉，正在后台预热本地模型..." : "正在预热本地视觉模型；首次可能需要几十秒...");
    try {
      const response = await fetch(`${apiBaseUrl}/multimodal/runtime/warmup`, {
        method: "POST",
        headers: authHeaders(),
      });
      const result = await response.json() as MultimodalWarmupResult;
      if (!response.ok || result.status !== "pass") {
        throw new Error(result.detail || result.code || "视觉模型预热失败");
      }
      setMediaMessage(result.already_loaded ? "本地视觉模型已就绪" : "本地视觉模型已预热，下一次深度视觉请求可直接开始");
      return true;
    } catch (error) {
      setMediaMessage(error instanceof Error ? error.message : "视觉模型预热失败");
      return false;
    } finally {
      setMediaBusy(false);
    }
  };

  useEffect(() => {
    const image = mediaFiles.image;
    const shouldPrewarm = shouldPrewarmVisualModel({
      visualMode,
      targetGrounding,
      query: input || speechDraftRef.current,
      workflowVisualMode: workflowPlan?.routing.visual_mode,
      uiTargetKind: uiAutomationSnapshot?.element?.target_kind,
    });
    if (!image || !shouldPrewarm || locked || mediaBusy || mediaCaptureState !== "idle") return;
    if (visualWarmupFileRef.current === image) return;
    visualWarmupFileRef.current = image;
    void warmupVisualModel(true).then((ready) => {
      if (!ready && visualWarmupFileRef.current === image) visualWarmupFileRef.current = null;
    });
  }, [
    input,
    locked,
    mediaBusy,
    mediaCaptureState,
    mediaFiles.image,
    targetGrounding,
    uiAutomationSnapshot?.element?.target_kind,
    visualMode,
    workflowPlan?.routing.visual_mode,
  ]);

  const startImageRegionSelection = (event: React.PointerEvent<HTMLImageElement>) => {
    if (mediaBusy || locked || !mediaFiles.image) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    imageSelectionStartRef.current = { x: event.clientX, y: event.clientY };
    invalidateMediaPerception();
    setImageSelectionDraft(null);
    setSelectedImageRegion(null);
  };

  const moveImageRegionSelection = (event: React.PointerEvent<HTMLImageElement>) => {
    const start = imageSelectionStartRef.current;
    if (!start) return;
    const next = imageRegionFromPointer(
      event.currentTarget.getBoundingClientRect(),
      start,
      { x: event.clientX, y: event.clientY },
    );
    setImageSelectionDraft(next);
  };

  const finishImageRegionSelection = (event: React.PointerEvent<HTMLImageElement>) => {
    const start = imageSelectionStartRef.current;
    imageSelectionStartRef.current = null;
    if (!start) return;
    const region = imageRegionFromPointer(
      event.currentTarget.getBoundingClientRect(),
      start,
      { x: event.clientX, y: event.clientY },
    );
    setImageSelectionDraft(null);
    if (!region || region.width < 0.005 || region.height < 0.005) {
      setSelectedImageRegion(null);
      setMediaMessage("选区太小，请拖动框选需要理解的区域；不框选则使用整张图片");
      return;
    }
    setSelectedImageRegion(region);
    setMediaPreviewOpen(false);
    setMediaMessage("已锁定图片选区；本轮只把该区域交给 OCR/VLM");
  };

  const cancelImageRegionSelection = () => {
    imageSelectionStartRef.current = null;
    setImageSelectionDraft(null);
  };

  const clearCapturedMedia = () => {
    if (mediaBusy) return;
    setMediaFiles({ audio: null, image: null });
    visualWarmupFileRef.current = null;
    setUiAutomationSnapshot(null);
    setSelectedImageRegion(null);
    setImageSelectionDraft(null);
    setMediaPreviewOpen(true);
    setSpeechConfirmationPending(false);
    setMultimodalDecision(null);
    invalidateMediaPerception();
    setMediaMessage("");
  };

  const clearCapturedMediaFile = (kind: "audio" | "image") => {
    if (mediaBusy) return;
    setMediaFiles((current) => ({ ...current, [kind]: null }));
    if (kind === "audio") {
      setSpeechConfirmationPending(false);
    } else {
      visualWarmupFileRef.current = null;
      setUiAutomationSnapshot(null);
      setSelectedImageRegion(null);
      setImageSelectionDraft(null);
      setMediaPreviewOpen(true);
    }
    invalidateMediaPerception();
    setMediaMessage(kind === "audio" ? "已删除录音" : "已删除截图");
  };

  const runLocalMediaPerception = async (speechOverride = "", workflowChecked = false): Promise<MultimodalRuntimeResult | null> => {
    const speechText = speechOverride.trim() || input.trim() || speechDraftRef.current.trim();
    if (mediaBusy || locked || !hasMediaPerceptionInput(mediaFiles.audio, mediaFiles.image, speechText)) return null;
    if (selectedWorkflowId && !workflowChecked && !(await ensureWorkflowReady(speechText))) return null;
    setMediaBusy(true);
    setMediaMessage("正在执行端侧感知...");
    try {
      if (!mediaFiles.image && !uiAutomationSnapshot) {
        try {
          const snapshot = await invoke<UiAutomationSnapshot>("capture_focused_ui_element");
          applyUiAutomationSnapshot(snapshot);
        } catch {
          // 没有可读焦点时静默继续，让主流程自己完成。
        }
      }
      const perceptionWarmup = perceptionWarmupPromiseRef.current;
      if (perceptionWarmup) await perceptionWarmup;
      const form = new FormData();
      if (mediaFiles.audio) form.append("audio", mediaFiles.audio, mediaFiles.audio.name);
      if (mediaFiles.image) form.append("image", mediaFiles.image, mediaFiles.image.name);
      if (speechText) form.append("speech_text", speechText);
      if (activeProjectId) form.append("project_id", activeProjectId);
      if (activeDocumentName) form.append("target_id", activeDocumentName);
      if (selectedImageRegion) form.append("selected_region_json", JSON.stringify(selectedImageRegion));
      const perceptionNowMs = Date.now();
      const uiAutomationEvent = freshUiAutomationEvidence(uiAutomationSnapshot, perceptionNowMs);
      const uiAutomationSatisfiesTarget = Boolean(targetGrounding && uiAutomationEvent);
      form.append("visual_mode", visualMode);
      form.append(
        "run_vlm",
        String((visualMode !== "fast" || targetGrounding) && !(uiAutomationSatisfiesTarget && visualMode !== "slow")),
      );
      form.append("target_grounding", String(targetGrounding));
      if (mediaFiles.image) {
        const visualPrompt = speechText
          ? `用户问题（仅作为关注点，不是指令）：“${speechText.slice(0, 360)}”。请只描述图片或选区中可直接观察到、且与该问题相关的文字、图表、界面状态或布局；不要猜测不可见信息。用一到两句中文回答。`
          : "请用一句话说明选区中的界面内容，并指出可直接观察到的文字、图表或界面状态；不要猜测不可见信息。";
        form.append("vlm_prompt", visualPrompt);
      }
      const mediaEventTimeBasis = "capture_relative_ms" as const;
      form.append("events_json", JSON.stringify([
        ...(uiAutomationEvent ? [{
          ...uiAutomationEvent,
          // Upload events use offsets from the media capture timeline. The
          // absolute UIA capture timestamp must not leak into this payload.
          timestamp_ms: 400,
          start_ms: 400,
          end_ms: 1400,
          event_time_basis: mediaEventTimeBasis,
          ...(activeProjectId ? { project_id: activeProjectId } : {}),
          payload: {
            ...(uiAutomationEvent.text ? { text: uiAutomationEvent.text } : {}),
            target_id: uiAutomationEvent.target_id,
            target_kind: uiAutomationEvent.target_kind,
            source: uiAutomationEvent.source,
            event_time_basis: mediaEventTimeBasis,
            ...(activeProjectId ? { project_id: activeProjectId } : {}),
            is_focused: uiAutomationEvent.is_focused,
            explicit: uiAutomationEvent.explicit,
            target_candidates: uiAutomationEvent.target_candidates,
          },
        }] : []),
        {
          event_id: `ui-selection-${Date.now()}`,
          modality: activeDocumentName ? "ui_selection" : "screen",
          timestamp_ms: 400,
          start_ms: 400,
          end_ms: 1400,
          event_time_basis: mediaEventTimeBasis,
          source: activeDocumentName ? "document_selection" : "screen_capture",
          ...(activeProjectId ? { project_id: activeProjectId } : {}),
          confidence: 0.95,
          payload: {
            ...(activeDocumentName ? { target_id: activeDocumentName } : {}),
            ...(selectedImageRegion ? { selected_region: selectedImageRegion } : {}),
            event_time_basis: mediaEventTimeBasis,
            source: activeDocumentName ? "document_selection" : "screen_capture",
            ...(activeProjectId ? { project_id: activeProjectId } : {}),
          },
        },
      ]));
      const response = await fetch(`${apiBaseUrl}/multimodal/runtime/infer-upload`, {
        method: "POST",
        headers: authHeaders(),
        body: form,
      });
      const result = await response.json() as MultimodalRuntimeResult;
      if (!response.ok) throw new Error((result as unknown as { error?: string }).error || "感知请求失败");
      setMultimodalRuntime(result);
      setMultimodalCapturedAtMs(Date.now());
      if (result.decision) setMultimodalDecision(result.decision);
      if (result.speech_text && !input.trim()) setInput(result.speech_text);
      const stageSummary = Object.entries(result.stages || {})
        .map(([name, stage]) => `${stageLabels[name] || name}:${statusLabels[stage.status] || stage.status}`)
        .join(" · ");
      setMediaMessage(`${statusLabels[result.status] || result.status} · ${stageSummary}`);
      return result;
    } catch (error) {
      setMultimodalRuntime(null);
      setMultimodalCapturedAtMs(null);
      setMediaMessage(error instanceof Error ? error.message : "感知请求失败");
      return null;
    } finally {
      setMediaBusy(false);
    }
  };

  const sendCapturedMedia = async (forceConfirmed = false) => {
    if (isGenerating || mediaBusy || locked) return;
    const query = input.trim() || speechDraftRef.current.trim();
    if (!hasMediaPerceptionInput(mediaFiles.audio, mediaFiles.image, query)) {
      setMediaMessage("没有识别到语音文字，请重录或直接输入问题");
      return;
    }
    if (speechConfirmationPending && !forceConfirmed) {
      setMediaMessage("请先确认识别文字无误，再点击确认发送");
      return;
    }
    if (!(await ensureWorkflowReady(query))) return;
    const cachedVisualMatchesQuery = Boolean(
      !mediaFiles.image ||
      multimodalRuntime?.stages?.vlm?.status !== "pass" ||
      multimodalRuntime?.speech_text?.trim() === query,
    );
    const cachedPerceptionIsFresh = Boolean(
      !mediaFiles.audio &&
      cachedVisualMatchesQuery &&
      multimodalRuntime &&
      multimodalCapturedAtMs &&
      Date.now() - multimodalCapturedAtMs <= 5 * 60 * 1000 &&
      JSON.stringify(multimodalRuntime.visual_context?.selected_region || null) === JSON.stringify(selectedImageRegion),
    );
    const result = cachedPerceptionIsFresh
      ? multimodalRuntime
      : await runLocalMediaPerception(query, true);
    if (!result || result.status === "invalid" || result.status === "blocked") {
      setMediaMessage("端侧感知未完成，本次未发送；可重试，或清除附件后仅发送文字");
      return;
    }
    const selectedRegionIsEnough = result.decision && canUseSelectedRegionWithoutClarification(
      result.decision.action,
      result.decision.clarification?.reason_codes,
      selectedImageRegion,
    );
    if ((result.decision?.action === "clarify" || result.decision?.action === "refuse") && !selectedRegionIsEnough) {
      setMultimodalDecision(result.decision);
      setMediaMessage(result.decision.clarification?.prompt || "当前目标未通过复核，本次未发送");
      return;
    }
    if (result.decision && selectedRegionIsEnough) {
      setMultimodalDecision({
        ...result.decision,
        action: "answer",
        clarification_required: false,
        clarification: undefined,
      });
      setMediaMessage("已锁定框选区域，正在进入知识问答。");
    }
    if (mediaFiles.audio && !result.speech_text.trim() && !query.trim()) {
      setMediaMessage("语音未得到可靠转写，本次未发送；请重录或确认文字后重试");
      return;
    }
    if (mediaFiles.image && !hasUsableVisualEvidence(result.perception_events || [])) {
      setMediaMessage("图片已进入端侧感知，但当前未形成可复用视觉证据；可尝试更精确框选后再发。");
      return;
    }
    if (mediaFiles.audio && !forceConfirmed) {
      const transcript = result.speech_text.trim() || query.trim();
      setInput(transcript);
      speechDraftRef.current = transcript;
      setSpeechConfirmationPending(true);
      setMediaMessage("本地转写已完成，请确认文字无误后再发送");
      return;
    }
    const resolvedQuery = result?.speech_text?.trim() || query;
    if (resolvedQuery) {
      await submitQuery(resolvedQuery, result, forceConfirmed);
    } else if (mediaFiles.image) {
      setMediaMessage("截图感知已完成，请在输入框补充问题后发送");
    } else {
      setMediaMessage("没有识别到语音文字，请重录或直接输入问题");
    }
    setSpeechConfirmationPending(false);
  };

  const updateReviewEdit = (candidateId: string, field: "title" | "content", value: string) => {
    setReviewState((previous) => {
      const currentEdit = previous.edits[candidateId] || { title: "", content: "" };
      return {
        ...previous,
        edits: {
          ...previous.edits,
          [candidateId]: {
            ...currentEdit,
            [field]: value,
          },
        },
      };
    });
  };

  const requestReflectionDraft = async (trigger: "manual") => {
    if (!messages.length || locked) {
      setReviewState((previous) => ({
        ...previous,
        review: null,
        mode: "done",
        message: locked ? "工作区未解锁，无法生成记忆候选。" : "当前会话没有可沉淀内容。",
      }));
      return null;
    }

    setReviewState((previous) => ({
      ...previous,
      mode: "drafting",
      message: "正在生成候选记忆...",
    }));

    const draftFingerprint = JSON.stringify({
      trigger,
      project_id: activeProjectId || null,
      workflow_id: selectedWorkflowId || null,
      messages,
    });
    if (reflectionDraftFingerprintRef.current !== draftFingerprint) {
      reflectionDraftFingerprintRef.current = draftFingerprint;
      reflectionDraftIdempotencyKeyRef.current = `reflection_${crypto.randomUUID()}`;
    }
    const idempotencyKey = reflectionDraftIdempotencyKeyRef.current;

    try {
      const response = await fetch(`${apiBaseUrl}/reflection/draft`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
          ...authHeaders(),
        },
        body: JSON.stringify({
          trigger,
          messages,
          project_id: activeProjectId || null,
          session_id: activeSessionId || null,
          workflow_id: selectedWorkflowId || null,
          request_id: idempotencyKey,
        }),
      });
      if (!response.ok) throw new Error("reflection draft failed");

      const review = await response.json() as ReflectionReview;
      const selectedIds = new Set(
        review.candidates.filter((candidate) => candidate.selected_by_default).map((candidate) => candidate.candidate_id),
      );
      const edits = Object.fromEntries(
        review.candidates.map((candidate) => [
          candidate.candidate_id,
          { title: candidate.title, content: candidate.content },
        ]),
      );

      setReviewState({
        review,
        selectedIds,
        edits,
        mode: review.candidates.length ? "reviewing" : "done",
        message: review.warning
          ? `候选记忆已生成，但${review.warning}`
          : (review.candidates.length ? "" : "本轮没有可沉淀内容。"),
      });
      reflectionDraftFingerprintRef.current = "";
      reflectionDraftIdempotencyKeyRef.current = "";
      return review;
    } catch {
      setReviewState((previous) => ({
        ...previous,
        review: null,
        mode: "error",
        message: "候选记忆生成失败。",
      }));
      return null;
    }
  };

  const submitReflectionDecisions = async () => {
    const activeReview = reviewState.review;
    if (!activeReview) return;

    setReviewState((previous) => ({
      ...previous,
      mode: "submitting",
      message: "正在写入长期记忆...",
    }));

    const decisions = activeReview.candidates.map((candidate) => {
      const accepted = reviewState.selectedIds.has(candidate.candidate_id);
      const edit = reviewState.edits[candidate.candidate_id] || {
        title: candidate.title,
        content: candidate.content,
      };
      return accepted
        ? {
            candidate_id: candidate.candidate_id,
            action: "accept",
            edited_title: edit.title,
            edited_content: edit.content,
          }
        : {
            candidate_id: candidate.candidate_id,
            action: "reject",
          };
    });

    try {
      const response = await fetch(`${apiBaseUrl}/reflection/reviews/${activeReview.review_id}/decide`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...authHeaders(),
        },
        body: JSON.stringify({ decisions, project_id: activeProjectId }),
      });
      const payload = await response.json() as { storage_sync?: { status?: string }; error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "reflection decide failed");
      setReviewState((previous) => ({
        ...previous,
        mode: "done",
        message: isLearningStorageSyncDegraded(payload)
          ? `记忆已保存，但归档/检索同步待恢复；后续打开记忆管理器或提问时会自动重试。${payload.warning ? ` 注意：${payload.warning}` : ""}`
          : `记忆已完成。${payload.warning ? ` 注意：${payload.warning}` : ""}`,
      }));
    } catch {
      setReviewState((previous) => ({
        ...previous,
        mode: "error",
        message: "记忆写入失败，请重试或放弃本次记忆。",
      }));
    }
  };

  const saveRuntimeMemory = async (
    message: string,
    sessionId: string | null,
    projectId: string | null,
    requestId: string | null,
  ) => {
    if (!sessionId || !projectId) return;
    try {
      const response = await fetch(`${apiBaseUrl}/learning/memories/runtime`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message, session_id: sessionId, project_id: projectId, request_id: requestId || undefined }),
      });
      if (!response.ok) return;
      const payload = await response.json() as { status?: string; memory_id?: string; project_id?: string | null; notice?: string; warning?: string };
      if (payload.status === "saved" && payload.memory_id) {
        setRuntimeMemoryNotice({
          memory_id: payload.memory_id,
          project_id: payload.project_id ?? projectId,
          text: `${payload.notice || "已自动记录一条用户偏好，可撤销。"}${payload.warning ? ` 注意：${payload.warning}` : ""}`,
        });
      }
    } catch {
      // 记忆保存是问答后的增量旁路，失败不能影响已经完成的回答。
    }
  };

  const revokeRuntimeMemory = async () => {
    const notice = runtimeMemoryNotice;
    if (!notice) return;
    try {
      const scopeQuery = notice.project_id ? `?project_id=${encodeURIComponent(notice.project_id)}` : "";
      const response = await fetch(`${apiBaseUrl}/learning/memories/${encodeURIComponent(notice.memory_id)}/revoke${scopeQuery}`, {
        method: "POST",
        headers: { ...authHeaders(), "Idempotency-Key": globalThis.crypto.randomUUID() },
      });
      const payload = await response.json() as { storage_sync?: { status?: string }; error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "memory revoke failed");
      setRuntimeMemoryNotice(null);
      setReviewState((previous) => ({
        ...previous,
        mode: "done",
        message: isLearningStorageSyncDegraded(payload)
          ? `已撤销刚才自动记录的记忆，但归档/检索同步待恢复。${payload.warning ? ` 注意：${payload.warning}` : ""}`
          : `已撤销刚才自动记录的记忆。${payload.warning ? ` 注意：${payload.warning}` : ""}`,
      }));
    } catch {
      setReviewState((previous) => ({ ...previous, mode: "error", message: "记忆撤销失败，请稍后重试。" }));
    }
  };

  const loadLearningMemories = async () => {
    if (locked || !runtimeConfigLoaded) {
      setMemoryManagerMessage("工作区未解锁，无法查看偏好画像。");
      return;
    }
    setMemoryManagerBusy(true);
    setMemoryManagerMessage("");
    try {
      const query = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
      const response = await fetch(`${apiBaseUrl}/learning/memories${query}`, {
        cache: "no-store",
        headers: authHeaders(),
      });
      const payload = await response.json() as { memories?: LearningMemory[]; health?: MemoryHealth; error?: string };
      if (!response.ok) throw new Error(payload.error || "memory list failed");
      const memories = Array.isArray(payload.memories) ? payload.memories : [];
      setLearningMemories(memories);
      setMemoryHealth(payload.health || null);
      setMemoryEdits(Object.fromEntries(
        memories.map((memory) => [memory.memory_id, { title: memory.title, content: memory.content }]),
      ));
    } catch {
      setLearningMemories([]);
      setMemoryHealth(null);
      setMemoryManagerMessage("偏好画像加载失败，请稍后重试。");
    } finally {
      setMemoryManagerBusy(false);
    }
  };

  const openMemoryManager = () => {
    setMemoryManagerOpen(true);
    void loadLearningMemories();
  };

  const updateMemoryEdit = (memoryId: string, field: "title" | "content", value: string) => {
    setMemoryEdits((previous) => ({
      ...previous,
      [memoryId]: {
        ...(previous[memoryId] || { title: "", content: "" }),
        [field]: value,
      },
    }));
  };

  const saveLearningMemory = async (memory: LearningMemory) => {
    const edit = memoryEdits[memory.memory_id] || { title: memory.title, content: memory.content };
    if (!edit.title.trim() || !edit.content.trim()) {
      setMemoryManagerMessage("标题和内容不能为空。");
      return;
    }
    setMemoryManagerBusy(true);
    setMemoryManagerMessage("");
    try {
      const scopeQuery = memory.project_id ? `?project_id=${encodeURIComponent(memory.project_id)}` : "";
      const response = await fetch(`${apiBaseUrl}/learning/memories/${encodeURIComponent(memory.memory_id)}${scopeQuery}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...authHeaders(),
          "Idempotency-Key": globalThis.crypto.randomUUID(),
        },
        body: JSON.stringify({
          title: edit.title,
          content: edit.content,
          expected_updated_at: memory.updated_at,
        }),
      });
      const payload = await response.json() as { memory?: LearningMemory; storage_sync?: { status?: string }; error?: string; warning?: string };
      if (!response.ok || !payload.memory) throw new Error(payload.error || "memory update failed");
      setLearningMemories((previous) => previous.map((item) => item.memory_id === memory.memory_id ? payload.memory! : item));
      setMemoryEdits((previous) => ({ ...previous, [memory.memory_id]: { title: payload.memory!.title, content: payload.memory!.content } }));
      setMemoryManagerMessage(isLearningStorageSyncDegraded(payload)
        ? `偏好已更新，但检索同步待恢复；后续打开记忆管理器或提问时会自动重试。${payload.warning ? ` 注意：${payload.warning}` : ""}`
        : `偏好已更新，检索索引已同步。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
    } catch (error) {
      setMemoryManagerMessage(error instanceof Error && error.message.includes("reload") ? "该偏好已被更新，请重新加载后再编辑。" : "偏好更新失败，请稍后重试。");
    } finally {
      setMemoryManagerBusy(false);
    }
  };

  const revokeLearningMemory = async (memory: LearningMemory) => {
    if (!window.confirm(`停用“${memory.title}”？停用后它不会再进入问答上下文，但仍保留加密审计记录。`)) return;
    setMemoryManagerBusy(true);
    setMemoryManagerMessage("");
    try {
      const scopeQuery = memory.project_id ? `?project_id=${encodeURIComponent(memory.project_id)}` : "";
      const response = await fetch(`${apiBaseUrl}/learning/memories/${encodeURIComponent(memory.memory_id)}/revoke${scopeQuery}`, {
        method: "POST",
        headers: { ...authHeaders(), "Idempotency-Key": globalThis.crypto.randomUUID() },
      });
      const payload = await response.json() as { storage_sync?: { status?: string }; error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "memory revoke failed");
      setLearningMemories((previous) => previous.filter((item) => item.memory_id !== memory.memory_id));
      setMemoryManagerMessage(isLearningStorageSyncDegraded(payload)
        ? `偏好已停用，但归档/检索同步待恢复；后续打开记忆管理器或提问时会自动重试。${payload.warning ? ` 注意：${payload.warning}` : ""}`
        : `偏好已停用，不会再被注入问答上下文。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
      if (runtimeMemoryNotice?.memory_id === memory.memory_id) setRuntimeMemoryNotice(null);
    } catch {
      setMemoryManagerMessage("偏好停用失败，请稍后重试。");
    } finally {
      setMemoryManagerBusy(false);
    }
  };

  const deleteLearningMemory = async (memory: LearningMemory) => {
    if (!window.confirm(`删除“${memory.title}”？删除后活动索引中将不再保留这条偏好。`)) return;
    setMemoryManagerBusy(true);
    setMemoryManagerMessage("");
    try {
      const scopeQuery = memory.project_id ? `&project_id=${encodeURIComponent(memory.project_id)}` : "";
      const response = await fetch(`${apiBaseUrl}/learning/memories/${encodeURIComponent(memory.memory_id)}?confirm=true${scopeQuery}`, {
        method: "DELETE",
        headers: { ...authHeaders(), "Idempotency-Key": globalThis.crypto.randomUUID() },
      });
      const payload = await response.json() as { storage_sync?: { status?: string }; error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "memory delete failed");
      setLearningMemories((previous) => previous.filter((item) => item.memory_id !== memory.memory_id));
      setMemoryManagerMessage(isLearningStorageSyncDegraded(payload)
        ? `偏好已删除，但归档/检索同步待恢复；后续打开记忆管理器或提问时会自动重试。${payload.warning ? ` 注意：${payload.warning}` : ""}`
        : `偏好已删除，活动索引和问答上下文均已移除。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
      if (runtimeMemoryNotice?.memory_id === memory.memory_id) setRuntimeMemoryNotice(null);
    } catch {
      setMemoryManagerMessage("偏好删除失败，请稍后重试。");
    } finally {
      setMemoryManagerBusy(false);
    }
  };

  const submitQuery = async (
    query: string,
    runtimeOverride?: MultimodalRuntimeResult | null,
    audioConfirmed = false,
  ): Promise<boolean> => {
    const userQuery = query.trim();
    if (!userQuery || isGenerating) return false;
    if (!activeProjectId || !activeSessionId) return false;
    if (indexBusy) {
      setMessages((previous) => [
        ...previous,
        { role: "user", content: userQuery, timestampMs: Date.now() },
        { role: "ai", content: "知识索引正在更新。为避免回答使用旧知识库，请等待索引完成后再提问。", timestampMs: Date.now() },
      ]);
      setInput("");
      return false;
    }

    const queryTimestampMs = Date.now();

    if (runtimeStatus?.locked) {
      setMessages((previous) => [
        ...previous,
        { role: "user", content: userQuery, timestampMs: queryTimestampMs },
        { role: "ai", content: `本地知识引擎已锁定：${runtimeStatus.reason}`, timestampMs: Date.now() },
      ]);
      setInput("");
      return false;
    }

    const history = messages
      .filter((message) => message.content.trim())
      .slice(-8)
      .map((message) => ({
        role: message.role === "ai" ? "assistant" : "user",
        // Evidence blocks belong to the UI artifact panel, not the next model request.
        content: parseMessageArtifacts(message.content).cleanContent.slice(0, 4000),
        ...(message.timestampMs !== undefined ? { timestamp_ms: message.timestampMs } : {}),
      }))
      .filter((message) => message.content);
    const activeContextModality = isImageDocumentName(activeDocumentName) ? "image" : "document";
    // The perception result may be produced immediately before this call.
    // Prefer the explicit value so React's asynchronous state update cannot
    // drop the current screenshot/OCR evidence from the chat request.
    const perceptionForQuery = runtimeOverride !== undefined ? runtimeOverride : multimodalRuntime;
    const perceptionCapturedAtMs = runtimeOverride !== undefined ? Date.now() : multimodalCapturedAtMs;
    const visualRegion = perceptionForQuery?.visual_context?.selected_region || selectedImageRegion;
    const runtimeEventTimeBasis = perceptionForQuery?.event_time_basis || "capture_relative_ms";
    const runtimePerceptionIsFresh = Boolean(
      perceptionForQuery && perceptionCapturedAtMs && queryTimestampMs - perceptionCapturedAtMs <= 5 * 60 * 1000,
    );
    const shouldAttachAmbientUiAutomation = shouldUseUiAutomationContext({
      query: userQuery,
      selectedText: uiAutomationSnapshot?.element?.selected_text,
    });
    const hasExplicitMediaContext = Boolean(
      mediaFiles.audio || mediaFiles.image || visualRegion || runtimeOverride !== undefined,
    );
    const runtimePerceptionEvents = runtimePerceptionIsFresh && (hasExplicitMediaContext || shouldAttachAmbientUiAutomation)
      ? (perceptionForQuery?.perception_events || [])
          .filter((event) => event.text?.trim() || event.selected_text?.trim() || event.bbox || (event.target_candidates?.length || 0) > 0)
          .map((event) => ({
            ...event,
            // Keep the runtime event's capture time and interval. A query
            // timestamp is not an event time and cannot anchor relative data.
            ...(event.project_id || !activeProjectId ? {} : { project_id: activeProjectId }),
            ...(event.event_time_basis ? {} : { event_time_basis: runtimeEventTimeBasis }),
            ...(event.event_time_origin_ms !== undefined && event.event_time_origin_ms !== null
              ? { event_time_origin_ms: event.event_time_origin_ms }
              : perceptionForQuery?.event_time_origin_ms !== undefined && perceptionForQuery.event_time_origin_ms !== null
                ? { event_time_origin_ms: perceptionForQuery.event_time_origin_ms }
                : {}),
            ...(event.modality === "audio" && audioConfirmed ? { explicit: true } : {}),
            ...(event.bbox || ((event.modality === "image" || event.modality === "image_region") && visualRegion) ? { bbox: event.bbox || visualRegion } : {}),
          }))
      : [];
    const uiAutomationEvent = visualRegion || mediaFiles.image || !shouldAttachAmbientUiAutomation
      ? null
      : runtimePerceptionEvents.some((event) => event.modality === "ui_automation")
      ? null
      : freshUiAutomationEvidence(uiAutomationSnapshot, queryTimestampMs);
    const uiAutomationCapturedAtMs = runtimePerceptionEvents.some((event) => event.modality === "ui_automation") || uiAutomationEvent
      ? uiAutomationSnapshot?.captured_at_ms || null
      : null;
    const selectedText = uiAutomationEvent?.selected_text
      || runtimePerceptionEvents.find((event) => event.selected_text?.trim())?.selected_text
      || undefined;
    const perceptionEvents = [
      ...(activeDocumentName ? [
          {
            event_id: `${activeContextModality}-${queryTimestampMs}`,
            modality: activeContextModality,
            timestamp_ms: queryTimestampMs,
            source_name: activeDocumentName,
            source: "active_document_context",
            project_id: activeProjectId,
            event_time_basis: "unix_epoch_ms",
            confidence: 1,
          },
        ] : []),
      ...runtimePerceptionEvents,
      ...(uiAutomationEvent ? [uiAutomationEvent] : []),
    ];

    const hasMultimodalContext = shouldRunMultimodalDecision({
      audio: mediaFiles.audio,
      image: mediaFiles.image,
      selectedRegion: visualRegion,
      perceptionEventCount: runtimePerceptionEvents.length,
      hasUiAutomation: Boolean(uiAutomationEvent),
      hasImageDocumentContext: activeContextModality === "image",
    });

    if (hasMultimodalContext) try {
      const decisionEvents = [
        {
          event_id: `speech-${queryTimestampMs}`,
          modality: "speech_text",
          start_ms: queryTimestampMs,
          end_ms: queryTimestampMs,
          event_time_basis: "unix_epoch_ms",
          source: "user_query",
          project_id: activeProjectId,
          payload: { text: userQuery },
          confidence: 1,
        },
        ...(activeDocumentNames.length || visualRegion
          ? [{
              event_id: `screen-${queryTimestampMs}`,
              modality: "screen",
              start_ms: queryTimestampMs,
              end_ms: queryTimestampMs,
              event_time_basis: "unix_epoch_ms",
              source: "active_screen_context",
              project_id: activeProjectId,
              payload: {
                document_name: activeDocumentName,
                document_names: activeDocumentNames,
                document_ids: activeDocumentIds,
                ...(visualRegion ? { selected_region: visualRegion } : {}),
                project_id: activeProjectId,
              },
              confidence: 1,
            }]
          : []),
        ...[...runtimePerceptionEvents, ...(uiAutomationEvent ? [uiAutomationEvent] : [])].map((event) => {
          const startMs = event.start_ms ?? event.timestamp_ms;
          const endMs = event.end_ms ?? startMs;
          return {
          ...event,
          // Target candidates are alignment artifacts, not generic image
          // prose. Keep UI Automation as its own high-reliability modality;
          // route OCR/VLM candidates through the target verifier modality
          // while keeping the original image event for visual evidence.
          ...(event.target_candidates?.length && event.modality !== "ui_automation" ? { modality: "image_region" as const } : {}),
          ...(startMs !== undefined ? { start_ms: startMs } : {}),
          ...(endMs !== undefined ? { end_ms: endMs } : {}),
          payload: {
            ...(event.text ? { text: event.text } : {}),
            ...(event.selected_text ? { selected_text: event.selected_text } : {}),
            ...(event.project_id || activeProjectId ? { project_id: event.project_id || activeProjectId } : {}),
            ...(event.source ? { source: event.source } : {}),
            ...(event.event_time_basis ? { event_time_basis: event.event_time_basis } : {}),
            ...(event.event_time_origin_ms !== undefined ? { event_time_origin_ms: event.event_time_origin_ms } : {}),
            ...(startMs !== undefined ? { start_ms: startMs } : {}),
            ...(endMs !== undefined ? { end_ms: endMs } : {}),
            ...(event.target_candidates ? { target_candidates: event.target_candidates } : {}),
          },
          };
        }),
      ];
      const decisionResponse = await fetch(`${apiBaseUrl}/multimodal/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          speech_text: userQuery,
          events: decisionEvents,
          active_project_id: activeProjectId,
          target_required: Boolean((perceptionForQuery?.target_grounding || targetGrounding) && uiAutomationSnapshot && !visualRegion),
        }),
      });
      if (decisionResponse.ok) {
        const decision = await decisionResponse.json() as MultimodalDecision;
        setMultimodalDecision(decision);
        const canBypassClarify = canUseSelectedRegionWithoutClarification(
          decision.action,
          decision.clarification?.reason_codes,
          visualRegion,
        );
        if ((decision.action === "clarify" || decision.action === "refuse") && !canBypassClarify) {
          const clarification = decision.clarification?.prompt || (
            decision.action === "refuse"
              ? "当前请求未通过本地安全边界，未进入知识问答。"
              : "当前目标尚未确认，未进入知识问答。"
          );
          setInput("");
          speechDraftRef.current = "";
          setMessages((previous) => [
            ...previous,
            { role: "user", content: userQuery, timestampMs: queryTimestampMs },
            { role: "ai", content: clarification, timestampMs: Date.now() },
          ]);
          return false;
        }
        if (canBypassClarify) {
          setMultimodalDecision({
            ...decision,
            action: "answer",
            clarification_required: false,
            clarification: undefined,
          });
          setMediaMessage("已采用框选区域继续回答，目标校验仅作为辅助提示。");
        }
      }
    } catch {
      setMultimodalDecision(null);
    }

    if (runtimeOverride !== undefined) clearCapturedMedia();
    setInput("");
    speechDraftRef.current = "";
    setMultimodalRuntime(null);
    setMultimodalCapturedAtMs(null);
    setMessages((previous) => [
      ...previous,
      { role: "user", content: userQuery, timestampMs: queryTimestampMs },
      { role: "ai", content: "", timestampMs: queryTimestampMs },
    ]);
    setIsGenerating(true);

    let receivedOutput = false;
    let chatCompleted = false;
    let timedOut = false;
    let timeout = 0;
    let chatRequestId: string | null = null;
    try {
      const requestId = globalThis.crypto.randomUUID();
      chatRequestId = requestId;
      const chatPayload = {
        query: userQuery,
        top_k: 6,
        workflow_id: selectedWorkflowId || undefined,
        session_id: activeSessionId,
        request_id: requestId,
        query_timestamp_ms: queryTimestampMs,
        history,
        perception_events: perceptionEvents,
          active_context: {
            project_id: activeProjectId,
            document_name: activeDocumentName || undefined,
            document_names: activeDocumentNames,
            document_ids: activeDocumentIds,
            ...(selectedWorkflowId === "experiment_compare" ? {
              baseline_run_id: baselineExperimentRunId || undefined,
              current_run_id: currentExperimentRunId || undefined,
            } : {}),
            selected_region: visualRegion || undefined,
            selected_text: selectedText,
            skill_id: selectedSkillId || undefined,
            ...(perceptionCapturedAtMs ? { captured_at_ms: perceptionCapturedAtMs } : {}),
          },
      };
      let response: Response | null = null;
      let lastError: unknown = null;
      const controller = new AbortController();
      const resetModelTimeout = () => {
        window.clearTimeout(timeout);
        timeout = window.setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, CHAT_RESPONSE_IDLE_TIMEOUT_MS);
      };
      resetModelTimeout();
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          response = await fetch(`${apiBaseUrl}/chat`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              ...(apiToken ? { "X-SoulDrive-Token": apiToken } : {}),
            },
            body: JSON.stringify(chatPayload),
            signal: controller.signal,
          });
          if (!response.ok) {
            const detail = (await response.text()).slice(0, 240);
            throw new ChatHttpError(response.status, detail);
          }
          if (!response.body) throw new Error("流式响应不可用");
          break;
        } catch (error) {
          lastError = error;
          response = null;
          if (error instanceof ChatHttpError || timedOut) break;
          if (attempt === 0) await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
      }
      if (!response?.body) throw lastError instanceof Error ? lastError : new Error("本地推理服务不可用");

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        setMessages((previous) => {
          const next = [...previous];
          const lastIndex = next.length - 1;
          next[lastIndex] = {
            ...next[lastIndex],
          content: next[lastIndex].content + chunk,
          };
          return next;
        });
        receivedOutput = true;
        resetModelTimeout();
      }
      chatCompleted = true;
    } catch (error) {
      console.error(error);
      const message = error instanceof ChatHttpError
        ? formatChatHttpFailure(error.status, error.detail)
        : formatChatTransportFailure({
            timedOut,
            sidecarReachable: await sidecarIsReachable(apiBaseUrl),
            receivedOutput,
          });
      setMessages((previous) => {
        const next = [...previous];
        next[next.length - 1].content = message;
        return next;
      });
    } finally {
      window.clearTimeout(timeout);
      setIsGenerating(false);
      if (chatCompleted) {
        if (uiAutomationCapturedAtMs) {
          setUiAutomationSnapshot((current) => current?.captured_at_ms === uiAutomationCapturedAtMs ? null : current);
        }
        void saveRuntimeMemory(userQuery, activeSessionId, activeProjectId, chatRequestId);
      }
    }
    return chatCompleted;
  };

  const startIndexing = async () => {
    if (locked || indexBusy || indexStartStatus === "starting") return;
    if (indexStartStatus === "idle") {
      indexRequestIdempotencyKeyRef.current = "";
      indexRequestRunIdRef.current = "";
    }
    if (!indexRequestIdempotencyKeyRef.current) {
      indexRequestIdempotencyKeyRef.current = crypto.randomUUID();
    }
    setIndexStartStatus("starting");
    try {
      const response = await fetch(`${apiBaseUrl}/index/run`, {
        method: "POST",
        headers: {
          ...authHeaders(),
          "Idempotency-Key": indexRequestIdempotencyKeyRef.current,
        },
      });
      const payload = await response.json().catch(() => ({})) as { run_id?: string | null; replayed?: boolean };
      if (payload.run_id) indexRequestRunIdRef.current = payload.run_id;
      if (!response.ok && response.status !== 409) {
        indexRequestIdempotencyKeyRef.current = "";
        indexRequestRunIdRef.current = "";
        throw new Error("indexer unavailable");
      }
      setIndexStartMessage(payload.replayed ? "已回放已有索引任务" : "索引任务已提交");
      setIndexStartStatus("idle");
    } catch {
      setIndexStartMessage("");
      setIndexStartStatus("error");
    }
  };

  const globalizeDocument = async (document: DocumentItem) => {
    if (document.scope !== "project" || documentScopeBusy || indexBusy) return;
    if (!window.confirm(`确定将“${document.name}”归为全局知识库吗？归类后所有项目都可以检索它。`)) return;
    setDocumentScopeBusy(true);
    setDocumentScopeMessage("正在解除项目绑定，请稍候…");
    try {
      const response = await fetch(`${apiBaseUrl}/documents/globalize`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: document.name, document_id: document.document_id }),
      });
      const payload = await response.json() as { error?: string; reindex_required?: boolean; warning?: string };
      if (!response.ok) throw new Error(payload.error || "文档归类失败");
      const listResponse = await fetch(`${apiBaseUrl}/documents/list`, { headers: authHeaders() });
      if (listResponse.ok) setDocumentLibrary(await listResponse.json());
      if (payload.reindex_required) {
        setDocumentScopeMessage(`已归为全局知识库，正在重建知识索引与 GraphRAG…${payload.warning ? ` 注意：${payload.warning}` : ""}`);
        await startIndexing();
      } else {
        setDocumentScopeMessage(`该文档已经属于全局知识库。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
      }
    } catch (error) {
      setDocumentScopeMessage(error instanceof Error ? error.message : "文档归类失败");
    } finally {
      setDocumentScopeBusy(false);
    }
  };

  const openPresentationBuilder = () => {
    if (!activeProjectId) {
      setPresentationMessage("请先选择一个项目，再生成可追溯的 PPT 产物。");
      return;
    }
    const selectedDocuments = activeDocumentRefs.length
      ? presentationDocuments.filter((document) => activeDocumentRefs.some((selected) => document.document_id
        ? selected.document_id === document.document_id
        : !selected.document_id && selected.name === document.name))
      : presentationDocuments.slice(0, 3);
    setPresentationSources((previous) => previous.length
      ? previous
      : selectedDocuments.slice(0, 12).map((document) => ({
        name: document.name,
        ...(document.document_id ? { document_id: document.document_id } : {}),
      })));
    setPresentationStyleSkillId("");
    if (!presentationTitle.trim() && activeDocumentNames.length) setPresentationTitle("论文精读组会汇报");
    setPresentationTask(null);
    setPresentationRequestId(crypto.randomUUID());
    setPresentationMessage("");
    setPresentationOpen(true);
  };

  const selectRecentPresentationSources = () => {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const recent = presentationDocuments
      .filter((document) => document.modified_at * 1000 >= cutoff)
      .slice(0, 12);
    setPresentationSources(recent.map((document) => ({
      name: document.name,
      ...(document.document_id ? { document_id: document.document_id } : {}),
    })));
    setPresentationTitle(recent.length ? "近七天论文阅读组会汇报" : "");
    setPresentationMessage(recent.length ? `已按本地导入/保存时间选中 ${recent.length} 份文档` : "最近七天没有已索引文档");
  };

  const isPresentationSource = (document: DocumentItem) => presentationSources.some((selected) => (
    document.document_id
      ? selected.document_id === document.document_id
      : !selected.document_id && selected.name === document.name
  ));

  const syncProjectSkills = async () => {
    setSkillBusy(true);
    setSkillMessage("正在同步项目 Skills...");
    try {
      const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
      const payload = await requestSkillApi<{ skills?: LearningSkill[]; summary?: Record<string, number>; path?: string; warning?: string }>("POST", `/learning/skills/sync${projectQuery}`);
      setLearningSkills(payload.skills || []);
      if (payload.path) setSkillRootPath(payload.path);
      const summary = payload.summary || {};
      setSkillMessage(`已同步 ${summary.imported || 0} 个新增、${summary.updated || 0} 个更新（目录：${payload.path || "skills"}）${payload.warning ? `；${payload.warning}` : ""}`);
    } catch (error) {
      setSkillMessage(error instanceof Error ? error.message : "同步项目 Skills 失败");
    } finally {
      setSkillBusy(false);
    }
  };

  const openSkillDirectory = async () => {
    if (!skillRootPath) {
      setSkillMessage("尚未获得本地 Skill 目录，请先点击“刷新列表”。");
      return;
    }
    try {
      await invoke("open_path", { path: skillRootPath });
      setSkillMessage("已打开本地 Skill 目录；可在目录中查看或编辑 SKILL.md、样式适配器和脚本。脚本不会被系统自动执行。");
    } catch {
      setSkillMessage("本地 Skill 目录打开失败，请检查目录是否仍然存在。");
    }
  };

  const openSkillSourceInEditor = async (skill: LearningSkill) => {
    const target = resolveLocalSkillFile(skillRootPath, skill.source_path || "");
    if (!target) {
      setSkillMessage("当前 Skill 没有可定位的本地 SKILL.md，请先刷新列表。");
      return;
    }
    try {
      const editor = await invoke<string>("open_in_editor", { path: target });
      setSkillMessage(`已用${editor === "vscode" ? "VS Code" : "本地文本编辑器"}打开 SKILL.md；保存后点击刷新列表，脚本不会被系统自动执行。`);
    } catch (error) {
      setSkillMessage(error instanceof Error ? error.message : "本地编辑器打开失败，请先点击打开本地目录。");
    }
  };

  const installProjectSkill = async () => {
    if (!skillSource.trim()) return;
    setSkillBusy(true);
    setSkillMessage("正在下载到待审核区，不会执行其中脚本...");
    try {
      const payload = await requestSkillApi<SkillInboxItem>("POST", "/learning/skills/install", { source: skillSource.trim(), target: skillInstallTarget, project_id: activeProjectId });
      setSkillInbox((previous) => [payload, ...previous.filter((item) => item.skill_name !== payload.skill_name)]);
      setSkillSource("");
      const replayLabel = payload.replayed ? "已在待审核区，回放了已有内容" : "已下载到待审核区";
      setSkillMessage(`「${payload.skill_title}」${replayLabel}并归类为${payload.target === "presentation" ? "PPT" : "对话"} Skill，请审核来源后批准；适配和验证将自动完成。${payload.warning ? `；${payload.warning}` : ""}`);
    } catch (error) {
      setSkillMessage(error instanceof Error ? error.message : "Skill 下载失败");
    } finally {
      setSkillBusy(false);
    }
  };

  const approveProjectSkill = async (skill: SkillInboxItem) => {
    setSkillBusy(true);
    setSkillMessage(`正在生成「${skill.skill_title}」的本地适配契约...`);
    try {
      const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
      const payload = await requestSkillApi<SkillAdapterReview>("POST", `/learning/skills/inbox/${encodeURIComponent(skill.skill_name)}/approve${projectQuery}`);
      setSkillAdapterReview(payload);
      const replayLabel = payload.replayed ? "已回放已有适配契约" : "适配契约已生成";
      setSkillMessage(`「${skill.skill_title}」${replayLabel}，请人工查看后确认启用。${payload.warning ? `；${payload.warning}` : ""}`);
    } catch (error) {
      setSkillMessage(error instanceof Error ? error.message : "Skill 批准失败");
    } finally {
      setSkillBusy(false);
    }
  };

  const activateReviewedSkill = async () => {
    if (!skillAdapterReview) return;
    setSkillBusy(true);
    try {
      const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
      const payload = await requestSkillApi<{ skills?: LearningSkill[]; replayed?: boolean; warning?: string }>("POST", `/learning/skills/inbox/${encodeURIComponent(skillAdapterReview.skill_name)}/activate${projectQuery}`);
      setLearningSkills(payload.skills || []);
      setSkillInbox((previous) => previous.filter((item) => item.skill_name !== skillAdapterReview.skill_name));
      setSkillAdapterReview(null);
      const replayLabel = payload.replayed ? "已回放已启用结果" : "已确认启用";
      setSkillMessage(`「${skillAdapterReview.skill_title || skillAdapterReview.skill_name}」${replayLabel}。${payload.warning ? `；${payload.warning}` : ""}`);
    } catch (error) {
      setSkillMessage(error instanceof Error ? error.message : "Skill 启用失败");
    } finally {
      setSkillBusy(false);
    }
  };

  const deleteProjectSkill = async (skill: LearningSkill) => {
    setSkillBusy(true);
    setSkillDeletingId(skill.skill_id);
    setSkillMessage(`正在删除「${skill.title}」...`);
    const deleteKeyId = `${activeProjectId || "global"}:${skill.skill_id}`;
    const idempotencyKey = skillDeleteIdempotencyKeyRef.current[deleteKeyId] || `skill-delete-${crypto.randomUUID()}`;
    skillDeleteIdempotencyKeyRef.current[deleteKeyId] = idempotencyKey;
    try {
      const projectQuery = activeProjectId ? `?project_id=${encodeURIComponent(activeProjectId)}` : "";
      const payload = await requestSkillApi<{ skills?: LearningSkill[]; warning?: string }>("DELETE", `/learning/skills/${encodeURIComponent(skill.skill_id)}${projectQuery}`, undefined, idempotencyKey);
      setLearningSkills(payload.skills || []);
      delete skillDeleteIdempotencyKeyRef.current[deleteKeyId];
      setSkillMessage(payload.warning ? `「${skill.title}」已删除；${payload.warning}` : `「${skill.title}」已从当前项目删除。`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Skill 删除失败";
      setSkillMessage(message);
    } finally {
      setSkillDeletingId("");
      setSkillBusy(false);
    }
  };

  const generatePresentation = async () => {
    if (!presentationSources.length || ["pending", "running"].includes(presentationTask?.status || "")) return;
    const hasIdentifiedPresentationSource = presentationSources.some((source) => Boolean(source.document_id));
    const hasLegacyPresentationSource = presentationSources.some((source) => !source.document_id);
    if (hasIdentifiedPresentationSource && hasLegacyPresentationSource) {
      setPresentationMessage("安全对象与普通文件不能混合生成 PPT，请分开选择后重试；这样才能保持来源对象身份可追溯。");
      return;
    }
    const requestId = presentationTask?.status === "cancelled"
      ? crypto.randomUUID()
      : presentationRequestId || crypto.randomUUID();
    setPresentationRequestId(requestId);
    setPresentationMessage("正在提取文档证据并生成实际大纲...");
    const presentationSourceIds = presentationSources.every((source) => Boolean(source.document_id))
      ? presentationSources.map((source) => source.document_id as string)
      : [];
    try {
      const response = await fetch(`${apiBaseUrl}/presentations/generate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiToken ? { "X-SoulDrive-Token": apiToken } : {}),
        },
        body: JSON.stringify({
          request_id: requestId,
          source_filenames: presentationSources.map((source) => source.name),
          source_document_ids: presentationSourceIds,
          project_id: activeProjectId,
          title: presentationTitle,
          audience: presentationAudience,
          max_slides: presentationMaxSlides,
          open_in_wps: presentationOpenInWps,
          style_skill_id: presentationStyleSkillId || null,
          ...(selectedWorkflowId === "experiment_compare" ? {
            baseline_run_id: baselineExperimentRunId || undefined,
            current_run_id: currentExperimentRunId || undefined,
          } : {}),
        }),
      });
      const payload = await response.json() as PresentationTask & { error?: string };
      if (!response.ok) throw new Error(payload.error || "presentation generation failed");
      setPresentationTask(payload);
      setPresentationMessage(
        payload.replayed
          ? payload.status === "completed"
            ? "已回放已完成的 PPT 大纲，可查看并确认。"
            : "已回放已有 PPT 任务，界面将继续显示其执行状态。"
          : payload.worker_started === false
            ? "PPT 任务已提交，等待本地 worker 接管。"
            : "正在提取文档证据并生成实际大纲...",
      );
    } catch (error) {
      setPresentationMessage(error instanceof Error ? error.message : "PPT 任务创建失败");
    }
  };

  const confirmPresentation = async () => {
    const planDigest = presentationTask?.plan_digest || presentationTask?.result?.plan_digest;
    if (!presentationTask || !planDigest) return;
    const projectQuery = presentationTask.project_id
      ? `?project_id=${encodeURIComponent(presentationTask.project_id)}`
      : "";
    setPresentationMessage("正在确认大纲并创建 PPT 生成任务...");
    try {
      const response = await fetch(`${apiBaseUrl}/presentations/tasks/${presentationTask.task_id}/confirm${projectQuery}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ plan_digest: planDigest }),
      });
      const payload = await response.json() as PresentationTask & { error?: string };
      if (!response.ok) throw new Error(payload.error || "大纲确认失败");
      setPresentationTask(payload);
      setPresentationMessage("大纲已确认，正在生成可编辑 PPTX...");
    } catch (error) {
      setPresentationMessage(error instanceof Error ? error.message : "大纲确认失败");
    }
  };

  const cancelPresentation = async () => {
    if (!presentationTask || !["pending", "running"].includes(presentationTask.status)) return;
    const projectQuery = presentationTask.project_id
      ? `?project_id=${encodeURIComponent(presentationTask.project_id)}`
      : "";
    setPresentationMessage("正在停止，等待当前步骤结束...");
    try {
      const response = await fetch(`${apiBaseUrl}/presentations/tasks/${presentationTask.task_id}/cancel${projectQuery}`, {
        method: "POST",
        headers: authHeaders(),
      });
      const payload = await response.json() as PresentationTask & { error?: string };
      if (!response.ok) throw new Error(payload.error || "停止任务失败");
      setPresentationTask(payload);
    } catch (error) {
      setPresentationMessage(error instanceof Error ? error.message : "停止任务失败");
    }
  };

  const resetPresentationPlan = () => {
    setPresentationTask(null);
    setPresentationRequestId(crypto.randomUUID());
    setPresentationMessage("已返回参数编辑；提交后会生成新的大纲请求");
  };

  const selectPresentationTask = (task: PresentationTask) => {
    if (task.project_id !== activeProjectId) return;
    setPresentationTask(task);
    setPresentationSources((task.source_filenames || []).map((name, index) => ({
      name, ...(task.source_document_ids?.[index] ? { document_id: task.source_document_ids[index] } : {}),
    })));
    setPresentationMessage(task.cancellation_pending ? "正在停止，等待当前步骤结束..." : "已找回持久化任务；查看不会自动执行或打开文件。");
  };

  const resumePresentation = async () => {
    if (!presentationTask) return;
    const projectQuery = presentationTask.project_id
      ? `?project_id=${encodeURIComponent(presentationTask.project_id)}`
      : "";
    setPresentationMessage("正在从检查点恢复...");
    try {
      const response = await fetch(`${apiBaseUrl}/presentations/tasks/${presentationTask.task_id}/resume${projectQuery}`, {
        method: "POST",
        headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
      });
      const payload = await response.json() as PresentationTask & { error?: string };
      if (!response.ok) throw new Error(payload.error || "resume failed");
      setPresentationTask(payload);
      setPresentationPollRefresh((value) => value + 1);
    } catch (error) {
      setPresentationMessage(error instanceof Error ? error.message : "恢复失败");
    }
  };

  const selectDraftKind = (kind: "email" | "schedule") => {
    setDraftKind(kind);
    setDraftArtifact(null);
    setEditingScheduleArtifactId(null);
    if (kind === "schedule") setScheduleSoundEnabled(true);
    if (kind !== "email") setEmailAttachmentRefs([]);
    setDraftMessage("仅生成本地草稿，不会外发邮件或写入日历。");
  };

  const openDraftBuilder = (kind: "email" | "schedule") => {
    selectDraftKind(kind);
    if (kind === "email" && !activeProjectId) setDraftMessage("请先选择一个项目，再创建邮件草稿。");
    setDraftOpen(true);
  };

  const createProject = async () => {
    const name = projectCreateName.trim();
    if (locked || !name || projectCreateBusy) return;
    const projectFingerprint = JSON.stringify({
      name,
      project_mode: projectCreateMode,
      root_path: projectCreateRoot.trim(),
    });
    if (projectCreateFingerprintRef.current !== projectFingerprint) {
      projectCreateFingerprintRef.current = projectFingerprint;
      projectCreateIdempotencyKeyRef.current = crypto.randomUUID();
    }
    if (!projectCreateIdempotencyKeyRef.current) {
      projectCreateIdempotencyKeyRef.current = crypto.randomUUID();
    }
    setProjectCreateBusy(true);
    setProjectCreateMessage("正在创建本地项目...");
    try {
      const response = await fetch(`${apiBaseUrl}/projects`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...authHeaders(),
          "Idempotency-Key": projectCreateIdempotencyKeyRef.current,
        },
        body: JSON.stringify({ name, project_mode: projectCreateMode, root_path: projectCreateRoot.trim() || undefined }),
      });
      const payload = await response.json() as { project_id?: string; replayed?: boolean; error?: string; warning?: string };
      if (!response.ok || !payload.project_id) throw new Error(payload.error || "项目创建失败");
      projectCreateFingerprintRef.current = "";
      projectCreateIdempotencyKeyRef.current = "";
      setProjectCreateName("");
      setProjectCreateMode("research");
      setProjectCreateRoot("");
      setProjectCreateOpen(false);
      setProjectCreateMessage("");
      setProjectMessage(`${payload.replayed ? "项目创建请求已回放，仍使用已有项目。" : "本地项目已创建。"}${payload.warning ? ` 注意：${payload.warning}` : ""}`);
      setActiveProjectId(payload.project_id);
      const listResponse = await fetch(`${apiBaseUrl}/projects`, { headers: authHeaders() });
      if (listResponse.ok) {
        const listPayload = await listResponse.json() as { projects?: ProjectSummary[] };
        setProjects(Array.isArray(listPayload.projects) ? listPayload.projects : []);
      }
    } catch (error) {
      setProjectCreateMessage(error instanceof Error ? error.message : "项目创建失败");
    } finally {
      setProjectCreateBusy(false);
    }
  };

  const deleteProject = async (project: ProjectSummary) => {
    if (locked || isGenerating || indexBusy || sessionLoading || projectDeleteBusy) return;
    if (!window.confirm(
      `确定删除项目“${project.name}”吗？项目索引、项目会话、项目记录和登记的原始项目文件夹都会永久删除。`
    )) return;
    setProjectDeleteBusy(true);
    setProjectMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(project.project_id)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      const payload = await response.json() as { error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "项目删除失败");
      let remaining = projects.filter((item) => item.project_id !== project.project_id);
      const listResponse = await fetch(`${apiBaseUrl}/projects`, { headers: authHeaders() });
      if (listResponse.ok) {
        const listPayload = await listResponse.json() as { projects?: ProjectSummary[] };
        remaining = Array.isArray(listPayload.projects) ? listPayload.projects : remaining;
      }
      setProjects(remaining);
      setProjectListUnavailable(false);
      window.alert(`项目“${project.name}”及其原始项目文件夹已删除。${payload.warning ? `\n\n注意：${payload.warning}` : ""}`);
      if (activeProjectId === project.project_id) {
        setActiveProjectId(remaining[0]?.project_id || null);
        setProjectSessions([]);
        setActiveSessionId(null);
        setMessages([]);
        setProjectOverview(null);
        setGraphRagDiagnostics(null);
      }
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "项目删除失败");
    } finally {
      setProjectDeleteBusy(false);
    }
  };

  const selectProjectFolder = async () => {
    try {
      const selected = await invoke<string | null>("select_project_folder");
      if (selected) setProjectCreateRoot(selected);
    } catch (error) {
      setProjectCreateMessage(error instanceof Error ? error.message : "选择项目目录失败");
    }
  };

  const selectProjectSession = async (session: ProjectSession) => {
    if (
      isGenerating
      || !apiToken
      || !activeProjectId
      || (session.session_id === activeSessionId && messages.length > 0)
    ) return;
    const requestId = ++sessionLoadRequestRef.current;
    setSessionLoading(true);
    resetSessionScopedContext();
    setMessages([]);
    setSessionMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/sessions/${encodeURIComponent(session.session_id)}/history?limit=32&project_id=${encodeURIComponent(activeProjectId)}`, { headers: authHeaders() });
      if (!response.ok) throw new Error("session history unavailable");
      const payload = await response.json() as { history?: Array<{ role?: string; content?: string; timestamp_ms?: number; created_at?: number }> };
      if (requestId !== sessionLoadRequestRef.current) return;
      setActiveSessionId(session.session_id);
      setSelectedWorkflowId((session.workflow_id || "") as MultimodalWorkflowId | "");
      setMessages((payload.history || []).filter((item) => item.content?.trim()).map((item) => ({
        role: item.role === "assistant" || item.role === "ai" ? "ai" : "user",
        content: String(item.content),
        timestampMs: item.timestamp_ms ?? (item.created_at ? Math.round(item.created_at * 1000) : undefined),
      })));
    } catch {
      if (requestId !== sessionLoadRequestRef.current) return;
      // 历史读取失败时明确退出当前会话，避免把未加载的历史误当成
      // 空会话继续写入；页面保留可见的重试提示，不会变成空白页。
      setActiveSessionId(null);
      setMessages([]);
      setSessionMessage("会话历史暂时无法读取，请稍后重试。");
    } finally {
      if (requestId === sessionLoadRequestRef.current) setSessionLoading(false);
    }
  };

  const openProjectSessionDialog = () => {
    if (locked || !activeProjectId || !apiToken || isGenerating || sessionLoading) return;
    setSessionCreateName("");
    sessionCreateFingerprintRef.current = "";
    sessionCreateIdRef.current = "";
    // Keep the current business as the default for a new session.  This prevents
    // two sessions created from the same business view from silently diverging.
    setSessionCreateWorkflowId(selectedWorkflowId);
    setSessionMessage("");
    setSessionCreateOpen(true);
  };

  const createProjectSession = async () => {
    const title = sessionCreateName.trim();
    if (locked || !activeProjectId || !apiToken || isGenerating || !title) {
      if (!title) setSessionMessage("请先填写会话名称");
      return;
    }
    setSessionLoading(true);
    setSessionMessage("");
    try {
      const sessionFingerprint = [activeProjectId, title, sessionCreateWorkflowId || ""].join("|\u001f|");
      if (sessionCreateFingerprintRef.current !== sessionFingerprint) {
        sessionCreateFingerprintRef.current = sessionFingerprint;
        sessionCreateIdRef.current = `session_${crypto.randomUUID()}`;
      }
      const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          session_id: sessionCreateIdRef.current,
          title,
          workflow_id: sessionCreateWorkflowId || undefined,
        }),
      });
      const payload = await response.json() as { session?: ProjectSession; replayed?: boolean; error?: string; warning?: string };
      if (!response.ok || !payload.session) throw new Error(payload.error || "新建对话失败");
      setProjectSessions((current) => [payload.session!, ...current]);
      setActiveSessionId(payload.session.session_id);
      setSelectedWorkflowId((payload.session.workflow_id || "") as MultimodalWorkflowId | "");
      resetSessionScopedContext();
      setMessages([]);
      setSessionCreateName("");
      setSessionCreateWorkflowId("");
      setSessionCreateOpen(false);
      sessionCreateFingerprintRef.current = "";
      sessionCreateIdRef.current = "";
      setSessionMessage(`${payload.replayed ? "会话创建请求已回放，仍使用已有会话。" : "会话已创建。"}${payload.warning ? ` 注意：${payload.warning}` : ""}`);
    } catch (error) {
      setSessionMessage(error instanceof Error ? error.message : "新建会话失败");
    } finally {
      setSessionLoading(false);
    }
  };

  const deleteProjectSession = async (session: ProjectSession) => {
    if (locked || !activeProjectId || !apiToken || isGenerating || sessionLoading) return;
    if (!window.confirm(`确定删除会话“${session.title || "未命名对话"}”吗？该会话的历史消息将从本地归档、Redis 和 PostgreSQL 控制面移除。`)) return;
    const requestId = ++sessionLoadRequestRef.current;
    setSessionLoading(true);
    setSessionMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/sessions/${encodeURIComponent(session.session_id)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      const payload = await response.json() as { error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "删除会话失败");
      if (requestId !== sessionLoadRequestRef.current) return;
      setSessionMessage(payload.warning ? `会话已删除。注意：${payload.warning}` : "会话已删除。");
      const remaining = projectSessions.filter((item) => item.session_id !== session.session_id);
      setProjectSessions(remaining);
      if (activeSessionId === session.session_id) {
        setActiveSessionId(null);
        setMessages([]);
        if (remaining[0]) {
          await selectProjectSession(remaining[0]);
        } else {
          const created = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(activeProjectId)}/sessions`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ...authHeaders() },
            body: JSON.stringify({ title: "主对话" }),
          });
          const createdPayload = await created.json() as { session?: ProjectSession; error?: string };
          if (!created.ok || !createdPayload.session) {
            throw new Error(createdPayload.error || "恢复主对话失败");
          }
          setProjectSessions([createdPayload.session]);
          setActiveSessionId(createdPayload.session.session_id);
          setMessages([]);
        }
      }
    } catch (error) {
      if (requestId === sessionLoadRequestRef.current) {
        setSessionMessage(error instanceof Error ? error.message : "删除会话失败");
      }
    } finally {
      if (requestId === sessionLoadRequestRef.current) setSessionLoading(false);
    }
  };

  const createLocalContact = async () => {
    if (locked || (draftKind === "email" && !activeProjectId) || !contactName.trim() || !contactEmail.trim()) return;
    setDraftBusy(true);
    setContactMessage("正在保存本地联系人...");
    try {
      const response = await fetch(`${apiBaseUrl}/contacts`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          display_name: contactName.trim(),
          email: contactEmail.trim(),
          aliases: contactAliases.split(/[，,\n]/).map((value) => value.trim()).filter(Boolean),
          // 联系人是本机通讯录记录，不应因为当前项目切换而消失。
          // 项目仍由草稿/资产决定，联系人本身保持全局可复用。
          project_ids: [],
        }),
      });
      const payload = await response.json() as { contact_id?: string; replayed?: boolean; error?: string; warning?: string };
      if (!response.ok || !payload.contact_id) throw new Error(payload.error || "本地联系人保存失败");
      const contact: LocalContact = {
        contact_id: payload.contact_id,
        display_name: contactName.trim(),
        email: contactEmail.trim(),
        aliases: contactAliases.split(/[，,\n]/).map((value) => value.trim()).filter(Boolean),
        project_ids: [],
      };
      setContacts((current) => [contact, ...current.filter((item) => item.contact_id !== contact.contact_id)]);
      setEmailRecipient(contactName.trim());
      setScheduleParticipant(contactName.trim());
      setContactName("");
      setContactEmail("");
      setContactAliases("");
      setContactDialogOpen(false);
      setContactMessage("");
      setDraftMessage(`${payload.replayed
        ? "本地联系人请求已回放，未新增重复记录。"
        : "本地联系人已保存，可用姓名或别名创建草稿。"}${payload.warning ? ` 注意：${payload.warning}` : ""}`);
    } catch (error) {
      setContactMessage(error instanceof Error ? error.message : "本地联系人保存失败");
    } finally {
      setDraftBusy(false);
    }
  };

  const openScheduleEditor = (artifact: ProjectArtifactSummary) => {
    if (locked || draftBusy) return;
    setDraftKind("schedule");
    setDraftArtifact(null);
    setEditingScheduleArtifactId(artifact.artifact_id);
    setScheduleTitle(artifact.plan?.title || "");
    setScheduleStartAt(scheduleInputValue(artifact.plan?.start_at));
    setScheduleDuration(artifact.plan?.duration_minutes || 60);
    setScheduleParticipant(artifact.plan?.participant?.display_name || artifact.plan?.participant?.email || "");
    setScheduleNotes(artifact.plan?.notes || "");
    setScheduleSoundEnabled(artifact.plan?.reminder_sound_enabled !== false);
    setScheduleExportPath("");
    setDraftMessage("修改后需要重新确认日程，确认前不会写入外部日历。");
    setDraftOpen(true);
  };

  const deleteLocalContact = async (contact: LocalContact) => {
    if (draftBusy || !window.confirm(`删除本地联系人“${contact.display_name}”？`)) return;
    setDraftBusy(true);
    try {
      const response = await fetch(`${apiBaseUrl}/contacts/${encodeURIComponent(contact.contact_id)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      const payload = await response.json() as { error?: string; warning?: string };
      if (!response.ok) throw new Error(payload.error || "联系人删除失败");
      setContacts((current) => current.filter((item) => item.contact_id !== contact.contact_id));
      setDraftMessage(`联系人“${contact.display_name}”已删除。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
    } catch (error) {
      setDraftMessage(error instanceof Error ? error.message : "联系人删除失败");
    } finally {
      setDraftBusy(false);
    }
  };

  const createCommunicationDraft = async () => {
    if (locked || (draftKind === "email" && !activeProjectId)) {
      setDraftMessage(draftKind === "email" ? "请先选择一个已解锁的项目。" : "请先解锁工作区。");
      return;
    }
    setDraftBusy(true);
    const isEmail = draftKind === "email";
    const isScheduleEdit = !isEmail && Boolean(editingScheduleArtifactId);
    setDraftMessage(isScheduleEdit ? "正在保存日程修改..." : "正在创建本地草稿...");
    try {
      const scheduleFingerprint = isEmail
        ? ""
        : [scheduleTitle.trim(), scheduleStartAt, String(scheduleDuration), scheduleParticipant.trim(), scheduleNotes.trim(), String(scheduleSoundEnabled)].join("|\u001f|");
      if (!isEmail && !isScheduleEdit && scheduleDraftFingerprintRef.current !== scheduleFingerprint) {
        scheduleDraftFingerprintRef.current = scheduleFingerprint;
        scheduleDraftIdempotencyKeyRef.current = crypto.randomUUID();
      }
      const emailFingerprint = isEmail
        ? [activeProjectId || "", emailRecipient.trim(), emailSubject.trim(), emailBody.trim(), ...emailAttachmentRefs].join("|\u001f|")
        : "";
      if (isEmail && emailDraftFingerprintRef.current !== emailFingerprint) {
        emailDraftFingerprintRef.current = emailFingerprint;
        emailDraftIdempotencyKeyRef.current = crypto.randomUUID();
      }
      const schedulePayload = {
        title: scheduleTitle.trim(),
        start_at: scheduleStartAt,
        duration_minutes: scheduleDuration,
        participant_query: scheduleParticipant.trim() || null,
        notes: scheduleNotes.trim(),
        reminder_sound_enabled: scheduleSoundEnabled,
        ...(!isScheduleEdit ? { idempotency_key: scheduleDraftIdempotencyKeyRef.current || crypto.randomUUID() } : {}),
      };
      const response = await fetch(`${apiBaseUrl}${isEmail ? "/drafts/email" : isScheduleEdit ? `/drafts/${encodeURIComponent(editingScheduleArtifactId!)}` : "/drafts/schedule"}`, {
        method: isScheduleEdit ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(isEmail ? {
          project_id: activeProjectId,
          recipient_query: emailRecipient.trim(),
          subject: emailSubject.trim(),
          body: emailBody.trim(),
          attachment_refs: emailAttachmentRefs,
          idempotency_key: emailDraftIdempotencyKeyRef.current,
        } : schedulePayload),
      });
      const payload = await response.json() as { artifact?: DraftArtifact; replayed?: boolean; error?: string; warning?: string; detail?: string };
      if (!response.ok || !payload.artifact) {
        const action = isScheduleEdit ? "日程修改" : "草稿创建";
        const detail = payload.error || payload.detail;
        throw new Error(detail ? `${action}失败：${detail}` : `${action}失败（HTTP ${response.status}）`);
      }
      setDraftArtifact(payload.artifact);
      if (draftKind === "email") {
        setEmailAttachmentRefs([]);
        emailDraftFingerprintRef.current = "";
        emailDraftIdempotencyKeyRef.current = "";
      }
      else if (isScheduleEdit) {
        setProjectScheduleArtifacts((current) => current.map((artifact) => artifact.artifact_id === payload.artifact!.artifact_id ? { ...artifact, ...payload.artifact } : artifact));
        setScheduleHistory((current) => current.map((artifact) => artifact.artifact_id === payload.artifact!.artifact_id ? { ...artifact, ...payload.artifact } : artifact));
        setSelectedScheduleArtifactId(payload.artifact.artifact_id);
        setDraftMessage("日程已修改，确认后才会冻结新的日程时间。");
      } else {
        // ponytail: update the local rail immediately; the 30-second poll is only reconciliation.
        setProjectScheduleArtifacts((current) => [
          payload.artifact!,
          ...current.filter((artifact) => artifact.artifact_id !== payload.artifact!.artifact_id),
        ]);
        setScheduleHistory((current) => [
          payload.artifact!,
          ...current.filter((artifact) => artifact.artifact_id !== payload.artifact!.artifact_id),
        ]);
        setSelectedScheduleArtifactId(payload.artifact.artifact_id);
        setScheduleTitle("");
        setScheduleStartAt("");
        setScheduleDuration(60);
        setScheduleParticipant("");
        setScheduleNotes("");
        scheduleDraftFingerprintRef.current = "";
        scheduleDraftIdempotencyKeyRef.current = "";
      }
      const auditNote = payload.warning ? ` 注意：${payload.warning}` : "";
      setDraftMessage(isScheduleEdit
        ? `日程已修改，确认后才会冻结新的日程时间。${auditNote}`
        : `${payload.replayed
          ? "请求已回放已有本地草稿，确认前不会发生外部副作用。"
          : "草稿已保存在本地，确认前不会发生外部副作用。"}${auditNote}`);
    } catch (error) {
      setDraftMessage(error instanceof Error ? error.message : isScheduleEdit ? "日程修改失败" : "草稿创建失败");
    } finally {
      setDraftBusy(false);
    }
  };

  const uploadEmailAttachments = async (files: FileList | File[]) => {
    if (locked || !activeProjectId) {
      setDraftMessage("请先选择一个已解锁的项目。");
      return;
    }
    const list = Array.from(files).filter((file) => file.size > 0);
    if (!list.length) return;
    setEmailAttachmentBusy(true);
    setDraftMessage("正在上传邮件附件...");
    try {
      const form = new FormData();
      form.append("project_id", activeProjectId);
      for (const file of list) form.append("attachments", file, file.name);
      const response = await fetch(`${apiBaseUrl}/drafts/email/attachments`, {
        method: "POST",
        headers: authHeaders(),
        body: form,
      });
      const payload = await response.json() as { attachment_refs?: string[]; error?: string };
      const attachmentRefs = payload.attachment_refs || [];
      if (!response.ok || !attachmentRefs.length) throw new Error(payload.error || "附件上传失败");
      setEmailAttachmentRefs((current) => [...current, ...attachmentRefs].filter((name, index, items) => items.indexOf(name) === index));
      setDraftMessage(`已上传 ${attachmentRefs.length} 个附件，可直接创建邮件草稿。`);
    } catch (error) {
      setDraftMessage(error instanceof Error ? error.message : "附件上传失败");
    } finally {
      setEmailAttachmentBusy(false);
    }
  };

  const removeEmailAttachment = (name: string) => {
    setEmailAttachmentRefs((current) => current.filter((item) => item !== name));
  };

  const setDraftConfirmation = async (status: "confirmed" | "cancelled") => {
    if (!draftArtifact || draftArtifact.confirmation_status !== "draft") return;
    setDraftBusy(true);
    try {
      const projectQuery = draftArtifact.project_id
        ? `?project_id=${encodeURIComponent(draftArtifact.project_id)}`
        : "";
      const response = await fetch(`${apiBaseUrl}/drafts/${encodeURIComponent(draftArtifact.artifact_id)}/status${projectQuery}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ status }),
      });
      const payload = await response.json() as { artifact?: DraftArtifact; error?: string; warning?: string };
      if (!response.ok || !payload.artifact) throw new Error(payload.error || "草稿状态更新失败");
      setDraftArtifact(payload.artifact);
      if (payload.artifact.artifact_type === "schedule_draft") {
        setProjectScheduleArtifacts((current) => current.map((artifact) => artifact.artifact_id === payload.artifact!.artifact_id ? { ...artifact, ...payload.artifact } : artifact));
      }
      const auditNote = payload.warning ? ` 注意：${payload.warning}` : "";
      setDraftMessage(status === "confirmed" ? `草稿已确认。配置 SMTP 后可手动发送一次。${auditNote}` : `草稿已取消，未执行任何外部操作。${auditNote}`);
    } catch (error) {
      setDraftMessage(error instanceof Error ? error.message : "草稿状态更新失败");
    } finally {
      setDraftBusy(false);
    }
  };

  const configureSmtp = async () => {
    if (!smtpSender.trim() || !smtpAuthorizationCode.trim()) {
      setDraftMessage("请填写发件邮箱和 SMTP 授权码。");
      return;
    }
    setDraftBusy(true);
    setSmtpConfigured("checking");
    setSmtpStatusMessage(`正在连接${smtpProvider === "qq" ? "QQ" : "网易 163"} SMTP 核验授权码，请稍候...`);
    setDraftMessage("正在核验 SMTP 授权码，核验成功后才会保存。");
    try {
      const response = await fetch(`${apiBaseUrl}/email/smtp-config`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ provider: smtpProvider, sender: smtpSender.trim(), authorization_code: smtpAuthorizationCode.trim() }),
      });
      const payload = await response.json() as { configured?: boolean; error?: string; detail?: string; warning?: string };
      if (!response.ok || payload.configured !== true) {
        throw new Error(payload.error || payload.detail || "SMTP 配置保存后核验失败");
      }
      const statusResponse = await fetch(`${apiBaseUrl}/email/smtp-status?sender=${encodeURIComponent(smtpSender.trim())}`, {
        cache: "no-store",
        headers: authHeaders(),
      });
      const statusPayload = await statusResponse.json() as { configured?: boolean; reason?: string };
      if (!statusResponse.ok || statusPayload.configured !== true) {
        throw new Error("SMTP 配置写入成功，但重新读取凭据管理器时未通过核验，请重新点击保存。");
      }
      setSmtpAuthorizationCode("");
      setSmtpConfigured("configured");
      setSmtpAccounts((current) => [
        ...current.filter((account) => account.sender !== smtpSender.trim()),
        { provider: smtpProvider, sender: smtpSender.trim() },
      ]);
      setSmtpConfigOpen(false);
      setSmtpStatusMessage("当前发件邮箱已配置，可在确认草稿后手动发送。");
      setDraftMessage(`SMTP 已保存到 Windows 凭据管理器，授权码不会写入项目文件。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "SMTP 配置失败";
      setSmtpConfigured("unconfigured");
      setSmtpStatusMessage(`SMTP 配置失败：${message}`);
      setDraftMessage(message);
    } finally {
      setDraftBusy(false);
    }
  };

  const selectSmtpAccount = (sender: string) => {
    if (sender === "__new__") {
      setSmtpPreference({ provider: smtpProvider, sender: "" });
      setSmtpConfigOpen(true);
      setSmtpAuthorizationCode("");
      setSmtpConfigured("unconfigured");
      setSmtpStatusMessage("请输入新的发件邮箱和 SMTP 授权码。");
      return;
    }
    const account = smtpAccounts.find((item) => item.sender === sender);
    if (account) {
      setSmtpPreference(account);
      setSmtpConfigOpen(false);
      setSmtpAuthorizationCode("");
    }
  };

  const deleteSmtpAccount = (account: SmtpPreference) => {
    if (!window.confirm(`删除发件方“${account.sender}”？`)) return;
    setSmtpAccounts((current) => current.filter((item) => item.sender !== account.sender));
    if (smtpSender === account.sender) {
      const next = smtpAccounts.find((item) => item.sender !== account.sender);
      setSmtpPreference(next || { provider: account.provider, sender: "" });
    }
    if (smtpDefaultSender === account.sender) setSmtpDefaultSender("");
    setDraftMessage(`发件方“${account.sender}”已从本机列表删除。`);
  };

  const sendConfirmedEmail = async () => {
    if (!draftArtifact || draftArtifact.confirmation_status !== "confirmed" || !smtpSender.trim()) return;
    setDraftBusy(true);
    try {
      if (emailDeliveryArtifactRef.current !== draftArtifact.artifact_id) {
        emailDeliveryArtifactRef.current = draftArtifact.artifact_id;
        emailDeliveryIdempotencyKeyRef.current = crypto.randomUUID();
      }
      const projectQuery = draftArtifact.project_id
        ? `?project_id=${encodeURIComponent(draftArtifact.project_id)}`
        : "";
      const response = await fetch(`${apiBaseUrl}/drafts/${encodeURIComponent(draftArtifact.artifact_id)}/send${projectQuery}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          sender: smtpSender.trim(),
          idempotency_key: emailDeliveryIdempotencyKeyRef.current,
        }),
      });
      const payload = await response.json() as {
        status?: string;
        idempotent?: boolean;
        delivery_status?: string;
        warning?: string;
        error?: string;
      };
      if (!response.ok || payload.status !== "sent") throw new Error(payload.error || "邮件发送失败");
      setDraftMessage(
        payload.idempotent
          ? `邮件发送请求已回放，未重复调用 SMTP；已有发送记录仍为成功。${payload.warning ? ` 注意：${payload.warning}` : ""}`
          : `邮件已通过已配置的 SMTP 服务发送，发送记录已写入本地审计日志。${payload.warning ? ` 注意：${payload.warning}` : ""}`,
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "邮件发送失败";
      setDraftMessage(message === "SMTP is not configured for this sender" ? "此发件邮箱尚未配置 SMTP，请先点击“保存并核验 SMTP 配置”。" : message);
    } finally {
      setDraftBusy(false);
    }
  };

  const exportConfirmedSchedule = async () => {
    if (!draftArtifact || draftArtifact.confirmation_status !== "confirmed") return;
    setDraftBusy(true);
    try {
      const response = await fetch(`${apiBaseUrl}/drafts/${encodeURIComponent(draftArtifact.artifact_id)}/calendar-ics`, {
        method: "POST", headers: authHeaders(),
      });
      const payload = await response.json() as { output_path?: string; replayed?: boolean; warning?: string; error?: string };
      if (!response.ok || !payload.output_path) throw new Error(payload.error || "日程导出失败");
      setScheduleExportPath(payload.output_path);
      setDraftMessage(payload.replayed
        ? `日程导出请求已回放，未重复写入本地 .ics 文件：${payload.output_path}${payload.warning ? `；${payload.warning}` : ""}`
        : `日程已导出为本地 .ics 文件：${payload.output_path}${payload.warning ? `；${payload.warning}` : ""}`);
    } catch (error) {
      setDraftMessage(error instanceof Error ? error.message : "日程导出失败");
    } finally {
      setDraftBusy(false);
    }
  };

  const openExportedScheduleFile = async () => {
    if (!scheduleExportPath) return;
    try {
      await invoke("open_path", { path: scheduleExportPath });
    } catch {
      setDraftMessage(`已导出，但无法自动打开：${scheduleExportPath}`);
    }
  };

  const deleteScheduleArtifact = async (artifactId: string) => {
    if (!artifactId) return;
    if (!window.confirm("删除这个日程？删除后它会从我的日程中消失。")) return;
    setDraftBusy(true);
    try {
      const response = await fetch(`${apiBaseUrl}/drafts/${encodeURIComponent(artifactId)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      const payload = await response.json() as { artifact?: DraftArtifact; warning?: string; error?: string };
      if (!response.ok || !payload.artifact) throw new Error(payload.error || "日程删除失败");
      setProjectScheduleArtifacts((current) => current.filter((artifact) => artifact.artifact_id !== artifactId));
      setScheduleHistory((current) => current.map((artifact) => artifact.artifact_id === artifactId ? { ...artifact, ...payload.artifact } : artifact));
      setDraftArtifact((current) => (current?.artifact_id === artifactId ? null : current));
      setSelectedScheduleArtifactId((current) => (current === artifactId ? null : current));
      setScheduleReminderArtifactId((current) => (current === artifactId ? null : current));
      setScheduleReminderMessage("");
      setDraftMessage(`日程已删除。${payload.warning ? ` 注意：${payload.warning}` : ""}`);
    } catch (error) {
      setDraftMessage(error instanceof Error ? error.message : "日程删除失败");
    } finally {
      setDraftBusy(false);
    }
  };

  const dismissScheduleReminder = () => {
    if (scheduleReminderKeyRef.current) scheduleReminderDismissedRef.current.add(scheduleReminderKeyRef.current);
    setScheduleReminderOpen(false);
    if (scheduleReminderPhase === "started") {
      setScheduleReminderArtifactId(null);
      setScheduleReminderMessage("");
      setScheduleReminderPhase(null);
      scheduleReminderKeyRef.current = "";
    }
  };

  const importDocuments = async () => {
    if (locked || documentImportStatus === "selecting" || documentImportStatus === "importing") return;
    setDocumentImportStatus("selecting");
    setDocumentImportMessage("");

    try {
      const sourcePaths = await invoke<string[]>("select_document_files");
      if (!sourcePaths.length) {
        setDocumentImportStatus("idle");
        return;
      }

      setDocumentImportStatus("importing");
      const response = await fetch(`${apiBaseUrl}/documents/import`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiToken ? { "X-SoulDrive-Token": apiToken } : {}),
        },
        body: JSON.stringify({
          source_paths: sourcePaths,
          ...(documentImportScope === "project" && activeProjectId ? { project_id: activeProjectId } : {}),
        }),
      });
      if (!response.ok) throw new Error("document import failed");

      const payload = await response.json() as DocumentImportResponse;
      const imageCount = payload.items.filter((item) => item.media_type?.startsWith("image/")).length;
      const processedCount = payload.items.filter((item) => item.status === "imported" || item.status === "already_present").length;
      const existingCount = payload.items.filter((item) => item.status === "already_present").length;
      const scopeLabel = payload.scope === "project" ? "当前项目专属" : "全局知识库";
      setDocumentImportMessage(
        `处理成功 ${processedCount}/${payload.items.length} 份（新增 ${payload.imported_count}，已存在 ${existingCount}）· 范围：${scopeLabel}${imageCount ? ` · 含 ${imageCount} 张待 OCR 图像` : ""}`,
      );
      setDocumentImportStatus("idle");

      const listResponse = await fetch(`${apiBaseUrl}/documents/list`, {
        headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
      });
      if (listResponse.ok) {
        setDocumentLibrary(await listResponse.json());
      }
    } catch {
      setDocumentImportStatus("error");
      setDocumentImportMessage("导入失败");
    }
  };

  const setupWorkspaceSecurity = async () => {
    if (securityBusy) return;
    if (passphrase.length < 8) {
      setSecurityMessage("口令至少需要 8 个字符");
      return;
    }
    if (passphrase !== confirmPassphrase) {
      setSecurityMessage("两次输入的口令不一致");
      return;
    }
    if (!acknowledgeNoRecovery) {
      setSecurityMessage("请先确认忘记口令不可恢复");
      return;
    }

    setSecurityBusy(true);
    setSecurityMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/security/init`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiToken ? { "X-SoulDrive-Token": apiToken } : {}),
        },
        body: JSON.stringify({ passphrase, acknowledge_no_recovery: acknowledgeNoRecovery }),
      });
      if (!response.ok) {
        setSecurityMessage(await readSecurityActionError("init", response));
        if (response.status === 409) await refreshSecurityAfterChange();
        return;
      }
      setPassphrase("");
      setConfirmPassphrase("");
      setAcknowledgeNoRecovery(false);
      setSecurityMessage("工作区已初始化并解锁");
      await refreshSecurityAfterChange();
    } catch {
      setSecurityMessage(formatSecurityActionError("init", 0));
    } finally {
      setSecurityBusy(false);
    }
  };

  const unlockWorkspaceSecurity = async () => {
    if (securityBusy || !passphrase) return;
    setSecurityBusy(true);
    setSecurityMessage("");
    try {
      const response = await fetch(`${apiBaseUrl}/security/unlock`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiToken ? { "X-SoulDrive-Token": apiToken } : {}),
        },
        body: JSON.stringify({ passphrase }),
      });
      if (!response.ok) {
        setSecurityMessage(await readSecurityActionError("unlock", response));
        if (response.status === 409) await refreshSecurityAfterChange();
        return;
      }
      setPassphrase("");
      setSecurityMessage("工作区已解锁");
      await refreshSecurityAfterChange();
    } catch {
      setSecurityMessage(formatSecurityActionError("unlock", 0));
    } finally {
      setSecurityBusy(false);
    }
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (mediaFiles.audio || mediaFiles.image) {
      void sendCapturedMedia(speechConfirmationPending);
      return;
    }
    void (async () => {
      const query = input.trim();
      if (!query || !(await ensureWorkflowReady(query))) return;
      await submitQuery(query);
    })();
  };

  useEffect(() => {
    const textarea = askTextAreaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }, [input]);

  const diagnosticSummary = summarizeDiagnostics(productDiagnostics, healthStatus);
  const installedSkills = useMemo(() => uniqueSkillsById(learningSkills), [learningSkills]);
  const conversationSkills = installedSkills.filter((skill) => skill.target === "conversation" && skill.compatibility_status === "verified");
  const managedSkillGroups = [
    { target: "conversation", title: "对话 Skills", skills: conversationSkills },
    { target: "presentation", title: "PPT Skills", skills: installedSkills.filter(isPresentationSkill) },
  ];
  const chatModelReport = productDiagnostics?.models?.runtime?.chat_model;
  const selectedChatModel = cloudModelConfig.enabled && cloudModelConfig.model
    ? cloudModelConfig.model
    : chatModelReport?.ready
    ? (chatModelReport.selected || "已加载")
    : chatModelReport?.selected
      ? `缺失：${chatModelReport.selected}`
      : "未检测";
  const selectedReranker =
    productDiagnostics?.models?.runtime?.reranker?.selected ||
    (productDiagnostics?.models?.runtime?.reranker?.mode === "disabled" ? "未启用" : "未检测");
  const workspaceSecurityMode = securityPanelMode(securityStatus);
  const modelAccelerationLabel = cloudModelConfig.enabled
    ? "云端 API"
    : chatModelReport?.provider === "ollama"
      ? "Ollama"
      : "CPU Edge";
  const workbenchStyle = {
    "--left-panel-width": leftCollapsed ? "72px" : "300px",
    "--chat-panel-fr": "1fr",
    "--artifact-panel-fr": `${artifactWeight}fr`,
  } as CSSProperties & Record<string, string>;
  const displayedImageRegion = imageSelectionDraft || selectedImageRegion;
  const multimodalEvidenceTags = (() => {
    if (!multimodalRuntime) return [] as string[];
    const tags = new Set<string>();
    const events = multimodalRuntime.perception_events || [];
    if (events.some((event) => event.modality === "audio")) tags.add("录音");
    if (events.some((event) => event.modality === "image" || event.modality === "screen")) tags.add("截图");
    if (
      multimodalRuntime.visual_context?.input_scope === "selected_region" ||
      events.some((event) => event.modality === "image_region" || event.modality === "ocr_region" || event.modality === "ui_selection")
    ) {
      tags.add("框选区");
    }
    if (events.some((event) => event.modality === "ui" || event.modality === "ui_automation")) tags.add("UIA");
    if (events.some((event) => event.modality === "document")) tags.add("文档");
    return Array.from(tags);
  })();
  const multimodalStageTags = multimodalRuntime
    ? Object.entries(multimodalRuntime.stages || {})
        .filter(([, stage]) => stage.status && stage.status !== "skipped")
        .map(([name, stage]) => `${stageLabels[name] || name}:${statusLabels[stage.status] || stage.status}`)
        .slice(0, 3)
    : [];

  return (
    <div className="app-frame">
      <header className="command-bar">
          <div className="brand-zone">
            <div className="brand-mark">
              <BrainCircuit size={24} />
            </div>
            <div>
              <h1>灵枢 SoulDrive</h1>
              <span>面向私有资产的端侧知识引擎</span>
            </div>
          </div>

          <div className={`schedule-rail ${scheduleRailExpanded ? "expanded" : ""}`} aria-label="我的日程">
            <div className="schedule-rail-head">
              <span>我的日程</span>
              <small>{visibleScheduleArtifacts.length ? `${visibleScheduleArtifacts.length} 项` : "暂无日程"}</small>
              {scheduleReminderPhase === "upcoming" && scheduleReminderLiveMessage && <small className={scheduleReminderArtifactId ? "urgent" : ""}>{scheduleReminderLiveMessage}</small>}
              <button
                type="button"
                className="schedule-history-toggle"
                disabled={locked || scheduleHistoryBusy}
                onClick={() => void openScheduleHistory()}
                title="查看近 7 天创建的日程"
              >
                <History size={13} />
                {scheduleHistoryBusy ? "加载中" : "近 7 天历史"}
              </button>
              <button
                type="button"
                className="schedule-rail-toggle"
                aria-label={scheduleRailExpanded ? "收起日程详情" : "展开日程详情"}
                title={scheduleRailExpanded ? "收起日程详情" : "展开日程详情"}
                onClick={() => setScheduleRailExpanded((current) => !current)}
              >
                {scheduleRailExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
            </div>
            <div className="schedule-rail-track">
              {visibleScheduleArtifacts.length ? visibleScheduleArtifacts.map((artifact) => (
                <button
                  key={artifact.artifact_id}
                  type="button"
                  className={`schedule-card ${selectedScheduleArtifactId === artifact.artifact_id ? "selected" : ""} ${scheduleReminderArtifactId === artifact.artifact_id ? "urgent" : ""}`}
                  onClick={() => {
                    setSelectedScheduleArtifactId(artifact.artifact_id);
                    setScheduleRailExpanded(true);
                  }}
                  title={artifact.plan?.notes || artifact.change_summary || "查看日程草稿"}
                >
                  <strong>{artifact.plan?.title || artifact.plan?.subject || "未命名日程"}</strong>
                  <span>{formatScheduleStartTime(artifact.plan?.start_at)}</span>
                </button>
              )) : <span className="schedule-empty">暂无日程</span>}
            </div>
            {scheduleRailExpanded && (
              <div className="schedule-detail-panel" role="region" aria-label="日程详情">
                {selectedScheduleArtifact ? (
                  <>
                    <div className="schedule-detail-head">
                      <div>
                        <span>日程详情</span>
                        <strong>{selectedScheduleArtifact.plan?.title || selectedScheduleArtifact.plan?.subject || "未命名日程"}</strong>
                      </div>
                      <small>{selectedScheduleArtifact.confirmation_status === "confirmed" ? "已确认" : selectedScheduleArtifact.confirmation_status === "cancelled" ? "已取消" : "草稿"}</small>
                    </div>
                    <div className="schedule-detail-grid">
                      <div><span>开始时间（创建时设置）</span><strong>{formatScheduleStartTime(selectedScheduleArtifact.plan?.start_at)}</strong></div>
                      <div><span>时长</span><strong>{selectedScheduleArtifact.plan?.duration_minutes ? `${selectedScheduleArtifact.plan.duration_minutes} 分钟` : "未设置"}</strong></div>
                      <div><span>参与人</span><strong>{selectedScheduleArtifact.plan?.participant?.display_name ? `${selectedScheduleArtifact.plan.participant.display_name} <${selectedScheduleArtifact.plan.participant.email}>` : "未填写"}</strong></div>
                      <div><span>说明</span><strong>{selectedScheduleArtifact.plan?.notes || selectedScheduleArtifact.change_summary || "暂无说明"}</strong></div>
                    </div>
                    <div className="schedule-detail-actions">
                      <button type="button" className="schedule-edit-action" disabled={draftBusy || locked} onClick={() => openScheduleEditor(selectedScheduleArtifact)}>
                        <Pencil size={14} />
                        修改日程
                      </button>
                      <button type="button" className="danger" disabled={draftBusy} onClick={() => void deleteScheduleArtifact(selectedScheduleArtifact.artifact_id)}>
                        删除日程
                      </button>
                    </div>
                  </>
                ) : (
                  <span className="schedule-empty">点击任一日程查看详情</span>
                )}
              </div>
            )}
          </div>

          <div className="run-status">
          <button
            type="button"
            className="layout-icon-button"
            title={leftCollapsed ? "展开信息栏" : "折叠信息栏"}
            aria-label={leftCollapsed ? "展开信息栏" : "折叠信息栏"}
            onClick={toggleLeftPanel}
          >
            {leftCollapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
          </button>
          <span className={`status-token ${locked ? "" : "online"}`}>
            <ShieldCheck size={14} />
            {runtimeStatus ? runtimeStatus.auth_level : "BOOTING"}
          </span>
          <span className={`status-token ${locked ? "" : "online"}`}>
            <Activity size={14} />
            {healthStatus.sidecar === "OFFLINE" ? "OFFLINE" : locked ? "LOCKED" : "READY"}
          </span>
        </div>
      </header>

      <section
        ref={workbenchRef}
        className={`workbench ${leftCollapsed ? "left-collapsed" : ""} ${artifactCollapsed ? "artifact-collapsed" : ""} ${isWorkbenchDragging ? "resizing" : ""}`}
        style={workbenchStyle}
      >
        {leftCollapsed ? (
          <aside className="system-panel collapsed-rail" aria-label="工作区信息栏">
            <button type="button" className="rail-button primary" title="展开信息栏" onClick={toggleLeftPanel}>
              <ChevronRight size={17} />
            </button>
            <div className={`rail-status ${locked ? "" : "online"}`} title={locked ? "工作区锁定" : "工作区已就绪"}>
              <ShieldCheck size={16} />
              <span>{locked ? "LOCK" : "OK"}</span>
            </div>
            <button
              type="button"
              className="rail-button"
              title={indexBusy ? "知识索引与 GraphRAG 构建中" : "重建知识索引与 GraphRAG"}
              aria-label={indexBusy ? "知识索引与 GraphRAG 构建中" : "重建知识索引与 GraphRAG"}
              disabled={locked || indexBusy || indexStartStatus === "starting"}
              onClick={() => void startIndexing()}
            >
              <RefreshCw size={16} />
            </button>
            <button
              type="button"
              className="rail-button"
              title="导入项目资料（报告、指标、配置等）"
              disabled={locked || documentImportStatus === "selecting" || documentImportStatus === "importing"}
              onClick={() => void importDocuments()}
            >
              <Upload size={16} />
            </button>
            <button type="button" className="rail-button" title={leftCollapsed && Math.abs(artifactWeight - GRAPH_PRIORITY_ARTIFACT_WEIGHT) < 0.01 ? "恢复均衡布局" : "导图优先"} onClick={useGraphPriorityLayout}>
              <GitBranch size={16} />
            </button>
          </aside>
        ) : (
        <aside className="system-panel">
          <div className="panel-block">
            <div className="panel-title">
              <span>
                <ShieldCheck size={15} />
                工作区解锁
              </span>
            </div>
            {workspaceSecurityMode === "unavailable" ? (
              <div className="diagnostic-summary warn">
                <div>
                  <strong>{healthStatus.sidecar === "OFFLINE" ? "ENGINE OFFLINE" : "CONNECTING"}</strong>
                  <span>
                    {healthStatus.sidecar === "OFFLINE"
                      ? "本地推理服务连接中断；当前不会自动把它误判为工作区锁定。"
                      : `正在读取本机运行状态${securityStatusError ? `（${securityStatusError}）` : ""}。`}
                  </span>
                </div>
              </div>
            ) : workspaceSecurityMode === "initialize" ? (
              <div className="security-form">
                <p>该口令用于保护本地工作区主密钥。SoulDrive 不会保存口令，也不提供找回能力。忘记口令后，当前 PC 或便携工作区中的加密知识库将无法解锁。</p>
                <input
                  type="password"
                  value={passphrase}
                  onChange={(event) => setPassphrase(event.target.value)}
                  placeholder="设置工作区口令"
                  disabled={securityBusy}
                />
                <input
                  type="password"
                  value={confirmPassphrase}
                  onChange={(event) => setConfirmPassphrase(event.target.value)}
                  placeholder="再次输入口令"
                  disabled={securityBusy}
                />
                <label className="security-check">
                  <input
                    type="checkbox"
                    checked={acknowledgeNoRecovery}
                    onChange={(event) => setAcknowledgeNoRecovery(event.target.checked)}
                    disabled={securityBusy}
                  />
                  <span>我已知晓忘记口令不可恢复</span>
                </label>
                <button type="button" disabled={securityBusy} onClick={() => void setupWorkspaceSecurity()}>
                  初始化并解锁
                </button>
              </div>
            ) : workspaceSecurityMode === "unlocked" ? (
              <div className="diagnostic-summary ok">
                <div>
                  <strong>UNLOCKED</strong>
                  <span>{securityStatus?.development_bypass ? "本机开发模式已自动解锁" : "工作区已通过口令解锁"}</span>
                </div>
              </div>
            ) : (
              <div className="security-form">
                <input
                  type="password"
                  value={passphrase}
                  onChange={(event) => setPassphrase(event.target.value)}
                  placeholder="输入工作区口令"
                  disabled={securityBusy}
                />
                <button type="button" disabled={securityBusy || !passphrase} onClick={() => void unlockWorkspaceSecurity()}>
                  解锁工作区
                </button>
              </div>
            )}
            {securityMessage && <p className="security-message">{securityMessage}</p>}
          </div>

          <div className="panel-block project-space-block">
            <div className="panel-title">
              <span>
                <Layers3 size={15} />
                项目空间
              </span>
              <span className="project-space-actions">
                <button
                  type="button"
                  title="创建本地项目"
                  disabled={locked}
                  onClick={() => { setProjectCreateMessage(""); setProjectCreateMode("research"); setProjectCreateRoot(""); setProjectCreateOpen(true); }}
                >
                  <span aria-hidden="true">+</span>
                </button>
              </span>
            </div>
            <div className="project-list">
              {projects.length ? projects.map((project) => (
                <div
                  key={project.project_id}
                  className={`project-row ${activeProjectId === project.project_id ? "active" : ""}`}
                >
                  <button
                    type="button"
                    className="project-select-button"
                    aria-pressed={activeProjectId === project.project_id}
                    onClick={() => setActiveProjectId(project.project_id)}
                  >
                    <strong>{project.name}</strong>
                    <span>{project.project_mode === "research" ? "研究" : "业务"} · {project.asset_count} 资产 · {project.experiment_count} 实验</span>
                  </button>
                  <button
                    type="button"
                    className="project-delete-button"
                    title={`删除项目：${project.name}`}
                    aria-label={`删除项目：${project.name}`}
                    disabled={locked || isGenerating || indexBusy || sessionLoading || projectDeleteBusy}
                    onClick={() => void deleteProject(project)}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              )) : (
                <div className="project-empty">
                  {projectListUnavailable ? "项目服务正在恢复，暂时无法读取本地项目。" : "创建项目后，在此查看实验、决策和交付物。"}
                </div>
              )}
            </div>
            {projectMessage && <div className="project-message" role="status">{projectMessage}</div>}
            {projectListUnavailable && projects.length ? <div className="project-empty">项目服务正在恢复，当前显示的是最近一次成功读取的列表。</div> : null}
            {activeProjectId && (
              <div className="session-list" aria-label="当前项目会话">
                <div className="session-list-header">
                  <span>项目对话</span>
                  <button type="button" title="在当前项目新建对话" aria-label="在当前项目新建对话" disabled={locked || isGenerating || sessionLoading} onClick={openProjectSessionDialog}>+</button>
                </div>
                {sessionCreateOpen && (
                  <form className="session-create-inline" onSubmit={(event) => { event.preventDefault(); void createProjectSession(); }}>
                    <strong>新建项目会话</strong>
                    <input
                      autoFocus
                      value={sessionCreateName}
                      onChange={(event) => setSessionCreateName(event.target.value)}
                      placeholder="例如：RAG 论文综述"
                      maxLength={120}
                      required
                      disabled={sessionLoading}
                    />
                    <label className="session-workflow-select">
                      <span>会话业务</span>
                      <select
                        value={sessionCreateWorkflowId}
                        onChange={(event) => setSessionCreateWorkflowId(event.target.value as MultimodalWorkflowId | "")}
                      disabled={sessionLoading}
                    >
                        <option value="">通用文档</option>
                        <option value="paper_review">论文精读</option>
                        <option value="experiment_compare">实验对比</option>
                        <option value="meeting_actions">会议纪要</option>
                      </select>
                    </label>
                    <div className="session-create-actions">
                      <button type="button" onClick={() => { setSessionCreateOpen(false); setSessionMessage(""); }} disabled={sessionLoading}>取消</button>
                      <button type="submit" disabled={sessionLoading || !sessionCreateName.trim()}>创建</button>
                    </div>
                  </form>
                )}
                {sessionMessage && <div className="session-message" role="status">{sessionMessage}</div>}
                {visibleProjectSessions.map((session) => (
                  <div
                    key={session.session_id}
                    className={`session-row ${activeSessionId === session.session_id ? "active" : ""}`}
                  >
                    <button
                      type="button"
                      className="session-select-button"
                      disabled={isGenerating || sessionLoading}
                      onClick={() => void selectProjectSession(session)}
                    >
                      <strong>{session.title || "未命名对话"}</strong>
                      <span>{session.workflow_id ? (workflowSpecs.find((workflow) => workflow.workflow_id === session.workflow_id)?.title || session.workflow_id) : "通用文档"}</span>
                    </button>
                    <button
                      type="button"
                      className="session-delete-button"
                      title={`删除会话：${session.title || "未命名对话"}`}
                      aria-label={`删除会话：${session.title || "未命名对话"}`}
                      disabled={isGenerating || sessionLoading}
                      onClick={() => void deleteProjectSession(session)}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
                {projectSessions.length > 3 && (
                  <button
                    type="button"
                    className="session-expand-button"
                    onClick={() => setShowAllProjectSessions((current) => !current)}
                  >
                    {showAllProjectSessions ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                    {showAllProjectSessions ? "收起会话" : `展开其余 ${projectSessions.length - 3} 条`}
                  </button>
                )}
              </div>
            )}
            {projectOverview ? (
              <div className="project-overview">
                <span className="project-overview-label">最近实验</span>
                {projectOverview.recent_runs.slice(0, 2).map((run) => (
                  <div key={`${run.experiment_name}-${run.performed_at}`}>
                    <strong>{run.experiment_name}</strong>
                    <span>{formatDocumentTime(Date.parse(run.performed_at) / 1000)} · {run.dataset_version || "未标注数据集"}</span>
                  </div>
                ))}
                {projectOverview.tasks.length ? <span className="project-task-count">待办 {projectOverview.tasks.length} 项</span> : null}
              </div>
            ) : null}

            {projectCreateOpen && (
              <div className="project-create-inline" role="dialog" aria-label="创建本地项目">
                <strong>创建本地项目</strong>
                <input
                  autoFocus
                  value={projectCreateName}
                  onChange={(event) => setProjectCreateName(event.target.value)}
                  onKeyDown={(event) => { if (event.key === "Enter") void createProject(); }}
                  placeholder="例如：SoulAlign 实验"
                  maxLength={160}
                  disabled={projectCreateBusy}
                />
                <label className="project-mode-select">
                  <span>项目类型</span>
                  <select value={projectCreateMode} onChange={(event) => setProjectCreateMode(event.target.value as ProjectMode)} disabled={projectCreateBusy}>
                    <option value="research">研究与论文实验</option>
                    <option value="business">业务材料与会议</option>
                  </select>
                </label>
                <label className="project-mode-select">
                  <span>项目文件夹</span>
                  <div className="project-folder-picker">
                    <input value={projectCreateRoot} onChange={(event) => setProjectCreateRoot(event.target.value)} placeholder="选择已有文件夹，留空则自动创建" maxLength={520} disabled={projectCreateBusy} />
                    <button type="button" title="选择项目文件夹" aria-label="选择项目文件夹" onClick={() => void selectProjectFolder()} disabled={projectCreateBusy}><FolderOpen size={14} /></button>
                  </div>
                </label>
                <div className="project-create-actions">
                  <button type="button" onClick={() => setProjectCreateOpen(false)} disabled={projectCreateBusy}>取消</button>
                  <button type="button" onClick={() => void createProject()} disabled={projectCreateBusy || !projectCreateName.trim()}>创建</button>
                </div>
                {projectCreateMessage && <span className="project-create-message">{projectCreateMessage}</span>}
              </div>
            )}
          </div>

          {activeProjectId && (
            <div className="panel-block workflow-panel workflow-panel-separate" aria-label="业务模块">
              <div className="panel-title">
                <span>
                  <ClipboardList size={15} />
                  业务模块
                </span>
              </div>
              <div className="workflow-tools-grid">
                <button type="button" onClick={() => openDraftBuilder("email")} disabled={locked}>
                  <Mail size={14} /> 邮件与日程
                </button>
                <button type="button" onClick={openPresentationBuilder} disabled={locked || presentationDocuments.length === 0}>
                  <Presentation size={14} /> PPT 生成
                </button>
                <button type="button" onClick={() => setSkillOpen(true)} disabled={locked}>
                  <ClipboardList size={14} /> Skills 管理
                </button>
              </div>
              {selectedWorkflowId === "experiment_compare" && (
                <>
                <ExperimentMetricImportPanel
                  key={activeProjectId}
                  projectId={activeProjectId}
                  documents={documents}
                  apiBaseUrl={apiBaseUrl}
                  apiToken={apiToken}
                  locked={locked || !runtimeConfigLoaded}
                  onImported={() => setExperimentRunsRefresh((value) => value + 1)}
                  onOpenDocuments={() => { setDocumentImportScope("project"); setDocumentLibraryOpen(true); }}
                />
                <div className="experiment-run-picker" aria-label="实验对比运行选择">
                  <strong>明确选择两次实验运行</strong>
                  <label>
                    基线运行
                    <select aria-label="基线实验运行" value={baselineExperimentRunId} onChange={(event) => setBaselineExperimentRunId(event.target.value)} disabled={locked}>
                      <option value="">请选择基线，不自动取第一条</option>
                      {experimentRuns.map((run) => (
                        <option key={run.run_id} value={run.run_id} disabled={run.run_id === currentExperimentRunId}>
                          {run.experiment_name} · {formatDocumentTime(Date.parse(run.performed_at) / 1000)} · {run.config_ref || "未标注配置"} · {run.run_id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    当前运行
                    <select aria-label="当前实验运行" value={currentExperimentRunId} onChange={(event) => setCurrentExperimentRunId(event.target.value)} disabled={locked}>
                      <option value="">请选择不同的当前运行</option>
                      {experimentRuns.map((run) => (
                        <option key={run.run_id} value={run.run_id} disabled={run.run_id === baselineExperimentRunId}>
                          {run.experiment_name} · {formatDocumentTime(Date.parse(run.performed_at) / 1000)} · {run.config_ref || "未标注配置"} · {run.run_id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <small>{experimentRuns.length ? "只使用已确认入账的运行；单位、数据版本等不兼容时会说明原因。配置差异不代表已证实因果。" : "当前项目尚无已确认运行，需先预览并确认导入指标。"}</small>
                  <button type="button" onClick={() => setExperimentRunsRefresh((value) => value + 1)} disabled={locked}>刷新运行列表</button>
                </div>
                </>
              )}
            </div>
          )}

          <div className="panel-block paper-library-block">
            <div className="panel-title">
              <span>
                <FileText size={15} />
                知识文档
              </span>
              <button
                type="button"
                title={indexBusy ? "知识索引与 GraphRAG 构建中" : "重建知识索引与 GraphRAG"}
                aria-label={indexBusy ? "知识索引与 GraphRAG 构建中" : "重建知识索引与 GraphRAG"}
                disabled={locked || indexBusy || indexStartStatus === "starting"}
                onClick={() => void startIndexing()}
              >
                <RefreshCw size={15} />
              </button>
              <button
                type="button"
                title="导入项目资料（报告、指标、配置等）"
                disabled={locked || documentImportStatus === "selecting" || documentImportStatus === "importing"}
                onClick={() => void importDocuments()}
              >
                <Upload size={15} />
              </button>
            </div>
            <label className="document-import-scope">
              <span>新导入文档范围</span>
              <select
                aria-label="新导入文档范围"
                value={documentImportScope}
                onChange={(event) => setDocumentImportScope(event.target.value as "global" | "project")}
                disabled={locked || documentImportStatus === "selecting" || documentImportStatus === "importing"}
              >
                <option value="global">全局知识库（所有项目可检索）</option>
                <option value="project" disabled={!activeProjectId}>当前项目专属</option>
              </select>
            </label>
            <div className="library-summary">
              <div>
                <strong>{documentLibrary?.document_count ?? runtimeStatus?.indexing?.discovered_files ?? 0}</strong>
                <span>份文档</span>
              </div>
              <div>
                <strong>{documentLibrary?.indexed_count ?? runtimeStatus?.indexing?.skipped_files ?? 0}</strong>
                <span>已索引</span>
              </div>
            </div>
            <div className={`indexing-strip ${indexBusy ? "active" : indexStartStatus === "error" ? "warn" : ""}`}>
              <span>{indexStartStatus === "error" ? "索引启动失败" : indexStartMessage || indexingStatus.toUpperCase()}</span>
              <strong>
                {runtimeStatus?.indexing?.processed_files ?? 0}/{runtimeStatus?.indexing?.total_files ?? 0}
              </strong>
            </div>
            {documentImportMessage && (
              <div className={`indexing-strip ${documentImportStatus === "error" ? "warn" : "active"}`}>
                <span>{documentImportMessage}</span>
                <strong>{documentImportStatus === "importing" ? "..." : ""}</strong>
              </div>
            )}
            <button type="button" className="document-library-launcher" disabled={locked} onClick={openDocumentLibrary}>
              <FileText size={20} />
              <span>
                <strong>查看文档库</strong>
                <small>在大窗口中管理文档、索引状态和上下文</small>
              </span>
              <ChevronRight size={17} />
            </button>
          </div>

          <details className="panel-block technical-details">
            <summary>
              <Activity size={15} />
              运行详情
            </summary>
            <div className={`diagnostic-summary ${diagnosticSummary.warnings.length ? "warn" : "ok"}`}>
              <div>
                <strong>{diagnosticSummary.warnings.length ? "ATTENTION" : "OPERATIONAL"}</strong>
                <span>
                  {diagnosticSummary.readyCount}/{diagnosticSummary.totalCount} 项检查 · {diagnosticSummary.primaryHint}
                </span>
              </div>
              {diagnosticSummary.warnings.length ? (
                <div className="diagnostic-chips">
                  {diagnosticSummary.warnings.slice(0, 4).map((item) => (
                    <span key={item}>{item}</span>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="metric-list">
              <div>
                <span>Sidecar</span>
                <strong>{healthStatus.sidecar}</strong>
              </div>
              <div>
                <span>索引状态</span>
                <strong>{runtimeStatus?.indexing?.status || "idle"}</strong>
              </div>
              <div>
                <span>推理加速</span>
                <strong>{modelAccelerationLabel}</strong>
              </div>
              <div>
                <span>回答模型</span>
                <strong>{selectedChatModel}</strong>
              </div>
              <div>
                <span>Redis 热状态</span>
                <strong>{storageStatus?.redis?.status || "未检测"}{storageStatus?.redis?.session_cache_count !== undefined ? ` · ${storageStatus.redis.session_cache_count} 会话` : ""}</strong>
              </div>
              <div>
                <span>PostgreSQL 控制面</span>
                <strong>{storageStatus?.postgres?.status || "未检测"}{storageStatus?.postgres?.session_count !== undefined ? ` · ${storageStatus.postgres.session_count} 会话` : ""}</strong>
              </div>
              <div>
                <span>待重放事件</span>
                <strong>{storageStatus?.outbox?.pending ?? 0}</strong>
              </div>
              <div>
                <span>Reranker</span>
                <strong>{selectedReranker}</strong>
              </div>
              <div>
                <span>GraphRAG</span>
                <strong>
                  {graphRagDiagnostics === null
                    ? "诊断不可用（等待本地引擎）"
                    : graphRagDiagnostics.status === "building"
                    ? "构建中（索引完成后可用）"
                    : graphRagDiagnostics.status === "failed"
                      ? "构建失败（请重建索引）"
                      : graphRagDiagnostics.status === "degraded"
                        ? "部分完成（有项目图谱失败）"
                      : graphRagDiagnostics.status === "locked"
                        ? "工作区已锁定（解锁后查看）"
                      : graphRagDiagnostics.status === "disabled"
                        ? "未启用"
                      : graphRagDiagnostics.ready
                          ? `Leiden · ${graphRagDiagnostics.community_count ?? 0} 社区`
                          : graphRagDiagnostics.observation_stale
                            ? "未构建（运行记录已完成，但当前索引文件缺失；请重建）"
                            : "未构建（请在知识文档区重建）"}
                </strong>
              </div>
              <div>
                <span>备份预检</span>
                <strong>
                  {backupPreflight === null
                    ? "诊断不可用"
                    : backupPreflight.ready
                      ? "可进入人工备份"
                      : `阻塞 · ${backupPreflight.blockers[0]?.code || "需处理"}`}
                </strong>
              </div>
              <div>
                <span>请求数</span>
                <strong>{runtimeMetrics?.total_requests ?? 0}</strong>
              </div>
              <div>
                <span>首 Token</span>
                <strong>{runtimeMetrics?.last_chat_first_token_ms ?? 0} ms</strong>
              </div>
              <div>
                <span>完整回答</span>
                <strong>{runtimeMetrics?.last_chat_total_ms ?? 0} ms</strong>
              </div>
              <div>
                <span>流式断线</span>
                <strong>{runtimeMetrics?.chat_abandoned ?? 0}</strong>
              </div>
              <div>
                <span>&lt;5s 达标率</span>
                <strong>{Math.round((runtimeMetrics?.chat_under_5s_rate ?? 0) * 100)}%</strong>
              </div>
              <div>
                <span>审计链</span>
                <strong>{productDiagnostics?.audit?.ready ? "INTACT" : "CHECK"}</strong>
              </div>
            </div>
            {runtimeMetrics?.last_error && <p>{runtimeMetrics.last_error}</p>}
          </details>

          <div className="panel-block cloud-model-block" aria-label="云端模型配置">
            <div className="panel-title">
              <span>
                <Cloud size={15} />
                云端模型
              </span>
              <span className={`cloud-model-status ${cloudModelEnabled ? "enabled" : ""}`}>
                {cloudModelEnabled !== cloudModelConfig.enabled
                  ? "待保存"
                  : cloudModelConfig.enabled
                    ? "已启用"
                    : cloudModelConfig.configured
                      ? "已配置"
                      : "未配置"}
              </span>
            </div>
            <p className="cloud-model-hint">填写 OpenAI-compatible API 地址和 Key；未启用或未配置时继续使用本地模型。</p>
            <div className="cloud-model-form">
              <label>
                <span>API 请求地址</span>
                <input
                  type="url"
                  value={cloudModelBaseUrl}
                  onChange={(event) => setCloudModelBaseUrl(event.target.value)}
                  placeholder="https://api.example.com/v1"
                  autoComplete="url"
                  disabled={locked || cloudModelBusy}
                />
              </label>
              <label>
                <span>API Key</span>
                <input
                  type="password"
                  value={cloudModelApiKey}
                  onChange={(event) => setCloudModelApiKey(event.target.value)}
                  placeholder={cloudModelConfig.api_key_configured ? "已保存，留空则保留当前 Key" : "输入云端 API Key"}
                  autoComplete="new-password"
                  disabled={locked || cloudModelBusy}
                />
              </label>
              <label>
                <span>默认模型</span>
                <select
                  value={selectedCloudModelIsListed ? cloudModelName : (cloudModelName ? "__custom__" : "")}
                  onChange={(event) => setCloudModelName(event.target.value === "__custom__" ? "" : event.target.value)}
                  disabled={locked || cloudModelBusy}
                >
                  <option value="">获取列表后选择</option>
                  {cloudModelConfig.models.map((model) => <option key={model} value={model}>{model}</option>)}
                  <option value="__custom__">手动输入模型名</option>
                </select>
                {(!selectedCloudModelIsListed || !cloudModelName) && (
                  <input
                    value={cloudModelName}
                    onChange={(event) => setCloudModelName(event.target.value)}
                    placeholder="手动输入模型名"
                    autoComplete="off"
                    disabled={locked || cloudModelBusy}
                  />
                )}
              </label>
              <label className="cloud-model-switch">
                <input
                  type="checkbox"
                  checked={cloudModelEnabled}
                  onChange={(event) => setCloudModelEnabled(event.target.checked)}
                  disabled={locked || cloudModelBusy}
                />
                <span>启用云端模型（启用后问答优先走云端）</span>
              </label>
              <div className="cloud-model-actions">
                <button type="button" onClick={() => void fetchCloudModels()} disabled={locked || cloudModelBusy || !cloudModelBaseUrl.trim()}>
                  <RefreshCw size={13} /> 获取模型列表
                </button>
                <button type="button" className="primary" onClick={() => void saveCloudModelConfig()} disabled={locked || cloudModelBusy || !cloudModelBaseUrl.trim()}>
                  <Check size={13} /> 保存配置
                </button>
              </div>
            </div>
            <p className="cloud-model-message" role="status">{cloudModelMessage}</p>
          </div>
        </aside>
        )}

        <main className="dialog-panel">
          <div className="dialog-header">
            <div>
              <span>
                <FileText size={14} />
                {projectOverview?.project.name || "Local Knowledge Engine"}
              </span>
              <h2>{projectOverview ? "项目知识问答与分析" : "私有知识问答与分析"}</h2>
              {reviewState.message && reviewState.mode !== "reviewing" && (
                <p className={`learning-review-message ${reviewState.mode === "error" ? "error" : ""}`}>
                  {reviewState.message}
                </p>
              )}
              {sessionMessage && <p className="session-message" role="status">{sessionMessage}</p>}
              {runtimeMemoryNotice && (
                <div className="runtime-memory-notice" role="status">
                  <span>{runtimeMemoryNotice.text}</span>
                  <button type="button" onClick={() => void revokeRuntimeMemory()}>撤销</button>
                </div>
              )}
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="memory-action"
                disabled={locked || isGenerating || reviewState.mode === "drafting" || reviewState.mode === "submitting"}
                onClick={() => void requestReflectionDraft("manual")}
                title="审核并保存本轮可复用记忆"
              >
                <BrainCircuit size={15} />
                记忆
              </button>
              <button
                type="button"
                className="memory-manage-action"
                disabled={locked || memoryManagerBusy}
                onClick={openMemoryManager}
                title="查看、修改、停用或删除已保存的偏好"
              >
                <Settings2 size={15} />
                画像
              </button>
              <div className="model-badge">
                <KeyRound size={14} />
                {selectedChatModel}
              </div>
            </div>
          </div>

          <div className="message-list">
            {messages.length === 0 && (
              <div className="conversation-empty">
                <FileText size={42} />
                <p>
                  {sessionLoading
                    ? "正在加载会话历史…"
                    : activeSessionId
                      ? "当前会话暂无消息，可直接输入问题。"
                      : "导入本地私有文档，选择当前文档上下文或直接提问。"}
                </p>
              </div>
            )}

            {parsedMessages.map(({ message, parsed }, index) => {
              const isUser = message.role === "user";
              const hasArtifacts = parsed.evidence.length > 0 || parsed.artifacts.length > 0;
              const displayContent = parsed.cleanContent || (
                parsed.artifacts.length
                  ? "已生成结构化证据链。"
                  : parsed.evidence.length
                    ? "已生成结构化证据链。"
                    : message.content
              );

              return (
                <article key={`${message.role}-${index}`} className={`message ${isUser ? "user" : "ai"}`}>
                  <div className="message-meta">{isUser ? "我" : "灵枢"}</div>
                  <div className="message-content">
                    {isUser ? (
                      <p>{message.content}</p>
                    ) : (
                      <>
                        {displayContent && <EvidenceMarkdown>{displayContent}</EvidenceMarkdown>}
                      </>
                    )}
                    {isGenerating && index === messages.length - 1 && <span className="stream-caret" />}
                    {!isUser && hasArtifacts && (
                      <div className="message-actions">
                        <button type="button" onClick={() => openArtifactStudio(parsed.evidence.length ? "evidence" : "graph")}>
                          查看详情
                        </button>
                        {parsed.evidence.length > 0 && <span>{parsed.evidence.length} 条证据</span>}
                        {parsed.artifacts.length > 0 && <span>{parsed.artifacts.length} 个导图</span>}
                      </div>
                    )}
                  </div>
                </article>
              );
            })}
            <div ref={messagesEndRef} />
          </div>

          {activeDocumentNames.length > 0 && (
            <div className="active-document-context" title={activeDocumentNames.join("、")}>
              <span>已选 {activeDocumentNames.length} 份上下文：{activeDocumentNames.join("、")}</span>
              <small>本轮只检索这些文档；选中文档时会暂停 GraphRAG，避免混入其他来源。</small>
              <button type="button" onClick={() => setActiveDocumentRefs([])} title="清除当前文档上下文">
                <X size={13} />
              </button>
            </div>
          )}

          {(multimodalDecision || multimodalRuntime) && (
            <details className="multimodal-details">
              <summary>查看详情</summary>
              {multimodalDecision && (
                <section className={`multimodal-decision ${multimodalDecision.action}`} aria-label="本轮多模态决策">
                  <span>本轮路径</span>
                  <strong>{formatMultimodalIntent(multimodalDecision.intent)}</strong>
                  <span>{multimodalDecision.clarification_required ? "需要确认" : "直接回答"}</span>
                  {multimodalDecision.clarification?.prompt && <small>{multimodalDecision.clarification.prompt}</small>}
                </section>
              )}
              {multimodalRuntime && (
                <section className={`media-runtime-result ${multimodalRuntime.status}`} aria-label="本轮端侧感知结果">
                  <span>端侧感知</span>
                  <strong>{statusLabels[multimodalRuntime.status] || multimodalRuntime.status}</strong>
                  <span>{Math.round(multimodalRuntime.latency_ms?.total || 0)} 毫秒</span>
                  {multimodalRuntime.visual_context?.input_scope && (
                    <span>范围:{multimodalRuntime.visual_context.input_scope === "selected_region" ? "框选区域" : "整张图片"}</span>
                  )}
                  {multimodalEvidenceTags.length > 0 && (
                    <div className="media-evidence-tags" aria-label="本轮证据">
                      <span className="media-evidence-label">证据</span>
                      {multimodalEvidenceTags.map((tag) => (
                        <span key={tag} className="media-evidence-tag">{tag}</span>
                      ))}
                    </div>
                  )}
                  {multimodalStageTags.length > 0 && (
                    <div className="media-evidence-tags media-stage-tags" aria-label="执行流程">
                      <span className="media-evidence-label">流程</span>
                      {multimodalStageTags.map((tag) => (
                        <span key={tag} className="media-evidence-tag">{tag}</span>
                      ))}
                    </div>
                  )}
                </section>
              )}
            </details>
          )}

          {(mediaFiles.audio || mediaFiles.image || uiAutomationSnapshot || mediaMessage) && (
            <div className={`media-capture-summary ${mediaBusy || mediaCaptureState !== "idle" ? "busy" : ""}`}>
              {mediaFiles.image && mediaImageUrl && (
                <div className={`media-visual-preview ${mediaPreviewOpen ? "" : "collapsed"}`}>
                  {mediaPreviewOpen && (
                    <div className="media-visual-canvas">
                      <img
                        src={mediaImageUrl}
                        alt="本轮授权的图片或屏幕截图"
                        draggable={false}
                        onPointerDown={startImageRegionSelection}
                        onPointerMove={moveImageRegionSelection}
                        onPointerUp={finishImageRegionSelection}
                        onPointerCancel={cancelImageRegionSelection}
                        onLostPointerCapture={cancelImageRegionSelection}
                      />
                      {displayedImageRegion && (
                        <div
                          className={`media-visual-region ${imageSelectionDraft ? "draft" : "selected"}`}
                          style={{
                            left: `${displayedImageRegion.x * 100}%`,
                            top: `${displayedImageRegion.y * 100}%`,
                            width: `${displayedImageRegion.width * 100}%`,
                            height: `${displayedImageRegion.height * 100}%`,
                          }}
                        />
                      )}
                    </div>
                  )}
                  <div className="media-visual-preview-hint">
                    <small>{selectedImageRegion ? "已锁定选区：本轮只处理框内内容" : "可拖动框选目标；不框选则处理整张图片"}</small>
                    <div className="media-region-actions">
                      <button type="button" className="media-preview-toggle" onClick={() => setMediaPreviewOpen((open) => !open)} title={mediaPreviewOpen ? "隐藏截图预览" : "显示或修改图片选区"}>
                        {mediaPreviewOpen ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                        {mediaPreviewOpen ? "隐藏截图" : "显示/修改选区"}
                      </button>
                      {selectedImageRegion && (
                        <button type="button" className="media-region-clear" onClick={() => { invalidateMediaPerception(); setSelectedImageRegion(null); setMediaPreviewOpen(true); }} title="清除图片选区">
                          <X size={12} /> 清除选区
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              )}
              <span>{mediaFiles.audio?.name || "未选择 WAV"}</span>
              <span>{mediaFiles.image?.name || "未选择截图"}</span>
              {uiAutomationSnapshot?.element && (
                <span title="本轮明确读取的 Windows UI Automation 控件">
                  控件：{uiAutomationSnapshot.element.name || uiAutomationSnapshot.element.target_kind}
                  {uiAutomationSnapshot.status === "redacted" ? "（已脱敏）" : ""}
                </span>
              )}
              {mediaFiles.audio && (
                <>
                  <button type="button" className="media-download-button" onClick={() => downloadCapturedMedia(mediaFiles.audio)} title="下载本轮录音 WAV">
                    <Download size={13} /> 录音
                  </button>
                  <button type="button" className="media-delete-button" onClick={() => clearCapturedMediaFile("audio")} title="删除本轮录音">
                    <Trash2 size={13} /> 删除录音
                  </button>
                </>
              )}
              {mediaFiles.image && (
                <>
                  <button type="button" className="media-download-button" onClick={() => downloadCapturedMedia(mediaFiles.image)} title="下载本轮授权截图">
                    <Download size={13} /> 截图
                  </button>
                  <button type="button" className="media-delete-button" onClick={() => clearCapturedMediaFile("image")} title="删除本轮截图">
                    <Trash2 size={13} /> 删除截图
                  </button>
                </>
              )}
              <strong>{mediaMessage}</strong>
            </div>
          )}

          {selectedWorkflow && (
            <details className="workflow-contract-review" open={selectedWorkflow.approval.status !== "approved"}>
              <summary>{selectedWorkflow.title} · v{selectedWorkflow.playbook_version} · {selectedWorkflow.approval.status === "approved" ? "当前项目已确认" : "需要确认用途"}</summary>
              <p>{selectedWorkflow.description}</p>
              <ul>{selectedWorkflow.input_contract.map((item) => <li key={item}>{item}</li>)}</ul>
              <p>输出：{selectedWorkflow.output}</p>
              <ul>{selectedWorkflow.side_effects.map((item) => <li key={item}>{item}</li>)}</ul>
              {selectedWorkflow.approval.status !== "approved" && <button type="button" disabled={locked || workflowBusy} onClick={() => void approveCurrentWorkflow()}>确认当前模式用途</button>}
            </details>
          )}
          {workflowMessage && <div className="workflow-readiness-notice" role="alert">{workflowMessage}</div>}
          <form className="ask-box" onSubmit={handleSubmit}>
            <input
              ref={mediaInputRef}
              className="media-file-input"
              type="file"
              accept=".wav,audio/wav,.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp"
              multiple
              onChange={handleMediaSelection}
            />
            <div className="ask-media-controls">
              {selectedWorkflow && (
                <span className="active-workflow-indicator" role="status" title={selectedWorkflow.description}>
                  <strong>{selectedWorkflow.title}</strong>
                  <small>内置会话模式已生效，普通文本可直接提问</small>
                </span>
              )}
              <button
                type="button"
                className="ask-media-button"
                disabled={isGenerating || mediaBusy || locked || mediaCaptureState !== "idle"}
                title="选择本轮 WAV 语音和/或截图；仅本轮端侧处理"
                aria-label="选择本轮多模态输入"
                onClick={() => mediaInputRef.current?.click()}
              >
                <Paperclip size={17} />
              </button>
              <button
                type="button"
                className={`ask-media-button record-media-button ${mediaCaptureState === "recording" ? "recording" : ""}`}
                disabled={isGenerating || mediaBusy || locked || mediaCaptureState === "processing" || mediaCaptureState === "capturing_screen"}
                title={mediaCaptureState === "recording" ? "点击停止录音" : "点击开始录音"}
                aria-label={mediaCaptureState === "recording" ? "停止录音" : "开始录音"}
                aria-pressed={mediaCaptureState === "recording"}
                onClick={toggleAudioRecording}
              >
                <Mic size={17} />
              </button>
              <button
                type="button"
                className="ask-media-button"
                disabled={isGenerating || mediaBusy || locked || mediaCaptureState !== "idle"}
                title="授权一个窗口或屏幕后，仅截取当前首帧为本轮截图"
                aria-label="授权采集当前窗口或屏幕截图"
                onClick={() => void captureScreenFrame()}
              >
                <ImageIcon size={17} />
              </button>
              <select
                className="skill-call-select"
                value={selectedSkillId}
                disabled={isGenerating || locked || sessionLoading || !activeSessionId}
                aria-label="手动调用 Skill"
                title="手动调用已安装 Skill"
                onChange={(event) => setSelectedSkillId(event.target.value)}
              >
                <option value="">不调用 Skill</option>
                {conversationSkills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.title}</option>)}
              </select>
              <select
                className="visual-route-select"
                value={visualMode}
                disabled={isGenerating || mediaBusy || locked || mediaCaptureState !== "idle"}
                aria-label="视觉路径"
                title="选择本轮视觉路由"
                onChange={(event) => setVisualMode(event.target.value as typeof visualMode)}
              >
                <option value="auto">自动选择</option>
                <option value="fast">只读图片文字</option>
                <option value="slow">深度理解图片</option>
              </select>
              <span className="media-auto-warmup-hint">模型按需后台加载</span>
            </div>
            <textarea
              ref={askTextAreaRef}
              className="ask-text-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onPaste={handleAskPaste}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              disabled={isGenerating || locked || indexBusy || sessionLoading || !activeSessionId}
              placeholder={locked ? "私有工作区尚未解锁" : sessionLoading ? "正在切换项目对话..." : !activeProjectId ? "请先创建或选择项目" : isGenerating ? "正在检索、核对与整理..." : selectedWorkflow ? `例如：${selectedWorkflow.suggested_query}` : "输入文档归纳、对比、方案分析或问答任务"}
            />
            <button
              type="submit"
              disabled={(!input.trim() && !mediaFiles.audio && !mediaFiles.image) || isGenerating || workflowBusy || locked || indexBusy || sessionLoading || !activeSessionId}
              title={mediaFiles.audio || mediaFiles.image ? (speechConfirmationPending ? "确认语音并发送当前内容" : "自动融合焦点控件、语音和截图后发送") : "发送文本问题"}
            >
              <Send size={17} />
              {mediaFiles.audio || mediaFiles.image ? (speechConfirmationPending ? "确认并发送" : "发送") : "发送"}
            </button>
          </form>

          {documentLibraryOpen && (
            <div className="document-viewer-backdrop" role="presentation" onMouseDown={closeDocumentLibrary}>
              <section className="document-library-modal" role="dialog" aria-modal="true" aria-label="文档库" onMouseDown={(event) => event.stopPropagation()}>
                <header className="document-viewer-header">
                  <div>
                    <span>DOCUMENT LIBRARY</span>
                    <h2>项目文档库</h2>
                    <small>{documents.length} 份文档 · {indexedDocuments.length} 份已索引 · {activeDocumentNames.length} 份当前上下文</small>
                  </div>
                  <div className="document-viewer-header-actions">
                    <button type="button" className="document-header-action" onClick={() => void previewIndexChanges()} disabled={indexPreviewBusy} title="只读比较当前文件与已有索引">
                      <RefreshCw size={14} /> {indexPreviewBusy ? "检查中…" : "检查变更"}
                    </button>
                    <button type="button" className="document-header-action" onClick={() => openRetrievalTest()} title="测试当前项目的检索结果">
                      <Search size={14} /> 检索测试
                    </button>
                    <button type="button" className="layout-icon-button" onClick={closeDocumentLibrary} title="关闭文档库">
                      <X size={18} />
                    </button>
                  </div>
                </header>
                <div className="document-library-body">
                  {documentDeleteMessage && !documentDeleteTarget && (
                    <div className="document-delete-notice" role="status">{documentDeleteMessage}</div>
                  )}
                  {documentScopeMessage && !documentDeleteTarget && (
                    <div className="document-delete-notice" role="status">{documentScopeMessage}</div>
                  )}
                  {indexPreviewMessage && !documentDeleteTarget && (
                    <div className="document-delete-notice" role="alert">{indexPreviewMessage}</div>
                  )}
                  {indexPreview && !documentDeleteTarget && (
                    <div className="index-preview-summary" role="status" aria-live="polite">
                      <div className="index-preview-summary-header">
                        <strong>索引变更预览</strong>
                        <span>只读检查 · 不会修改文档或索引</span>
                      </div>
                      <div className="index-preview-counts">
                        <span className={indexPreview.counts.added ? "attention" : ""}>新增 {indexPreview.counts.added}</span>
                        <span className={indexPreview.counts.modified ? "attention" : ""}>修改 {indexPreview.counts.modified}</span>
                        <span className={indexPreview.counts.deleted ? "attention" : ""}>删除 {indexPreview.counts.deleted}</span>
                        <span>未变化 {indexPreview.counts.unchanged}</span>
                        <span className={indexPreview.counts.unverified ? "attention" : ""}>未验证 {indexPreview.counts.unverified}</span>
                        <span className={indexPreview.counts.blocked ? "attention" : ""}>阻塞 {indexPreview.counts.blocked}</span>
                      </div>
                      {(indexPreview.modified.length > 0 || indexPreview.deleted.length > 0 || indexPreview.added.length > 0) && (
                        <small>
                          {[...indexPreview.added.map((item) => `新增：${item.name}`), ...indexPreview.modified.map((item) => `修改：${item.name}`), ...indexPreview.deleted.map((item) => `删除：${item.name}`)].slice(0, 6).join(" · ")}
                          {(indexPreview.added.length + indexPreview.modified.length + indexPreview.deleted.length) > 6 ? " · …" : ""}
                        </small>
                      )}
                    </div>
                  )}
                  <div className="document-library-list">
                    {documents.length ? documents.map((document) => (
                      <article key={document.document_id || document.relative_path} className={`document-library-row ${isActiveDocument(document) ? "selected" : ""}`}>
                        <div className="document-library-row-title">
                          {document.media_type?.startsWith("image/") || isImageDocumentName(document.name) ? <ImageIcon size={16} /> : <FileText size={16} />}
                          <div className="document-library-row-title-copy">
                            <strong title={document.name}>{document.name}</strong>
                            {documentIndexDiagnostic(document) && <small className={`document-library-row-diagnostic ${documentIndexStatusClass(document)}`} title={documentIndexDiagnostic(document)}>{documentIndexDiagnostic(document)}</small>}
                          </div>
                        </div>
                        <div className="document-library-row-meta">
                          <span className={documentIndexStatusClass(document)}>{documentIndexStatusLabel(document)}</span>
                          <span className={document.scope === "global" ? "global" : document.scope === "ambiguous" ? "ambiguous" : "project"}>
                            {document.scope === "global" ? "全局知识库" : document.scope === "ambiguous" ? "多个项目绑定" : "项目专属"}
                          </span>
                          <span>{formatBytes(document.size_bytes)}</span>
                          <span>{formatDocumentTime(document.modified_at)}</span>
                        </div>
                        <div className="document-library-row-actions">
                          <button type="button" className="document-view-button" onClick={() => void openDocumentViewer(document)}>查看</button>
                          <button type="button" className="document-context-button" disabled={!document.indexed} onClick={() => toggleDocumentContext(document)}>
                            <LocateFixed size={12} /> {isActiveDocument(document) ? "已选上下文" : "加入上下文"}
                          </button>
                          {document.scope === "project" && (
                            <button
                              type="button"
                              className="document-globalize-button"
                              disabled={documentScopeBusy || indexBusy || documentDeleteBusy}
                              onClick={() => void globalizeDocument(document)}
                              title="解除当前项目绑定并归入全局知识库"
                            >
                              归为全局
                            </button>
                          )}
                          <button type="button" className="document-delete-button" onClick={() => openDocumentDeleteConfirmation(document)} title="删除文档及其本地索引">
                            <Trash2 size={12} /> 删除
                          </button>
                        </div>
                      </article>
                    )) : <div className="paper-empty"><FileText size={32} /><span>暂无文档，请先导入材料</span></div>}
                  </div>
                </div>
                {documentDeleteTarget && (
                  <div className="document-delete-dialog" role="dialog" aria-modal="true" aria-label="删除文档确认">
                    <div>
                      <span>删除文档</span>
                      <h3>{documentDeleteTarget.name}</h3>
                      <p>将删除原文件及其本地索引，无法恢复。</p>
                    </div>
                    {!documentDeleteAuthorization ? (
                      <form onSubmit={(event) => { event.preventDefault(); void authorizeDocumentDelete(); }}>
                        <input type="password" autoFocus value={documentDeletePassphrase} onChange={(event) => setDocumentDeletePassphrase(event.target.value)} placeholder="输入工作区密码" />
                        <div><button type="button" onClick={() => { setDocumentDeleteTarget(null); setDocumentDeleteIdempotencyKey(null); }}>取消</button><button type="submit" disabled={documentDeleteBusy || !documentDeletePassphrase}>校验并删除</button></div>
                      </form>
                    ) : (
                      <div className="document-delete-actions"><button type="button" disabled={documentDeleteBusy} onClick={() => { setDocumentDeleteTarget(null); setDocumentDeleteIdempotencyKey(null); }}>取消</button><button type="button" className="danger" disabled={documentDeleteBusy} onClick={() => void deleteDocument()}>{documentDeleteBusy ? "正在删除…" : "确认删除"}</button></div>
                    )}
                    {documentDeleteMessage && <small>{documentDeleteMessage}</small>}
                  </div>
                )}
              </section>
            </div>
          )}

          {retrievalTestOpen && (
            <div className="document-viewer-backdrop" role="presentation" onMouseDown={closeRetrievalTest}>
              <section className="retrieval-test-modal" role="dialog" aria-modal="true" aria-label="检索测试" onMouseDown={(event) => event.stopPropagation()}>
                <header className="document-viewer-header">
                  <div>
                    <span>RETRIEVAL TEST</span>
                    <h2>检索测试</h2>
                    <small>只读检查召回结果，不生成回答，不修改文档或索引</small>
                  </div>
                  <button type="button" className="layout-icon-button" onClick={closeRetrievalTest} title="关闭检索测试">
                    <X size={18} />
                  </button>
                </header>
                <form className="retrieval-test-form" onSubmit={(event) => { event.preventDefault(); void runRetrievalTest(); }}>
                  <label>
                    <span>测试查询</span>
                    <textarea
                      value={retrievalTestQuery}
                      onChange={(event) => setRetrievalTestQuery(event.target.value)}
                      rows={3}
                      maxLength={6000}
                      placeholder="输入要检查的文档问题或章节关键词"
                      disabled={retrievalTestBusy || locked}
                    />
                  </label>
                  <div className="retrieval-test-form-actions">
                    <span>{activeProjectId ? "当前项目 + 全局知识库" : "全局知识库"}</span>
                    <button type="submit" disabled={retrievalTestBusy || locked || !retrievalTestQuery.trim()}>
                      <Search size={14} /> {retrievalTestBusy ? "检索中…" : "运行检索"}
                    </button>
                  </div>
                </form>
                {retrievalTestMessage && <div className="retrieval-test-message" role="status">{retrievalTestMessage}</div>}
                {retrievalTestResult && (
                  <div className="retrieval-test-results">
                    <div className="retrieval-test-summary">
                      <span>候选 {retrievalTestResult.chunks.length}</span>
                      <span>路由 {retrievalTestResult.project_route || "global"}</span>
                      <span>证据门 {String(retrievalTestResult.observability?.sufficiency?.decision ?? "未计算")}</span>
                      <span>{Math.round(retrievalTestResult.elapsed_ms)} ms</span>
                    </div>
                    {retrievalTestResult.query_variants.length > 1 && (
                      <div className="retrieval-test-variants">
                        <strong>可尝试的查询变体</strong>
                        <div>
                          {retrievalTestResult.query_variants.map((variant) => (
                            <div key={variant} className="retrieval-test-variant-row">
                              <button type="button" onClick={() => void runRetrievalTest(variant)} disabled={retrievalTestBusy || retrievalFeedbackBusy}>
                                {variant}
                              </button>
                              {variant.trim() !== retrievalTestQuery.trim() && (
                                <>
                                  <button type="button" className="retrieval-test-save-button" onClick={() => void saveRetrievalFeedback(variant, "global")} disabled={retrievalTestBusy || retrievalFeedbackBusy}>
                                    {retrievalFeedbackBusy ? "保存中…" : selectedWorkflowId ? "记住全局·当前窗口" : "记住全局"}
                                  </button>
                                  {activeProjectId && (
                                    <button type="button" className="retrieval-test-save-button" onClick={() => void saveRetrievalFeedback(variant, "project")} disabled={retrievalTestBusy || retrievalFeedbackBusy}>
                                      {selectedWorkflowId ? "记住当前项目·当前窗口" : "记住当前项目"}
                                    </button>
                                  )}
                                </>
                              )}
                            </div>
                          ))}
                        </div>
                        <small>只有你明确点击“记住”后，改写才会进入后续召回；它不会修改原文档或索引。</small>
                      </div>
                    )}
                    {(retrievalTestResult.feedback || []).length > 0 && (
                      <div className="retrieval-test-feedback">
                        <strong>已生效的检索提示</strong>
                        {(retrievalTestResult.feedback || []).map((feedback) => (
                          <div key={feedback.feedback_id}>
                            <span>{feedback.scope === "global" ? "全局" : "当前项目"}{feedback.workflow_id ? ` · ${workflowSpecs.find((workflow) => workflow.workflow_id === feedback.workflow_id)?.title || feedback.workflow_id}` : ""} · {feedback.corrected_query}</span>
                            <button type="button" onClick={() => void revokeRetrievalFeedback(feedback)} disabled={retrievalFeedbackBusy || retrievalTestBusy}>
                              撤销
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                    <div className="retrieval-test-chunks">
                      {retrievalTestResult.chunks.length ? retrievalTestResult.chunks.map((chunk) => (
                        <article key={`${chunk.id}-${chunk.candidate_id || "candidate"}`} className="retrieval-test-chunk">
                          <header>
                            <strong>{chunk.id}</strong>
                            <span>{chunk.source_filename} · {chunk.page ? `第 ${chunk.page} 页` : "未标页码"}</span>
                          </header>
                          <div className="retrieval-test-chunk-meta">
                            <span>{chunk.section_path?.length ? chunk.section_path.join(" / ") : chunk.section}</span>
                            <span>排序 {typeof chunk.score === "number" ? chunk.score.toFixed(3) : "—"}</span>
                            {chunk.section_route_mode && <span>路由 {chunk.section_route_mode} · {Math.round((chunk.section_route_coverage || 0) * 100)}%</span>}
                            {chunk.section_node_title && <span>节点 {chunk.section_node_title} · L{chunk.section_node_depth || "—"} · 子节点 {chunk.section_node_child_count || 0}</span>}
                          </div>
                          {chunk.support_summary && <p className="retrieval-test-support">支持度 {typeof chunk.support_score === "number" ? chunk.support_score.toFixed(3) : "—"} · {chunk.support_summary}{chunk.section_route_source ? ` · ${chunk.section_route_source}` : ""}</p>}
                          <pre>{chunk.content}{chunk.truncated ? "\n…（已截断）" : ""}</pre>
                          {chunk.breakdown && Object.keys(chunk.breakdown).length > 0 && (
                            <small className="retrieval-test-breakdown">
                              {Object.entries(chunk.breakdown).slice(0, 5).map(([key, value]) => `${key} ${Number(value).toFixed(3)}`).join(" · ")}
                            </small>
                          )}
                        </article>
                      )) : <p className="retrieval-test-empty">本次没有通过候选门的检索分块。</p>}
                    </div>
                  </div>
                )}
              </section>
            </div>
          )}

          {documentViewerOpen && documentPreview && (
            <div className="document-viewer-backdrop" role="presentation" onMouseDown={() => setDocumentViewerOpen(false)}>
              <section className="document-viewer" role="dialog" aria-modal="true" aria-label="文档查看器" onMouseDown={(event) => event.stopPropagation()}>
                <header className="document-viewer-header">
                  <div>
                    <span>DOCUMENT VIEWER</span>
                    <h2>{documentPreview.name}</h2>
                    <small>{documentPreview.media_type || "文档"} · 只读预览 · 不会修改原文件</small>
                  </div>
                  <button type="button" className="layout-icon-button" onClick={() => setDocumentViewerOpen(false)} title="关闭查看器">
                    <X size={18} />
                  </button>
                </header>
                <div className="document-viewer-toolbar">
                  <button type="button" className="document-context-button" onClick={() => toggleDocumentContext(documentPreview)}>
                    <LocateFixed size={13} />
                    {isActiveDocument(documentPreview) ? "移出上下文" : "加入上下文"}
                  </button>
                  <span>{activeDocumentNames.length ? `当前已选 ${activeDocumentNames.length} 份上下文` : "当前使用全局索引检索"}</span>
                  {documentPreview.chunks?.length ? (
                    <div className="document-preview-tabs" role="tablist" aria-label="预览方式">
                      <button type="button" className={documentPreviewMode === "text" ? "active" : ""} onClick={() => setDocumentPreviewMode("text")}>清洗正文</button>
                      <button type="button" className={documentPreviewMode === "chunks" ? "active" : ""} onClick={() => setDocumentPreviewMode("chunks")}>切片 {documentPreview.chunks.length}</button>
                    </div>
                  ) : null}
                </div>
                <article className="document-viewer-content">
                  {documentPreviewMode === "chunks" && documentPreview.chunks?.length ? (
                    <div className="document-preview-chunks">
                      {documentPreview.chunks.map((chunk) => (
                        <section key={chunk.id} className="document-preview-chunk">
                          <header><strong>切片 {chunk.index}</strong><span>{chunk.page ? `第 ${chunk.page} 页` : "未标页码"} · {chunk.section} · 已生成 {chunk.child_count} 个检索子块</span></header>
                          <EvidenceMarkdown>{chunk.text}</EvidenceMarkdown>
                        </section>
                      ))}
                    </div>
                  ) : documentPreview.text ? <EvidenceMarkdown>{documentPreview.text}</EvidenceMarkdown> : <p>该文件暂无可直接预览的文本内容。</p>}
                </article>
                {documentPreview.truncated && <footer className="document-viewer-footer">预览已截取前 30,000 个字符；完整内容仍保留在本地索引中。</footer>}
              </section>
            </div>
          )}
        </main>

        <button
          type="button"
          className="workbench-splitter"
          title="拖拽调整聊天区与导图区宽度"
          aria-label="调整聊天区与导图区宽度"
          aria-orientation="vertical"
          onPointerDown={startWorkbenchResize}
          onKeyDown={handleSplitterKeyDown}
        />

        <aside className={`artifact-studio ${artifactCollapsed ? "collapsed" : ""}`}>
          <div className="studio-header">
            <div>
              <span>
                <Layers3 size={14} />
                Evidence Studio
              </span>
              <h2>证据链与结构导图</h2>
            </div>
            {artifactCollapsed ? (
              <button type="button" className="studio-toggle" title="展开证据详情" onClick={() => setArtifactCollapsed(false)}>
                <ChevronLeft size={14} />
              </button>
            ) : (
              <button type="button" className="studio-toggle" title="收起证据详情" onClick={() => setArtifactCollapsed(true)}>
                <ChevronRight size={14} />
              </button>
            )}
            <div className="artifact-tabs">
              <button type="button" aria-pressed={leftCollapsed && Math.abs(artifactWeight - GRAPH_PRIORITY_ARTIFACT_WEIGHT) < 0.01} title={leftCollapsed && Math.abs(artifactWeight - GRAPH_PRIORITY_ARTIFACT_WEIGHT) < 0.01 ? "恢复均衡布局" : "导图优先布局"} onClick={useGraphPriorityLayout}>
                <Layers3 size={14} />
                {leftCollapsed && Math.abs(artifactWeight - GRAPH_PRIORITY_ARTIFACT_WEIGHT) < 0.01 ? "恢复" : "优先"}
              </button>
              <button type="button" className={artifactTab === "evidence" ? "active" : ""} onClick={() => openArtifactStudio("evidence")}>
                <ClipboardList size={14} />
                证据
              </button>
              <button type="button" className={artifactTab === "graph" ? "active" : ""} onClick={() => openArtifactStudio("graph")}>
                <GitBranch size={14} />
                导图
              </button>
              <button type="button" className={artifactTab === "audit" ? "active" : ""} onClick={() => openArtifactStudio("audit")}>
                <ShieldCheck size={14} />
                审计
              </button>
            </div>
          </div>

          {!artifactCollapsed && (
            <div className="studio-body">
              {artifactTab === "evidence" && (
                <EvidencePanel evidence={latestEvidence} />
              )}
              {artifactTab === "graph" && (
                <GraphPanel
                  latestMindmap={latestMindmap}
                  mindmapLayout={mindmapLayout}
                  toggleMindmapNode={toggleMindmapNode}
                />
              )}
              {artifactTab === "audit" && <AuditPanel events={auditEvents} />}
            </div>
          )}
        </aside>
      </section>

      {reviewState.mode === "reviewing" && reviewState.review && (
        <section className="learning-review-backdrop" role="presentation">
          <div className="learning-review-dialog" role="dialog" aria-modal="true" aria-labelledby="learning-review-title">
            <div className="learning-review-head">
              <div>
                <span>Learning Review</span>
                <h2 id="learning-review-title">审核候选记忆</h2>
              </div>
              <button
                type="button"
                title="关闭审核"
                onClick={() => setReviewState((previous) => ({ ...previous, mode: "idle", review: null, message: "" }))}
              >
                <X size={17} />
              </button>
            </div>
            <div className="learning-review-list">
              {reviewState.review.candidates.map((candidate) => {
                const edit = reviewState.edits[candidate.candidate_id] || {
                  title: candidate.title,
                  content: candidate.content,
                };
                const selected = reviewState.selectedIds.has(candidate.candidate_id);

                return (
                  <article key={candidate.candidate_id} className={`learning-review-card ${selected ? "selected" : ""}`}>
                    <label className="learning-review-select">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={(event) => {
                          setReviewState((previous) => {
                            const selectedIds = new Set(previous.selectedIds);
                            if (event.target.checked) {
                              selectedIds.add(candidate.candidate_id);
                            } else {
                              selectedIds.delete(candidate.candidate_id);
                            }
                            return { ...previous, selectedIds };
                          });
                        }}
                      />
                      <span>
                        {candidate.asset_type.toUpperCase()} · {candidate.subtype === "project_constraint" ? "项目约束" : candidate.subtype}
                      </span>
                    </label>
                    <input
                      value={edit.title}
                      onChange={(event) => updateReviewEdit(candidate.candidate_id, "title", event.target.value)}
                      aria-label="候选标题"
                    />
                    <textarea
                      value={edit.content}
                      onChange={(event) => updateReviewEdit(candidate.candidate_id, "content", event.target.value)}
                      aria-label="候选内容"
                    />
                    <div className="learning-review-meta">
                      <span>{Math.round(candidate.confidence * 100)}%</span>
                      <span>{candidate.rationale}</span>
                    </div>
                    {candidate.evidence.length > 0 && <p>{candidate.evidence[0]}</p>}
                  </article>
                );
              })}
            </div>
            <div className="learning-review-actions">
              <button
                type="button"
                onClick={() => setReviewState((previous) => ({ ...previous, mode: "idle", review: null, message: "" }))}
              >
                取消
              </button>
              <button type="button" onClick={() => void submitReflectionDecisions()}>
                确认记忆
              </button>
            </div>
          </div>
        </section>
      )}

      {memoryManagerOpen && (
        <section className="learning-review-backdrop" role="presentation">
          <div className="learning-memory-dialog" role="dialog" aria-modal="true" aria-labelledby="learning-memory-title">
            <div className="learning-review-head">
              <div>
                <span>Personalization Profile</span>
                <h2 id="learning-memory-title">偏好画像</h2>
              </div>
              <button
                type="button"
                title="关闭偏好画像"
                onClick={() => setMemoryManagerOpen(false)}
              >
                <X size={17} />
              </button>
            </div>
            <div className="learning-memory-summary">
              <p>
                {activeProjectId ? "当前显示：当前项目长期记忆/约束 + 全局偏好。其他项目内容不会出现在这里。" : "当前显示：全部已启用全局偏好。选择项目后可按项目范围管理。"}
              </p>
              <p>偏好和项目约束只影响当前项目的工作方式，不得改写事实、数值、证据或安全边界。</p>
              {memoryHealth && (
                <p className="learning-memory-message" role="status">
                  {memoryHealth.status === "ok"
                    ? `记忆健康检查正常：活动事实 ${memoryHealth.counts.active_memories} 条，检索块 ${memoryHealth.counts.active_memory_chunks} 个。`
                    : `记忆健康检查发现 ${memoryHealth.issue_count} 项待处理问题（活动事实 ${memoryHealth.counts.active_memories} 条，待同步 ${memoryHealth.counts.pending_syncs} 条）。请优先人工检查，不会自动合并或删除记忆。`}
                </p>
              )}
              {memoryManagerMessage && <p className="learning-memory-message" role="status">{memoryManagerMessage}</p>}
            </div>
            <div className="learning-memory-list">
              {memoryManagerBusy && <p className="learning-memory-empty">正在同步偏好画像…</p>}
              {!memoryManagerBusy && learningMemories.length === 0 && (
                <p className="learning-memory-empty">当前范围没有已启用的长期记忆或项目约束。</p>
              )}
              {!memoryManagerBusy && learningMemories.map((memory) => {
                const edit = memoryEdits[memory.memory_id] || { title: memory.title, content: memory.content };
                const sourceIndexes = memory.source_trace?.message_indexes || [];
                const validityLabel = memory.validity_status === "scheduled"
                  ? "计划生效"
                  : memory.validity_status === "expired"
                    ? "已过期"
                    : "当前生效";
                const confirmationLabel = memory.confirmation_status === "human_reviewed"
                  ? "人工确认"
                  : memory.confirmation_status === "explicit_signal"
                    ? "明确表达 · 未人工复核"
                    : "历史记录";
                return (
                  <article key={memory.memory_id} className="learning-memory-card">
                    <div className="learning-memory-card-head">
                      <div className="learning-memory-badges">
                        <span>
                          {memory.subtype === "project_constraint"
                            ? `项目约束 · ${memory.project_id || "当前项目"}`
                            : memory.subtype === "project_fact"
                              ? `项目事实 · ${memory.project_id || "当前项目"}`
                              : memory.scope === "global"
                                ? "全局偏好"
                                : `项目偏好 · ${memory.project_id || "当前项目"}`}
                        </span>
                        <span>{validityLabel}</span>
                        <span>{confirmationLabel}</span>
                        <span>v{memory.record_version || 1}</span>
                      </div>
                      <code>{memory.memory_id}</code>
                    </div>
                    <input
                      value={edit.title}
                      onChange={(event) => updateMemoryEdit(memory.memory_id, "title", event.target.value)}
                      aria-label={`${memory.title}的偏好标题`}
                    />
                    <textarea
                      value={edit.content}
                      onChange={(event) => updateMemoryEdit(memory.memory_id, "content", event.target.value)}
                      aria-label={`${memory.title}的偏好内容`}
                    />
                    <div className="learning-memory-meta">
                      <span>确认：{memory.confirmed_at ? formatDocumentTime(memory.confirmed_at) : "未人工确认"}</span>
                      <span>最近使用：{memory.last_used_at ? formatDocumentTime(memory.last_used_at) : "尚未注入"}</span>
                      <span>更新：{formatDocumentTime(memory.updated_at)}</span>
                    </div>
                    <p className="learning-memory-source">
                      来源：{memory.source_excerpt || memory.source_trace?.excerpt || "已保存来源，但没有可展示的原文摘录"}
                      {sourceIndexes.length > 0 && ` · 会话消息 ${sourceIndexes.join(", ")}`}
                    </p>
                    <div className="learning-memory-actions">
                      <button type="button" disabled={memoryManagerBusy} onClick={() => void saveLearningMemory(memory)}>保存修改</button>
                      <button type="button" disabled={memoryManagerBusy} onClick={() => void revokeLearningMemory(memory)}>停用</button>
                      <button type="button" className="danger" disabled={memoryManagerBusy} onClick={() => void deleteLearningMemory(memory)}>删除</button>
                    </div>
                  </article>
                );
              })}
            </div>
            <div className="learning-review-actions">
              <span>停用保留加密审计记录；删除会移除活动索引。</span>
              <button type="button" onClick={() => setMemoryManagerOpen(false)}>完成</button>
            </div>
          </div>
        </section>
      )}

      {presentationOpen && (
        <section className="learning-review-backdrop" role="presentation">
          <div className="presentation-dialog" role="dialog" aria-modal="true" aria-labelledby="presentation-title">
            <div className="learning-review-head">
              <div>
                <span>Presentation Builder</span>
                <h2 id="presentation-title">生成 WPS 演示文稿</h2>
              </div>
              <button type="button" title="关闭" onClick={() => setPresentationOpen(false)}>
                <X size={17} />
              </button>
            </div>

            <div className="presentation-builder-body">
              <div className="presentation-source-list" aria-label="已索引文档">
                <button type="button" onClick={selectRecentPresentationSources} disabled={Boolean(presentationTask)}>选择最近 7 天文档</button>
                <small>按本地导入/保存时间筛选，不代表论文发表时间。</small>
                {presentationDocuments.map((document) => (
                  <label key={document.document_id || document.relative_path} className={isPresentationSource(document) ? "selected" : ""}>
                    <input
                      type="checkbox"
                      checked={isPresentationSource(document)}
                      disabled={Boolean(presentationTask) || (!isPresentationSource(document) && presentationSources.length >= 12)}
                      onChange={(event) => {
                        setPresentationSources((previous) => {
                          const selected = previous.some((item) => document.document_id
                            ? item.document_id === document.document_id
                            : !item.document_id && item.name === document.name);
                          if (event.target.checked === selected) return previous;
                          const source = {
                            name: document.name,
                            ...(document.document_id ? { document_id: document.document_id } : {}),
                          };
                          return event.target.checked
                            ? [...previous, source].slice(0, 12)
                            : previous.filter((item) => document.document_id
                              ? item.document_id !== document.document_id
                              : item.document_id || item.name !== document.name);
                        });
                      }}
                    />
                    <FileText size={15} />
                    <span title={document.name}>{document.name}</span>
                  </label>
                ))}
                <section className="presentation-task-list" aria-label="当前项目 PPT 任务">
                  <strong>当前项目任务</strong>
                  <button type="button" onClick={() => setPresentationTasksRefresh((value) => value + 1)}>刷新任务列表</button>
                  {presentationTasksMessage && <p role="status">{presentationTasksMessage}</p>}
                  {!presentationTasksMessage && !presentationTasks.length && <small>暂无持久化 PPT 任务</small>}
                  {presentationTasks.map((task) => (
                    <button type="button" key={task.task_id} className={presentationTask?.task_id === task.task_id ? "selected" : ""} onClick={() => selectPresentationTask(task)}>
                      <strong>{task.result?.outline?.title || (task.kind === "presentation.plan" ? "大纲计划" : "PPT 生成")} · {task.status}</strong>
                      <small>{task.task_id.slice(-16)} · {task.cancellation_pending ? "正在停止，等待步骤结束" : task.current_step || `${task.progress.completed}/${task.progress.total} 步`}</small>
                    </button>
                  ))}
                </section>
              </div>

              <div className="presentation-options">
                {presentationTask ? (
                  <p role="note">当前查看已保存任务，使用原计划参数。修改需返回参数编辑并生成新大纲。</p>
                ) : (<>
                <div className="presentation-skill-strip" aria-label="可参与 PPT 生成的技能">
                  <div className="presentation-skill-strip-head">
                    <span>PPT 可用技能</span>
                    <small>{presentationStyleSkills.length} 个</small>
                  </div>
                  <div className="presentation-skill-chips">
                    {presentationStyleSkills.length ? presentationStyleSkills.map((skill) => (
                      <button
                        key={skill.skill_id}
                        type="button"
                        className={`presentation-skill-chip ${presentationStyleSkillId === skill.skill_id ? "active" : ""}`}
                        onClick={() => setPresentationStyleSkillId(skill.skill_id)}
                        title={compactPresentationSkillDescription(skill)}
                      >
                        {skill.title}
                      </button>
                    )) : <span className="presentation-skill-empty">暂无可直接参与 PPT 生成的技能</span>}
                  </div>
                </div>
                <label>
                  <span>汇报标题</span>
                  <input
                    value={presentationTitle}
                    onChange={(event) => setPresentationTitle(event.target.value)}
                    maxLength={120}
                    placeholder="留空则自动生成"
                  />
                </label>
                <label>
                  <span>汇报对象</span>
                  <select value={presentationAudience} onChange={(event) => setPresentationAudience(event.target.value as typeof presentationAudience)}>
                    <option value="research">高校论文与实验汇报</option>
                    <option value="technical">技术评审</option>
                    <option value="government">政府汇报</option>
                  </select>
                </label>
                <label>
                  <span>页数上限</span>
                  <input
                    type="number"
                    min={4}
                    max={16}
                    value={presentationMaxSlides}
                    onChange={(event) => setPresentationMaxSlides(Math.max(4, Math.min(16, Number(event.target.value) || 10)))}
                  />
                </label>
                <label>
                  <span>PPT 风格 Skill（可留空）</span>
                  <select value={presentationStyleSkillId} onChange={(event) => setPresentationStyleSkillId(event.target.value)}>
                    <option value="">不使用 Skills</option>
                    {presentationStyleSkills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.title}</option>)}
                  </select>
                </label>
                <label className="presentation-toggle">
                  <input type="checkbox" checked={presentationOpenInWps} onChange={(event) => setPresentationOpenInWps(event.target.checked)} />
                  <span>完成后用 WPS 打开</span>
                </label>
                </>)}
                <div className={`presentation-task-state ${presentationTask?.status || "idle"}`} role="status" aria-live="polite">
                  <strong>{presentationTask?.status?.toUpperCase() || "READY"}</strong>
                  <span>{presentationMessage || `已选择 ${presentationSources.length} 份文档`}</span>
                  {presentationTask && (
                    <div className="presentation-progress">
                      <i style={{ width: `${Math.round(100 * presentationTask.progress.completed / Math.max(1, presentationTask.progress.total))}%` }} />
                    </div>
                  )}
                  {presentationTask?.repetition_guard?.decision === "warn" ? (
                    <p className="presentation-repetition-warning" role="alert">
                      检测到相同任务输入曾失败或中断（{presentationTask.repetition_guard.matches?.length || 0} 次）。请先检查执行轨迹，再决定是否继续。
                    </p>
                  ) : null}
                  {presentationTask?.steps?.length ? (
                    <details className="presentation-attempt-history">
                      <summary>
                        执行轨迹 · {presentationTask.progress.completed}/{presentationTask.progress.total} 步 · {presentationTask.steps.reduce((total, step) => total + Number(step.attempt_count || 0), 0)} 次尝试
                      </summary>
                      <ol>
                        {presentationTask.steps.map((step) => (
                          <li key={step.step_name}>
                            <div>
                              <b>{step.step_name}</b>
                              <span>{formatTaskStepStatus(step.status)} · {step.attempt_count || 0} 次</span>
                            </div>
                            {step.attempt_history?.length ? (
                              <ul>
                                {step.attempt_history.map((attempt) => (
                                  <li key={`${step.step_name}-${attempt.attempt}`}>
                                    第 {attempt.attempt} 次：{formatTaskStepStatus(attempt.status)}{attempt.error ? ` · ${attempt.error.slice(0, 240)}` : ""}
                                  </li>
                                ))}
                              </ul>
                            ) : null}
                          </li>
                        ))}
                      </ol>
                    </details>
                  ) : null}
                  {presentationTask?.result?.outline && (
                    <div className="presentation-outline-review">
                      <strong>大纲审核 · {presentationTask.result.outline.title || presentationTitle || "未命名汇报"}</strong>
                      <span>确认下面的实际大纲摘要后，系统才会生成本地 PPTX。</span>
                      {presentationTask.result.evidence_validation?.notice && <span role="status">{presentationTask.result.evidence_validation.notice}</span>}
                      <ol>
                        {(presentationTask.result.outline.slides || []).map((slide, index) => (
                          <li key={`${index}-${slide.title || "slide"}`}>
                            <b>{index + 1}. {slide.title || "未命名页面"}</b>
                            {slide.points?.length ? <small>{slide.points.join(" · ")}</small> : null}
                            {slide.sources?.length ? <em>证据：{slide.sources.join("、")}</em> : null}
                          </li>
                        ))}
                      </ol>
                      {presentationTask.result.plan_digest && <code>摘要：{presentationTask.result.plan_digest.slice(0, 16)}…</code>}
                    </div>
                  )}
                  {presentationTask?.result?.output_path && (
                    <p title={presentationTask.result.output_path}>
                      {presentationTask.result.slide_count} 页 · {presentationTask.result.renderer} · {presentationTask.result.outline_source === "local_ollama" ? "Ollama 提纲" : presentationTask.result.outline_source === "experiment_ledger_extractive" ? "账本测量提纲" : presentationTask.result.outline_source === "extractive" ? "证据提炼" : "自定义提纲"}
                      {presentationTask.result.evidence_metrics ? <><br />来源标注页 {Math.round(presentationTask.result.evidence_metrics.evidence_slide_coverage * 100)}% · {formatPresentationEvidenceCoverage(presentationTask.result.evidence_metrics)} · {Math.round(presentationTask.result.elapsed_ms || 0)} ms</> : null}
                      {presentationTask.result.skill_application?.skill_title && <><br />Skill：{presentationTask.result.skill_application.skill_title} · 指令{presentationTask.result.skill_application.instructions_applied ? "已应用" : "未应用"} · 样式{presentationTask.result.skill_application.style_applied ? "已应用" : "未提供"}</>}
                      {presentationTask.result.artifact_id && <><br />项目产物已登记：{presentationTask.result.artifact_id}</>}
                    </p>
                  )}
                  {presentationTask?.result?.output_path && <button type="button" onClick={() => void invoke("open_path", { path: presentationTask.result?.output_path }).catch(() => setPresentationMessage("无法定位结果文件，请核对本地路径"))}>定位结果文件</button>}
                </div>
              </div>
            </div>

            <div className="learning-review-actions">
              <button type="button" onClick={() => setPresentationOpen(false)}>关闭</button>
              {presentationTask && ["pending", "running"].includes(presentationTask.status) ? (
                <button type="button" onClick={() => void cancelPresentation()} disabled={presentationTask.cancellation_pending}>停止</button>
              ) : presentationTask?.kind === "presentation.plan" && presentationTask.status === "completed" && (presentationTask.plan_digest || presentationTask.result?.plan_digest) ? (
                <>
                  <button type="button" onClick={resetPresentationPlan}>返回修改参数</button>
                  <button type="button" onClick={() => void confirmPresentation()}>确认并生成 PPT</button>
                </>
              ) : presentationTask?.recoverable ? (
                <button type="button" onClick={() => void resumePresentation()}>继续生成</button>
              ) : presentationTask ? (
                <button type="button" onClick={resetPresentationPlan}>新建大纲</button>
              ) : (
                <button
                  type="button"
                  disabled={!presentationSources.length}
                  onClick={() => void generatePresentation()}
                >
                  生成大纲
                </button>
              )}
            </div>
          </div>
        </section>
      )}

      {skillOpen && (
        <section className="learning-review-backdrop" role="presentation">
          <div className="skill-dialog" role="dialog" aria-modal="true" aria-labelledby="skill-title">
            <div className="learning-review-head">
              <div><span>Research Skills</span><h2 id="skill-title">资料处理 Skill</h2></div>
              <button type="button" title="关闭" onClick={() => setSkillOpen(false)}><X size={17} /></button>
            </div>
            {skillMessage && (
              <div className="skill-status-overlay" role="status" aria-live="polite">
                <div className="skill-status-banner">
                  <span>{skillMessage}</span>
                  <button type="button" title="关闭提示" aria-label="关闭提示" onClick={() => setSkillMessage("")}><X size={14} /></button>
                </div>
              </div>
            )}
            {pendingSkillDelete && (
              <div className="skill-confirm-overlay" role="presentation">
                <div className="skill-confirm-card" role="dialog" aria-modal="true" aria-labelledby="skill-confirm-title">
                  <strong id="skill-confirm-title">删除已安装 Skill</strong>
                  <p>确定删除「{pendingSkillDelete.title}」吗？</p>
                  <div className="skill-confirm-actions">
                    <button
                      type="button"
                      className="danger"
                      disabled={skillBusy}
                      onClick={() => {
                        const target = pendingSkillDelete;
                        setPendingSkillDelete(null);
                        if (target) void deleteProjectSkill(target);
                      }}
                    >
                      确认删除
                    </button>
                    <button type="button" disabled={skillBusy} onClick={() => setPendingSkillDelete(null)}>
                      取消
                    </button>
                  </div>
                </div>
              </div>
            )}
            {skillAdapterReview && (
              <div className="skill-confirm-overlay" role="presentation">
                <div className="skill-adapter-review-card" role="dialog" aria-modal="true" aria-labelledby="skill-adapter-title">
                  <strong id="skill-adapter-title">人工审核本地适配契约</strong>
                  <p>{skillAdapterReview.skill_title || skillAdapterReview.skill_name} · {skillAdapterReview.target === "presentation" ? "PPT" : "对话"} Skill</p>
                  <pre>{JSON.stringify(skillAdapterReview.adapter || {}, null, 2)}</pre>
                  <div className="skill-confirm-actions">
                    <button type="button" disabled={skillBusy} onClick={() => void activateReviewedSkill()}>确认启用</button>
                    <button type="button" disabled={skillBusy} onClick={() => setSkillAdapterReview(null)}>暂不启用</button>
                  </div>
                </div>
              </div>
            )}
            <div className="skill-dialog-body">
              <section className="skill-library">
                <p>下载时先选择用途；系统会自动生成静态适配契约，但必须由你查看后确认启用。原始 SKILL.md 仅用于追溯，脚本不会执行。</p>
                <label>
                  <span>安装入口</span>
                  <input value={skillSource} maxLength={512} onChange={(event) => setSkillSource(event.target.value)} placeholder="GitHub URL、npx skills add、npx add-skill、npm install" />
                </label>
                <label>
                  <span>安装用途</span>
                  <select value={skillInstallTarget} onChange={(event) => setSkillInstallTarget(event.target.value as typeof skillInstallTarget)}>
                    <option value="conversation">对话 Skill</option>
                    <option value="presentation">PPT Skill</option>
                  </select>
                </label>
                <div className="skill-install-actions">
                  <button type="button" disabled={skillBusy || !skillSource.trim()} onClick={() => void installProjectSkill()}>安装到项目</button>
                  <button type="button" disabled={skillBusy} onClick={() => void syncProjectSkills()}>刷新列表</button>
                  <button type="button" disabled={skillBusy || !skillRootPath} onClick={() => void openSkillDirectory()}>打开本地目录</button>
                </div>
                {skillInbox.map((skill) => (
                  <article key={skill.skill_name} className="skill-card">
                    <div><strong>待审核：{skill.skill_title}</strong></div>
                    <p>{skill.description || "安装内容已下载，等待用户审核。"} · {skill.target === "presentation" ? "PPT" : "对话"} Skill</p>
                    <button type="button" disabled={skillBusy} onClick={() => void approveProjectSkill(skill)}>生成并查看适配契约</button>
                  </article>
                ))}
                {installedSkills.length ? managedSkillGroups.map((group) => (
                  <section key={group.target} className="skill-group" aria-label={group.title}>
                    <h3>{group.title}</h3>
                    {group.skills.length ? group.skills.map((skill) => (
                      <article key={skill.skill_id} className="skill-card">
                        <div><strong>{skill.title}</strong></div>
                        <p>{compactSkillDescription(skill)}</p>
                        <small className="skill-resource-meta">{compactSkillResources(skill)}</small>
                        <div className="skill-card-actions">
                          <button type="button" disabled={skillBusy} onClick={() => void openSkillSourceInEditor(skill)}>查看原始指令</button>
                          <button type="button" className="danger" disabled={skillBusy || skillDeletingId === skill.skill_id} onClick={() => setPendingSkillDelete(skill)}>
                            {skillDeletingId === skill.skill_id ? "正在删除..." : "删除"}
                          </button>
                        </div>
                      </article>
                    )) : <p className="skill-group-empty">暂无{group.title}</p>}
                  </section>
                )) : <div className="paper-empty"><ClipboardList size={30} /><span>尚无已安装 Skill</span></div>}
              </section>
            </div>
          </div>
        </section>
      )}

      {draftOpen && (
        <section className="learning-review-backdrop" role="presentation">
          <div className="draft-dialog" role="dialog" aria-modal="true" aria-labelledby="draft-title">
            <div className="learning-review-head">
              <div>
                <span>Local Drafts</span>
                <h2 id="draft-title">{editingScheduleArtifactId ? "修改日程" : "邮件与日程草稿"}</h2>
              </div>
              <button type="button" title="关闭" onClick={() => setDraftOpen(false)}>
                <X size={17} />
              </button>
            </div>

            <div className="draft-builder-body">
              <section className="draft-contact-pane" aria-label="本地联系人">
                <div>
                  <h3>本地联系人</h3>
                  <p>仅保存在当前 PC 的本地工作区中，不同步为员工目录；保存后可在各项目的邮件或日程草稿中复用。</p>
                </div>
                <button
                  type="button"
                  disabled={draftBusy || locked || (draftKind === "email" && !activeProjectId)}
                  onClick={() => { setContactMessage(""); setContactDialogOpen(true); }}
                >
                  <UserRoundPlus size={15} />
                  添加本地联系人
                </button>
                {contacts.length ? (
                  <div className="contact-list" aria-label="已保存联系人">
                    {contacts.map((contact) => (
                      <div key={contact.contact_id} className="contact-card-shell">
                        <button
                          type="button"
                          className="contact-card"
                          disabled={draftBusy}
                          onClick={() => {
                            setEmailRecipient(contact.display_name);
                            setScheduleParticipant(contact.display_name);
                            setDraftMessage(`已选择联系人：${contact.display_name} <${contact.email}>`);
                          }}
                        >
                          <strong>{contact.display_name}</strong>
                          <span>{contact.email}</span>
                          {contact.aliases.length > 0 && <small>别名：{contact.aliases.join("、")}</small>}
                        </button>
                        <button type="button" className="contact-delete-button" title={`删除联系人：${contact.display_name}`} aria-label={`删除联系人：${contact.display_name}`} disabled={draftBusy} onClick={() => void deleteLocalContact(contact)}>
                          <Trash2 size={13} />
                        </button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="contact-empty">暂无已保存联系人。点击上方按钮添加第一个联系人。</p>
                )}
              </section>

              <section className="draft-composer">
                <div className="draft-kind-tabs" aria-label="草稿类型">
                  <button type="button" className={draftKind === "email" ? "active" : ""} onClick={() => selectDraftKind("email")}>
                    <Mail size={15} />
                    邮件
                  </button>
                  <button type="button" className={draftKind === "schedule" ? "active" : ""} onClick={() => selectDraftKind("schedule")}>
                    <CalendarDays size={15} />
                    日程
                  </button>
                </div>

                {draftKind === "email" ? (
                  <form className="draft-form" onSubmit={(event) => { event.preventDefault(); void createCommunicationDraft(); }}>
                    <div className="draft-context">
                      <div className="smtp-sender-picker">
                        <span>发送方</span>
                        <div className="smtp-sender-options" role="listbox" aria-label="选择发送方">
                          {smtpAccounts.map((account) => (
                            <div key={account.sender} className="smtp-sender-option">
                              <button type="button" className={smtpSender === account.sender ? "active" : ""} onClick={() => selectSmtpAccount(account.sender)}>
                                <Mail size={14} />
                                <span>{account.sender}</span>
                                {smtpDefaultSender === account.sender && <small>默认</small>}
                              </button>
                              <button type="button" className="smtp-delete-sender" title={`删除发件方：${account.sender}`} aria-label={`删除发件方：${account.sender}`} onClick={() => deleteSmtpAccount(account)}>
                                <Trash2 size={12} />
                              </button>
                            </div>
                          ))}
                          <button type="button" className="smtp-add-sender" onClick={() => selectSmtpAccount("__new__")}>
                            <Plus size={14} />
                            添加发送方
                          </button>
                        </div>
                      </div>
                      {smtpConfigOpen ? (
                        <div className="smtp-config-panel">
                          <label>
                            <span>发件服务</span>
                            <select value={smtpProvider} onChange={(event) => setSmtpPreference((current) => ({ ...current, provider: event.target.value as "qq" | "netease163" }))}>
                              <option value="qq">QQ 邮箱</option>
                              <option value="netease163">网易 163 邮箱</option>
                            </select>
                          </label>
                          <label>
                            <span>发件邮箱</span>
                            <input type="email" value={smtpSender} maxLength={320} onChange={(event) => setSmtpPreference((current) => ({ ...current, sender: event.target.value }))} placeholder="your-email@example.com" />
                          </label>
                          <label>
                            <span>SMTP 授权码（只配置时填写）</span>
                            <input type="password" value={smtpAuthorizationCode} maxLength={128} onChange={(event) => {
                              const value = event.target.value;
                              setSmtpAuthorizationCode(value);
                              if (value.trim()) {
                                setSmtpConfigured("unconfigured");
                                setSmtpStatusMessage("授权码已输入，请点击“保存并核验 SMTP 配置”完成设置。");
                              }
                            }} placeholder="在邮箱安全设置中生成" autoComplete="new-password" />
                          </label>
                          <button className="smtp-save-button" type="button" disabled={draftBusy || !smtpSender.trim() || !smtpAuthorizationCode.trim()} onClick={() => void configureSmtp()}>保存并核验 SMTP 配置</button>
                          <p className="draft-context">{smtpStatusMessage || "尚未读取本机 SMTP 配置。"}</p>
                        </div>
                      ) : (
                        <div className="smtp-account-status">
                          <span>{smtpSender ? `当前发送方：${smtpSender}` : "尚未选择发送方"}</span>
                          <button type="button" disabled={!smtpSender || smtpDefaultSender === smtpSender} onClick={() => setSmtpDefaultSender(smtpSender)}>设为默认</button>
                        </div>
                      )}
                    </div>
                    <label>
                      <span>收件人（姓名或别名）</span>
                      <input value={emailRecipient} maxLength={240} onChange={(event) => setEmailRecipient(event.target.value)} placeholder="先保存本地联系人" required />
                    </label>
                    <label>
                      <span>主题</span>
                      <input value={emailSubject} maxLength={240} onChange={(event) => setEmailSubject(event.target.value)} placeholder="实验评审材料" required />
                    </label>
                    <label>
                      <span>正文</span>
                      <textarea value={emailBody} maxLength={12000} onChange={(event) => setEmailBody(event.target.value)} placeholder="请说明沟通事项和所需反馈。" required />
                    </label>
                    <div className="email-attachments">
                      <strong>附件</strong>
                      <input
                        type="file"
                        multiple
                        disabled={draftBusy || emailAttachmentBusy}
                        onChange={(event) => {
                          const files = event.currentTarget.files;
                          if (files?.length) void uploadEmailAttachments(files);
                          event.currentTarget.value = "";
                        }}
                      />
                      {emailAttachmentRefs.length ? (
                        <div className="email-attachment-list">
                          {emailAttachmentRefs.map((name) => (
                            <button key={name} type="button" className="email-attachment-chip" onClick={() => removeEmailAttachment(name)}>
                              {name}
                              <X size={12} />
                            </button>
                          ))}
                        </div>
                      ) : (
                        <span>在此直接上传邮件附件</span>
                      )}
                    </div>
                    <button type="submit" disabled={draftBusy || locked || !activeProjectId || !emailRecipient.trim() || !emailSubject.trim() || !emailBody.trim()}>
                      <Mail size={15} />
                      创建邮件草稿
                    </button>
                  </form>
                ) : (
                  <form className="draft-form" onSubmit={(event) => { event.preventDefault(); void createCommunicationDraft(); }}>
                    <label>
                      <span>日程标题</span>
                      <input value={scheduleTitle} maxLength={240} onChange={(event) => setScheduleTitle(event.target.value)} placeholder="项目评审会" required />
                    </label>
                    <div className="draft-form-row">
                      <label>
                        <span>开始时间（创建时设置）</span>
                        <input type="datetime-local" value={scheduleStartAt} onChange={(event) => setScheduleStartAt(event.target.value)} required />
                      </label>
                      <label>
                        <span>时长（分钟）</span>
                        <input type="number" min={5} max={1440} value={scheduleDuration} onChange={(event) => setScheduleDuration(Math.max(5, Math.min(1440, Number(event.target.value) || 60)))} required />
                      </label>
                    </div>
                    <div className="schedule-sound-setting">
                      <button
                        type="button"
                        className={`schedule-sound-toggle ${scheduleSoundEnabled ? "enabled" : ""}`}
                        aria-pressed={scheduleSoundEnabled}
                        disabled={draftBusy}
                        title="提前 10 分钟播放声音提醒"
                        onClick={() => setScheduleSoundEnabled((current) => !current)}
                      >
                        {scheduleSoundEnabled ? <Volume2 size={15} /> : <VolumeX size={15} />}
                        提前 10 分钟声音提醒：{scheduleSoundEnabled ? "开启" : "关闭"}
                      </button>
                    </div>
                    <label>
                      <span>参与人（可选，姓名或别名）</span>
                      <input value={scheduleParticipant} maxLength={240} onChange={(event) => setScheduleParticipant(event.target.value)} placeholder="先保存本地联系人" />
                    </label>
                    <label>
                      <span>说明（可选）</span>
                      <textarea value={scheduleNotes} maxLength={12000} onChange={(event) => setScheduleNotes(event.target.value)} placeholder="议程、地点或准备事项。" />
                    </label>
                    <button type="submit" disabled={draftBusy || locked || !scheduleTitle.trim() || !scheduleStartAt}>
                      <CalendarDays size={15} />
                      {editingScheduleArtifactId ? "保存日程修改" : "创建日程草稿"}
                    </button>
                  </form>
                )}
              </section>
            </div>

            <div className="draft-review">
              <div className={`draft-state ${draftArtifact?.confirmation_status || "idle"}`}>
                <strong>{draftArtifact?.confirmation_status?.toUpperCase() || "READY"}</strong>
                <span>{draftMessage || "仅创建本地草稿，确认不会发送邮件或写入日历。"}</span>
              </div>
              {draftArtifact && (
                <div className="draft-artifact-preview">
                  <span>{draftArtifact.artifact_type === "email_draft" ? "EMAIL DRAFT" : "SCHEDULE DRAFT"}</span>
                  <strong>{draftArtifact.plan.subject || draftArtifact.plan.title}</strong>
                  {draftArtifact.plan.recipient && <p>收件人：{draftArtifact.plan.recipient.display_name} &lt;{draftArtifact.plan.recipient.email}&gt;</p>}
                  {draftArtifact.plan.participant && <p>参与人：{draftArtifact.plan.participant.display_name} &lt;{draftArtifact.plan.participant.email}&gt;</p>}
                  {draftArtifact.plan.start_at && <p>开始时间（创建时设置）：{formatScheduleStartTime(draftArtifact.plan.start_at)} · {draftArtifact.plan.duration_minutes} 分钟</p>}
                </div>
              )}
              {draftArtifact?.confirmation_status === "draft" && (
                <div className="draft-confirm-actions">
                  <button type="button" disabled={draftBusy} onClick={() => void setDraftConfirmation("cancelled")}>取消草稿</button>
                  <button type="button" disabled={draftBusy} onClick={() => void setDraftConfirmation("confirmed")}>
                    <Check size={15} />
                    确认并冻结草稿
                  </button>
                </div>
              )}
              {draftArtifact?.artifact_type === "email_draft" && draftArtifact.confirmation_status === "confirmed" && (
                <div className="draft-confirm-actions">
                  <button type="button" disabled={draftBusy || !smtpSender.trim() || smtpConfigured !== "configured"} onClick={() => void sendConfirmedEmail()}>
                    <Mail size={15} />
                    {smtpConfigured === "configured" ? "发送已确认邮件" : "先完成 SMTP 配置"}
                  </button>
                </div>
              )}
              {draftArtifact?.artifact_type === "schedule_draft" && draftArtifact.confirmation_status === "confirmed" && (
                <div className="draft-confirm-actions">
                  <button type="button" disabled={draftBusy} onClick={() => void exportConfirmedSchedule()}>
                    <CalendarDays size={15} />
                    导出日程 .ics
                  </button>
                  {scheduleExportPath && (
                    <button type="button" disabled={draftBusy} onClick={() => void openExportedScheduleFile()}>
                      查找导出文件
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
          {contactDialogOpen && (
            <div className="contact-modal-backdrop" role="presentation">
              <form className="contact-modal" role="dialog" aria-modal="true" aria-labelledby="contact-modal-title" onSubmit={(event) => { event.preventDefault(); void createLocalContact(); }}>
                <div className="contact-modal-head">
                  <div><span>Local Contacts</span><h2 id="contact-modal-title">添加本地联系人</h2></div>
                  <button type="button" title="关闭" onClick={() => setContactDialogOpen(false)}><X size={17} /></button>
                </div>
                <label>
                  <span>姓名</span>
                  <input autoFocus value={contactName} maxLength={120} onChange={(event) => setContactName(event.target.value)} placeholder="李老师" required />
                </label>
                <label>
                  <span>邮箱</span>
                  <input type="email" value={contactEmail} maxLength={320} onChange={(event) => setContactEmail(event.target.value)} placeholder="name@example.com" required />
                </label>
                <label>
                  <span>别名（可选，以逗号分隔）</span>
                  <input value={contactAliases} maxLength={600} onChange={(event) => setContactAliases(event.target.value)} placeholder="导师，李工" />
                </label>
                {contactMessage && <p className="contact-modal-message">{contactMessage}</p>}
                <div className="contact-modal-actions">
                  <button type="button" onClick={() => setContactDialogOpen(false)}>取消</button>
                  <button type="submit" disabled={draftBusy || locked || !activeProjectId}>保存联系人</button>
                </div>
              </form>
            </div>
          )}
        </section>
      )}
      {scheduleHistoryOpen && (
        <div className="schedule-history-backdrop" role="presentation" onMouseDown={() => setScheduleHistoryOpen(false)}>
          <section className="schedule-history-dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-history-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="schedule-history-head">
              <div>
                <span>Schedule History</span>
                <h2 id="schedule-history-title">近 7 天日程</h2>
                <small>按创建时间保留最近 7 天；到期和已取消记录仍可查看。</small>
              </div>
              <button type="button" className="layout-icon-button" title="关闭日程历史" onClick={() => setScheduleHistoryOpen(false)}>
                <X size={17} />
              </button>
            </header>
            {scheduleHistoryMessage && <div className="schedule-history-message" role="status">{scheduleHistoryMessage}</div>}
            <div className="schedule-history-body">
              <div className="schedule-history-list" aria-label="近 7 天日程列表">
                {scheduleHistory.length ? scheduleHistory.map((artifact) => (
                  <button
                    key={artifact.artifact_id}
                    type="button"
                    className={`schedule-history-item ${selectedScheduleHistoryArtifactId === artifact.artifact_id ? "selected" : ""}`}
                    onClick={() => setSelectedScheduleHistoryArtifactId(artifact.artifact_id)}
                  >
                    <strong>{artifact.plan?.title || "未命名日程"}</strong>
                    <span>{formatScheduleStartTime(artifact.plan?.start_at)}</span>
                    <small>{formatScheduleCreatedAt(artifact.created_at)}</small>
                    <em>{artifact.confirmation_status === "confirmed" ? "已确认" : artifact.confirmation_status === "cancelled" ? "已取消" : "草稿"}</em>
                  </button>
                )) : <div className="schedule-empty">近 7 天暂无已创建日程</div>}
              </div>
              <article className="schedule-history-detail" aria-label="日程历史详情">
                {selectedScheduleHistoryArtifact ? (
                  <>
                    <div className="schedule-history-detail-head">
                      <div>
                        <span>历史详情</span>
                        <h3>{selectedScheduleHistoryArtifact.plan?.title || "未命名日程"}</h3>
                      </div>
                      <em>{selectedScheduleHistoryArtifact.confirmation_status === "confirmed" ? "已确认" : selectedScheduleHistoryArtifact.confirmation_status === "cancelled" ? "已取消" : "草稿"}</em>
                    </div>
                    <dl>
                      <div><dt>创建时间</dt><dd>{formatScheduleCreatedAt(selectedScheduleHistoryArtifact.created_at)}</dd></div>
                      <div><dt>开始时间</dt><dd>{formatScheduleStartTime(selectedScheduleHistoryArtifact.plan?.start_at)}</dd></div>
                      <div><dt>时长</dt><dd>{selectedScheduleHistoryArtifact.plan?.duration_minutes ? `${selectedScheduleHistoryArtifact.plan.duration_minutes} 分钟` : "未设置"}</dd></div>
                      <div><dt>参与人</dt><dd>{selectedScheduleHistoryArtifact.plan?.participant?.display_name ? `${selectedScheduleHistoryArtifact.plan.participant.display_name} <${selectedScheduleHistoryArtifact.plan.participant.email}>` : "未填写"}</dd></div>
                    </dl>
                    <p>{selectedScheduleHistoryArtifact.plan?.notes || selectedScheduleHistoryArtifact.change_summary || "暂无说明"}</p>
                  </>
                ) : <div className="schedule-empty">点击左侧日程查看详细信息</div>}
              </article>
            </div>
          </section>
        </div>
      )}
      {scheduleReminderOpen && scheduleReminderArtifact && (
        <div className="schedule-reminder-backdrop" role="presentation">
          <div className="schedule-reminder-dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-reminder-title">
            <div className="schedule-reminder-head">
              <div>
                <span>{scheduleReminderPhase === "started" ? "Schedule Started" : "Schedule Reminder"}</span>
                <h2 id="schedule-reminder-title">{scheduleReminderPhase === "started" ? "日程已开始" : "日程提醒"}</h2>
              </div>
              <button type="button" title="关闭提醒" aria-label="关闭提醒" onClick={dismissScheduleReminder}>
                <X size={17} />
              </button>
            </div>
            <div className="schedule-reminder-body">
              <strong>{scheduleReminderPhase === "started" ? "日程已开始，请按计划开始这一项安排" : "还有 10 分钟就要开始这一项日程了"}</strong>
              <h3>{scheduleReminderTitle === "日程" ? "未命名日程" : scheduleReminderTitle}</h3>
              <p>开始时间（创建时设置）：{formatScheduleStartTime(scheduleReminderArtifact.plan?.start_at)}</p>
            </div>
            <div className="schedule-reminder-actions">
              <button type="button" onClick={dismissScheduleReminder}>知道了</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function formatEvidenceNumber(value: number | null | undefined, unit?: string): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const normalized = Number(value.toFixed(6)).toString();
  return `${normalized}${unit || ""}`;
}

function formatEvidenceSignedNumber(value: number | null | undefined, unit?: string): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const normalized = Number(value.toFixed(6)).toString();
  return `${value > 0 ? "+" : ""}${normalized}${unit || ""}`;
}

function formatEvidenceSource(source?: { file_ref?: string; source_locator?: string }): string {
  if (!source) return "未记录";
  const parts = [source.file_ref, source.source_locator].filter((value): value is string => Boolean(value));
  return parts.length ? parts.join(" / ") : "未记录";
}

function EvidencePanel({ evidence }: { evidence: EvidenceItem[] }) {
  if (!evidence.length) {
    return (
      <div className="studio-empty">
        <ClipboardList size={42} />
        <h3>等待证据链</h3>
        <p>完成一次知识问答后，这里会显示检索片段、来源文件和检索排序分。</p>
      </div>
    );
  }

  return (
    <div className="evidence-list">
      {evidence.map((item) => {
        const isVisual = item.modality === "image" || item.evidence_type === "visual_perception";
        const isAudio = item.modality === "audio" || item.evidence_type === "audio_transcript";
        const isExperiment = item.modality === "experiment" || item.evidence_type === "experiment_measurement";
        return (
        <section key={item.id} className={`evidence-card ${isVisual ? "visual-evidence-card" : isAudio ? "audio-evidence-card" : isExperiment ? "experiment-evidence-card" : ""}`}>
          <div className="evidence-head">
            <strong>{item.id}</strong>
            <span
              title={isVisual ? "视觉置信度来自本轮 OCR/VLM 感知，不是项目文献排序分。" : isAudio ? "语音置信度来自本轮转写；行动项仍需以用户确认结果为准。" : isExperiment ? "该证据来自显式选择的实验运行，并已通过可比性门；不代表因果已被证明。" : "检索排序分只用于排列本轮候选证据，不是准确率、置信度或概率；分数越高表示越靠前。"}
            >
              {isVisual
                ? `视觉置信度 ${typeof item.confidence === "number" ? item.confidence.toFixed(3) : typeof item.score === "number" ? item.score.toFixed(3) : "未知"}`
                : isAudio
                  ? `语音置信度 ${typeof item.confidence === "number" ? item.confidence.toFixed(3) : "未知"}`
                : isExperiment
                  ? "可比性门通过 · 结构化测量"
                : `排序分 ${typeof item.score === "number" ? item.score.toFixed(4) : "未知"}${typeof item.support_score === "number" ? ` · 支持度 ${item.support_score.toFixed(3)}` : ""}`}
            </span>
          </div>
          <h3>{isVisual ? "本轮授权视觉证据" : isAudio ? "本轮确认语音/转写" : isExperiment ? (item.document_title || "结构化实验测量") : item.source_filename}</h3>
          <div className="evidence-meta">
            {isVisual ? (
              <>
                <span>{item.section || "当前图片"}</span>
                {item.bbox && <span>区域 x{item.bbox.x.toFixed(3)} y{item.bbox.y.toFixed(3)} w{item.bbox.width.toFixed(3)} h{item.bbox.height.toFixed(3)}</span>}
              </>
            ) : isAudio ? (
              <span>{item.section || "本轮语音转写"}</span>
            ) : isExperiment ? (
              <>
                <span>{item.metric || item.section || "未标注指标"}</span>
                <span>基线 {formatEvidenceNumber(item.baseline_value, item.unit)} → 当前 {formatEvidenceNumber(item.current_value, item.unit)}</span>
                <span>绝对变化 {formatEvidenceSignedNumber(item.absolute_delta, item.absolute_delta_unit ?? item.unit)}{typeof item.relative_delta === "number" ? `（相对 ${formatEvidenceSignedNumber(item.relative_delta * 100, "%")}）` : ""}</span>
                {item.split && <span>split {item.split}</span>}
                {item.definition_version && <span>定义 {item.definition_version}</span>}
              </>
            ) : (
              <>
                <span>页码 {item.page_label || "未知"}</span>
                <span>切片 {item.chunk_index ?? "未知"}</span>
                <span>{item.section || "未标注章节"}</span>
              </>
            )}
          </div>
          {!isVisual && !isAudio && item.support_summary && (
            <div className="evidence-support" title="支持度是基于问题与证据文本重叠的可解释启发式，不是概率或准确率。">
              <span>问题相关摘录</span>
              <p>{item.support_summary}</p>
            </div>
          )}
          {isExperiment && (item.source_filenames?.length || item.baseline_source || item.current_source) ? (
            <div className="evidence-source-locations">
              <span>两侧源定位</span>
              <p>
                基线：{formatEvidenceSource(item.baseline_source)}；当前：{formatEvidenceSource(item.current_source)}
              </p>
            </div>
          ) : null}
          {!isVisual && !isAudio && item.visual_assets?.length ? (
            <div className="evidence-visual-assets">
              <span>关联图像/图表资产</span>
              {item.visual_assets.slice(0, 4).map((asset) => (
                <p key={`${item.id}-${asset.asset_id || asset.source_locator || asset.caption}`}>
                  {asset.caption}
                  {asset.page ? ` · 第 ${asset.page} 页` : ""}
                  {asset.source_locator ? ` · ${asset.source_locator}` : ""}
                  {asset.vision_status === "pending_local_model" ? " · 等待视觉解析" : ""}
                </p>
              ))}
            </div>
          ) : null}
          <p>{item.snippet || "无片段摘要"}</p>
        </section>
        );
      })}
    </div>
  );
}

function GraphPanel({
  latestMindmap,
  mindmapLayout,
  toggleMindmapNode,
}: {
  latestMindmap: MindmapArtifact | null;
  mindmapLayout: MindmapLayout | null;
  toggleMindmapNode: (nodeId: string) => void;
}) {
  const [detailNode, setDetailNode] = useState<PositionedNode | null>(null);

  if (!latestMindmap || !mindmapLayout) {
    return (
      <div className="studio-empty">
          <Network size={44} />
          <h3>等待本轮回答导图</h3>
          <p>本轮回答尚未生成结构化导图。</p>
      </div>
    );
  }

  const mapFitScale = Math.min(graphFitScale(mindmapLayout), 0.54);

  return (
    <div className="map-shell">
      <div className="map-title">
        <Network size={16} />
        <strong>{latestMindmap.title}</strong>
      </div>
      <TransformWrapper
        key={`${latestMindmap.raw.length}-${mindmapLayout.width}-${mindmapLayout.height}`}
        initialScale={mapFitScale}
        minScale={0.18}
        maxScale={2.4}
        centerOnInit
        centerZoomedOut
        limitToBounds
        smooth
        wheel={{ step: GRAPH_WHEEL_STEP }}
        doubleClick={{ disabled: true }}
      >
        {({ zoomIn, zoomOut, centerView }) => (
          <>
            <div className="map-tools">
              <button type="button" onClick={() => zoomIn(GRAPH_BUTTON_ZOOM_STEP, 160)} title="放大">
                <ZoomIn size={16} />
              </button>
              <button type="button" onClick={() => zoomOut(GRAPH_BUTTON_ZOOM_STEP, 160)} title="缩小">
                <ZoomOut size={16} />
              </button>
              <button type="button" onClick={() => centerView(mapFitScale, 180)} title="居中">
                <LocateFixed size={16} />
              </button>
            </div>
            <TransformComponent
              wrapperStyle={{ width: "100%", height: "100%" }}
              contentStyle={{ width: `${mindmapLayout.width}px`, height: `${mindmapLayout.height}px` }}
            >
              <svg
                className="mindmap-svg"
                width={mindmapLayout.width}
                height={mindmapLayout.height}
                viewBox={`0 0 ${mindmapLayout.width} ${mindmapLayout.height}`}
              >
                <g className="edge-layer">
                  {mindmapLayout.edges.map((edge) => (
                    <path key={`${edge.from.id}-${edge.to.id}`} d={edgePath(edge)} />
                  ))}
                </g>
                <g className="node-layer">
                  {mindmapLayout.nodes.map((node) => (
                    <g
                      key={node.id}
                      className={`map-node ${node.hasChildren ? "has-children" : ""}`}
                      transform={`translate(${node.x + CANVAS_PADDING}, ${node.y + CANVAS_PADDING})`}
                      onClick={() => node.hasChildren && toggleMindmapNode(node.id)}
                    >
                      <rect width={NODE_WIDTH} height={NODE_HEIGHT} rx="6" />
                      <foreignObject x="0" y="0" width={NODE_WIDTH} height={NODE_HEIGHT}>
                        <div className="map-node-content">
                          <span className="node-level">L{node.depth + 1}</span>
                          <span className="node-title">{node.title}</span>
                          <span className="node-actions">
                            <button
                              type="button"
                              className="node-detail-button"
                              title="查看完整内容"
                              aria-label={`查看 ${node.title} 的完整内容`}
                              onClick={(event) => {
                                event.stopPropagation();
                                setDetailNode(node);
                              }}
                            >
                              <FileText size={11} />
                            </button>
                            {node.hasChildren && <span className="node-count">{node.collapsed ? `+${node.children.length}` : "-"}</span>}
                          </span>
                        </div>
                      </foreignObject>
                    </g>
                  ))}
                </g>
              </svg>
            </TransformComponent>
          </>
        )}
      </TransformWrapper>
        {detailNode && (
        <div className="map-detail-backdrop" role="presentation" onClick={() => setDetailNode(null)}>
          <section
            className="map-detail-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="map-detail-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="map-detail-head">
              <div>
                <span>L{detailNode.depth + 1}</span>
                <h3 id="map-detail-title">节点完整内容</h3>
              </div>
              <button type="button" onClick={() => setDetailNode(null)} title="关闭">
                <X size={17} />
              </button>
            </div>
            <div className="map-detail-meta">
              <span>{detailNode.hasChildren ? `${detailNode.children.length} 个子节点` : "末级节点"}</span>
              {detailNode.collapsed && <span>已折叠</span>}
            </div>
            <p>{formatMindmapBranch(detailNode)}</p>
          </section>
        </div>
        )}
    </div>
  );
}

function AuditPanel({ events }: { events: AuditEvent[] }) {
  if (!events.length) {
    return (
      <div className="studio-empty">
        <ShieldCheck size={42} />
        <h3>等待审计事件</h3>
        <p>运行态解锁、检索和回答完成后，这里会显示最近的 hash chain 事件。</p>
      </div>
    );
  }

  return (
    <div className="audit-list">
      {events.slice().reverse().map((event) => (
        <section key={event.event_id} className="audit-card">
          <div>
            <strong>{event.event_type}</strong>
            <span>{formatAuditTime(event.timestamp)}</span>
          </div>
          <code>{event.event_hash?.slice(0, 18) || "NO_HASH"}</code>
          <p>{event.trace_id}</p>
        </section>
      ))}
    </div>
  );
}
