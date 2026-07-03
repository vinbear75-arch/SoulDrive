import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { invoke } from "@tauri-apps/api/core";
import { TransformComponent, TransformWrapper } from "react-zoom-pan-pinch";
import {
  Activity,
  BrainCircuit,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  FileText,
  GitBranch,
  KeyRound,
  Layers3,
  LocateFixed,
  Network,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Upload,
  Usb,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./App.css";
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

interface Message {
  role: "user" | "ai";
  content: string;
}

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

interface EvidenceItem {
  id: string;
  source_filename: string;
  page_label?: string;
  chunk_index?: number | null;
  section?: string | null;
  score?: number;
  snippet?: string;
  breakdown?: Record<string, number>;
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

interface RuntimeMetrics {
  uptime_seconds: number;
  total_requests: number;
  failed_requests: number;
  chat_requests: number;
  chat_failures: number;
  average_latency_ms: number;
  last_error?: string | null;
}

interface DocumentItem {
  name: string;
  relative_path: string;
  size_bytes: number;
  modified_at: number;
  indexed: boolean;
}

interface DocumentLibrary {
  ready: boolean;
  document_count: number;
  indexed_count: number;
  workspace: string;
  documents: DocumentItem[];
}

interface DocumentImportResponse {
  imported_count: number;
  items: Array<{
    name: string;
    status: "imported" | "already_present" | "rejected";
    error_code?: string;
  }>;
}

interface SecurityStatus {
  crypto_initialized: boolean;
  software_unlocked: boolean;
  hardware_mounted: boolean;
  reason?: string;
  no_recovery: boolean;
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
      };
      reranker?: {
        selected?: string | null;
        mode?: string;
      };
      config?: {
        n_gpu_layers?: number;
        gpu_mode?: string;
        gpu_device_name?: string | null;
        gpu_reason?: string;
      };
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

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const MINDMAP_BLOCK_PATTERN = /```souldrive-mindmap\s*([\s\S]*?)```/g;
const EVIDENCE_BLOCK_PATTERN = /```souldrive-evidence\s*([\s\S]*?)```/g;
const GRAPH_WHEEL_STEP = 0.006;
const GRAPH_BUTTON_ZOOM_STEP = 0.12;
const PAPERS_PER_PAGE = 5;
const WORKBENCH_LAYOUT_STORAGE_KEY = "souldrive.workbench.layout.v1";
const DEFAULT_ARTIFACT_WEIGHT = 1.25;
const MIN_ARTIFACT_WEIGHT = 0.72;
const MAX_ARTIFACT_WEIGHT = 2.8;
const GRAPH_PRIORITY_ARTIFACT_WEIGHT = 2.25;

interface WorkbenchLayoutPreference {
  leftCollapsed: boolean;
  artifactWeight: number;
}

const promptTemplates = [
  "请归纳这批论文或技术资料的核心主题、关键结论和差异，并给出引用依据。",
  "请对比近三份技术文档、研究论文或项目方案的目标、实现路径、约束和风险。",
  "请生成一个围绕当前资料库的技术路线图，并标注关键来源。",
  "请从企业落地角度评估这些资料对应方案的可复用性、成本和实施风险。",
];

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

function parseEvidence(raw: string): EvidenceItem[] {
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function parseMessageArtifacts(content: string): ParsedMessage {
  const artifacts = Array.from(content.matchAll(MINDMAP_BLOCK_PATTERN)).map((match) =>
    parseMindmap(match[1].trim()),
  );
  const evidence = Array.from(content.matchAll(EVIDENCE_BLOCK_PATTERN)).flatMap((match) =>
    parseEvidence(match[1].trim()),
  );

  return {
    cleanContent: content
      .replace(MINDMAP_BLOCK_PATTERN, "")
      .replace(EVIDENCE_BLOCK_PATTERN, "")
      .trim(),
    artifacts,
    evidence,
  };
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
  const [runtimeMetrics, setRuntimeMetrics] = useState<RuntimeMetrics | null>(null);
  const [productDiagnostics, setProductDiagnostics] = useState<ProductDiagnostics | null>(null);
  const [documentLibrary, setDocumentLibrary] = useState<DocumentLibrary | null>(null);
  const [securityStatus, setSecurityStatus] = useState<SecurityStatus | null>(null);
  const [passphrase, setPassphrase] = useState("");
  const [confirmPassphrase, setConfirmPassphrase] = useState("");
  const [acknowledgeNoRecovery, setAcknowledgeNoRecovery] = useState(false);
  const [securityMessage, setSecurityMessage] = useState("");
  const [securityBusy, setSecurityBusy] = useState(false);
  const [documentPage, setDocumentPage] = useState(1);
  const [apiToken, setApiToken] = useState<string | null>(null);
  const [apiBaseUrl, setApiBaseUrl] = useState(DEFAULT_API_BASE_URL);
  const [runtimeConfigLoaded, setRuntimeConfigLoaded] = useState(false);
  const [indexStartStatus, setIndexStartStatus] = useState<"idle" | "starting" | "error">("idle");
  const [documentImportStatus, setDocumentImportStatus] = useState<"idle" | "selecting" | "importing" | "error">("idle");
  const [documentImportMessage, setDocumentImportMessage] = useState("");
  const [artifactTab, setArtifactTab] = useState<"evidence" | "graph" | "audit">("evidence");
  const [collapsedNodeIds, setCollapsedNodeIds] = useState<Set<string>>(new Set());
  const [workbenchLayout, setWorkbenchLayout] = useState<WorkbenchLayoutPreference>(() => readWorkbenchLayoutPreference());
  const [isWorkbenchDragging, setIsWorkbenchDragging] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const workbenchRef = useRef<HTMLElement | null>(null);

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
  const totalDocumentPages = Math.max(1, Math.ceil(documents.length / PAPERS_PER_PAGE));
  const activeDocumentPage = Math.min(documentPage, totalDocumentPages);
  const visibleDocuments = useMemo(() => {
    const start = (activeDocumentPage - 1) * PAPERS_PER_PAGE;
    return documents.slice(start, start + PAPERS_PER_PAGE);
  }, [activeDocumentPage, documents]);
  const locked = runtimeStatus?.locked ?? true;
  const indexingStatus = runtimeStatus?.indexing?.status || "idle";
  const indexBusy = ["queued", "scanning", "indexing"].includes(indexingStatus);
  const leftCollapsed = workbenchLayout.leftCollapsed;
  const artifactWeight = workbenchLayout.artifactWeight;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    saveWorkbenchLayoutPreference(workbenchLayout);
  }, [workbenchLayout]);

  useEffect(() => {
    setCollapsedNodeIds(new Set());
  }, [latestMindmap?.raw]);

  useEffect(() => {
    setDocumentPage(1);
  }, [documentLibrary?.document_count]);

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

    let isMounted = true;

    const refreshRuntimeStatus = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/runtime/status`);
        if (!response.ok) throw new Error("runtime status unavailable");
        const data = await response.json();
        if (isMounted) setRuntimeStatus(data);
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

    const refreshSecurityStatus = async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/security/status`, {
          headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
        });
        if (!response.ok) throw new Error("security unavailable");
        const data = await response.json();
        if (isMounted) setSecurityStatus(data);
      } catch {
        if (isMounted) setSecurityStatus(null);
      }
    };

    void refreshRuntimeStatus();
    void refreshAuditEvents();
    void refreshProductHealth();
    void refreshMetrics();
    void refreshProductDiagnostics();
    void refreshDocumentLibrary();
    void refreshSecurityStatus();
    const timer = window.setInterval(() => {
      void refreshRuntimeStatus();
      void refreshAuditEvents();
      void refreshProductHealth();
      void refreshMetrics();
      void refreshProductDiagnostics();
      void refreshDocumentLibrary();
      void refreshSecurityStatus();
    }, 3000);

    return () => {
      isMounted = false;
      window.clearInterval(timer);
    };
  }, [apiBaseUrl, apiToken, runtimeConfigLoaded]);

  useEffect(() => {
    if (latestMindmap) {
      setArtifactTab("graph");
    } else if (latestEvidence.length) {
      setArtifactTab("evidence");
    }
  }, [latestMindmap, latestEvidence.length]);

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
      leftCollapsed: true,
      artifactWeight: GRAPH_PRIORITY_ARTIFACT_WEIGHT,
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

  const submitQuery = async (query: string) => {
    const userQuery = query.trim();
    if (!userQuery || isGenerating) return;

    if (runtimeStatus?.locked) {
      setMessages((previous) => [
        ...previous,
        { role: "user", content: userQuery },
        { role: "ai", content: `本地知识引擎已锁定：${runtimeStatus.reason}` },
      ]);
      setInput("");
      return;
    }

    setInput("");
    setMessages((previous) => [...previous, { role: "user", content: userQuery }, { role: "ai", content: "" }]);
    setIsGenerating(true);

    try {
      const response = await fetch(`${apiBaseUrl}/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiToken ? { "X-SoulDrive-Token": apiToken } : {}),
        },
        body: JSON.stringify({ query: userQuery, top_k: 6 }),
      });

      if (!response.ok) throw new Error("后端拒绝请求");
      if (!response.body) throw new Error("流式响应不可用");

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
      }
    } catch (error) {
      console.error(error);
      setMessages((previous) => {
        const next = [...previous];
        next[next.length - 1].content = "无法连接本地推理服务。请确认 SoulDrive sidecar 已启动。";
        return next;
      });
    } finally {
      setIsGenerating(false);
    }
  };

  const startIndexing = async () => {
    if (locked || indexBusy || indexStartStatus === "starting") return;
    setIndexStartStatus("starting");
    try {
      const response = await fetch(`${apiBaseUrl}/index/run`, {
        method: "POST",
        headers: apiToken ? { "X-SoulDrive-Token": apiToken } : undefined,
      });
      if (!response.ok && response.status !== 409) {
        throw new Error("indexer unavailable");
      }
      setIndexStartStatus("idle");
    } catch {
      setIndexStartStatus("error");
    }
  };

  const importDocuments = async () => {
    if (locked || documentImportStatus === "selecting" || documentImportStatus === "importing") return;
    setDocumentImportStatus("selecting");
    setDocumentImportMessage("");

    try {
      const sourcePaths = await invoke<string[]>("select_pdf_files");
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
        body: JSON.stringify({ source_paths: sourcePaths }),
      });
      if (!response.ok) throw new Error("document import failed");

      const payload = await response.json() as DocumentImportResponse;
      setDocumentImportMessage(`导入 ${payload.imported_count}/${payload.items.length} 份`);
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
    void submitQuery(input);
  };

  const diagnosticSummary = summarizeDiagnostics(productDiagnostics, healthStatus);
  const gpuConfig = productDiagnostics?.models?.runtime?.config;
  const selectedChatModel = productDiagnostics?.models?.runtime?.chat_model?.selected || "未检测";
  const selectedReranker =
    productDiagnostics?.models?.runtime?.reranker?.selected ||
    (productDiagnostics?.models?.runtime?.reranker?.mode === "disabled" ? "未启用" : "未检测");
  const workspaceSecurityMode = securityPanelMode(securityStatus);
  const modelAccelerationLabel =
    typeof gpuConfig?.n_gpu_layers === "number" && gpuConfig.n_gpu_layers > 0
      ? `GPU ${gpuConfig.n_gpu_layers}`
      : "CPU Edge";
  const workbenchStyle = {
    "--left-panel-width": leftCollapsed ? "72px" : "300px",
    "--chat-panel-fr": "1fr",
    "--artifact-panel-fr": `${artifactWeight}fr`,
  } as CSSProperties & Record<string, string>;

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
        className={`workbench ${leftCollapsed ? "left-collapsed" : ""} ${isWorkbenchDragging ? "resizing" : ""}`}
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
              title={indexBusy ? "索引任务运行中" : "更新知识索引"}
              disabled={locked || indexBusy || indexStartStatus === "starting"}
              onClick={() => void startIndexing()}
            >
              <RefreshCw size={16} />
            </button>
            <button
              type="button"
              className="rail-button"
              title="导入 PDF 文档"
              disabled={locked || documentImportStatus === "selecting" || documentImportStatus === "importing"}
              onClick={() => void importDocuments()}
            >
              <Upload size={16} />
            </button>
            <button type="button" className="rail-button" title="导图优先" onClick={useGraphPriorityLayout}>
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
                  <strong>WAITING</strong>
                  <span>正在等待本地安全服务；如长时间未恢复，请更新或重新打包 sidecar。</span>
                </div>
              </div>
            ) : workspaceSecurityMode === "initialize" ? (
              <div className="security-form">
                <p>该口令用于保护本地工作区主密钥。SoulDrive 不会保存口令，也不提供找回能力。忘记口令后，该 U 盘中的加密知识库将无法解锁。</p>
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
                  <span>工作区已通过口令解锁</span>
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

          <div className="panel-block paper-library-block">
            <div className="panel-title">
              <span>
                <FileText size={15} />
                知识文档
              </span>
              <button
                type="button"
                title={indexBusy ? "索引任务运行中" : "更新知识索引"}
                disabled={locked || indexBusy || indexStartStatus === "starting"}
                onClick={() => void startIndexing()}
              >
                <RefreshCw size={15} />
              </button>
              <button
                type="button"
                title="导入 PDF 文档"
                disabled={locked || documentImportStatus === "selecting" || documentImportStatus === "importing"}
                onClick={() => void importDocuments()}
              >
                <Upload size={15} />
              </button>
            </div>
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
              <span>{indexStartStatus === "error" ? "索引启动失败" : indexingStatus.toUpperCase()}</span>
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
            <div className="paper-list">
              {documentLibrary?.documents?.length ? (
                visibleDocuments.map((document) => (
                  <section key={document.relative_path} className="paper-row">
                    <div className="paper-row-main">
                      <FileText size={14} />
                      <strong title={document.name}>{document.name}</strong>
                    </div>
                    <div className="paper-row-meta">
                      <span>{formatBytes(document.size_bytes)}</span>
                      <span>{formatDocumentTime(document.modified_at)}</span>
                      <span className={document.indexed ? "indexed" : ""}>{document.indexed ? "已索引" : "待索引"}</span>
                    </div>
                  </section>
                ))
              ) : (
                <div className="paper-empty">
                  <FileText size={28} />
                  <span>暂无文档</span>
                </div>
              )}
            </div>
            <div className="paper-pagination">
              <button
                type="button"
                title="上一页"
                disabled={activeDocumentPage <= 1}
                onClick={() => setDocumentPage((page) => Math.max(1, page - 1))}
              >
                <ChevronLeft size={15} />
              </button>
              <span>{activeDocumentPage} / {totalDocumentPages}</span>
              <button
                type="button"
                title="下一页"
                disabled={activeDocumentPage >= totalDocumentPages}
                onClick={() => setDocumentPage((page) => Math.min(totalDocumentPages, page + 1))}
              >
                <ChevronRight size={15} />
              </button>
            </div>
          </div>

          <div className="panel-block">
            <div className="panel-title">
              <Search size={15} />
              研究任务模板
            </div>
            <div className="prompt-stack">
              {promptTemplates.map((template) => (
                <button
                  key={template}
                  type="button"
                  disabled={isGenerating || locked}
                  onClick={() => void submitQuery(template)}
                >
                  {template}
                </button>
              ))}
            </div>
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
                <span>Reranker</span>
                <strong>{selectedReranker}</strong>
              </div>
              <div>
                <span>请求数</span>
                <strong>{runtimeMetrics?.total_requests ?? 0}</strong>
              </div>
              <div>
                <span>审计链</span>
                <strong>{productDiagnostics?.audit?.ready ? "INTACT" : "CHECK"}</strong>
              </div>
            </div>
            {runtimeMetrics?.last_error && <p>{runtimeMetrics.last_error}</p>}
          </details>
        </aside>
        )}

        <main className="dialog-panel">
          <div className="dialog-header">
            <div>
              <span>
                <FileText size={14} />
                Local Knowledge Engine
              </span>
              <h2>私有知识问答与分析</h2>
            </div>
            <div className="model-badge">
              <KeyRound size={14} />
              {selectedChatModel}
            </div>
          </div>

          <div className="message-list">
            {messages.length === 0 && (
              <div className="conversation-empty">
                <Usb size={42} />
                <p>插入授权移动存储后，选择研究任务或直接提问。</p>
              </div>
            )}

            {parsedMessages.map(({ message, parsed }, index) => {
              const isUser = message.role === "user";
              const displayContent = parsed.cleanContent || (parsed.evidence.length ? "已生成结构化证据链。" : message.content);

              return (
                <article key={`${message.role}-${index}`} className={`message ${isUser ? "user" : "ai"}`}>
                  <div className="message-meta">{isUser ? "YOU" : "SOULDRIVE"}</div>
                  <div className="message-content">
                    {isUser ? (
                      <p>{message.content}</p>
                    ) : (
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayContent}</ReactMarkdown>
                    )}
                    {isGenerating && index === messages.length - 1 && <span className="stream-caret" />}
                  </div>
                </article>
              );
            })}
            <div ref={messagesEndRef} />
          </div>

          <form className="ask-box" onSubmit={handleSubmit}>
            <input
              value={input}
              onChange={(event) => setInput(event.target.value)}
              disabled={isGenerating || locked}
              placeholder={locked ? "授权移动存储未就绪，知识引擎已锁定" : isGenerating ? "端侧模型生成中..." : "输入文档归纳、对比、方案分析或问答任务"}
            />
            <button type="submit" disabled={!input.trim() || isGenerating || locked} title="发送">
              <Send size={17} />
            </button>
          </form>
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

        <aside className="artifact-studio">
          <div className="studio-header">
            <div>
              <span>
                <Layers3 size={14} />
                Evidence Studio
              </span>
              <h2>证据链与结构导图</h2>
            </div>
            <div className="artifact-tabs">
              <button type="button" title="导图优先布局" onClick={useGraphPriorityLayout}>
                <Layers3 size={14} />
                优先
              </button>
              <button type="button" className={artifactTab === "evidence" ? "active" : ""} onClick={() => setArtifactTab("evidence")}>
                <ClipboardList size={14} />
                证据
              </button>
              <button type="button" className={artifactTab === "graph" ? "active" : ""} onClick={() => setArtifactTab("graph")}>
                <GitBranch size={14} />
                导图
              </button>
              <button type="button" className={artifactTab === "audit" ? "active" : ""} onClick={() => setArtifactTab("audit")}>
                <ShieldCheck size={14} />
                审计
              </button>
            </div>
          </div>

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
        </aside>
      </section>
    </div>
  );
}

function EvidencePanel({ evidence }: { evidence: EvidenceItem[] }) {
  if (!evidence.length) {
    return (
      <div className="studio-empty">
        <ClipboardList size={42} />
        <h3>等待证据链</h3>
        <p>完成一次知识问答后，这里会显示检索片段、来源文件和重排分。</p>
      </div>
    );
  }

  return (
    <div className="evidence-list">
      {evidence.map((item) => (
        <section key={item.id} className="evidence-card">
          <div className="evidence-head">
            <strong>{item.id}</strong>
            <span>{typeof item.score === "number" ? item.score.toFixed(4) : "0.0000"}</span>
          </div>
          <h3>{item.source_filename}</h3>
          <div className="evidence-meta">
            <span>页码 {item.page_label || "未知"}</span>
            <span>切片 {item.chunk_index ?? "未知"}</span>
            <span>{item.section || "未标注章节"}</span>
          </div>
          <p>{item.snippet || "无片段摘要"}</p>
        </section>
      ))}
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
        <h3>等待结构导图</h3>
        <p>当回答包含结构化导图时，这里会渲染可缩放的回答结构。</p>
      </div>
    );
  }

  return (
    <div className="map-shell">
      <div className="map-title">
        <Network size={16} />
        <strong>{latestMindmap.title}</strong>
      </div>
      <TransformWrapper
        key={`${latestMindmap.raw.length}-${mindmapLayout.width}-${mindmapLayout.height}`}
        initialScale={graphFitScale(mindmapLayout)}
        minScale={0.18}
        maxScale={2.4}
        centerOnInit
        centerZoomedOut
        limitToBounds={false}
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
              <button type="button" onClick={() => centerView(graphFitScale(mindmapLayout), 180)} title="居中">
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
