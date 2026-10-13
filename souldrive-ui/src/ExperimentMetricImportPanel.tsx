import { useEffect, useRef, useState } from "react";
import {
  emptyMetricFields, isProjectMetricSource, metricConfirmationRequest, metricPreviewRequest,
  type MetricFields, type MetricImportResult, type MetricPreview, type MetricSource,
} from "./experimentImport";

const metadataFields: Array<[keyof MetricFields, string, number]> = [
  ["performed_at", "实验时间（含时区）", 64], ["dataset_version", "数据集版本", 160],
  ["config_ref", "配置引用", 260], ["evaluation_script_ref", "评测脚本版本", 260],
  ["environment_ref", "环境引用", 260], ["unit", "指标单位", 64],
  ["direction", "指标方向", 32], ["definition_version", "指标定义版本", 128], ["split", "数据划分", 64],
];

interface Props {
  projectId: string;
  documents: MetricSource[];
  apiBaseUrl: string;
  apiToken: string | null;
  locked: boolean;
  onImported: () => void;
  onOpenDocuments: () => void;
}

export function ExperimentMetricImportPanel({ projectId, documents, apiBaseUrl, apiToken, locked, onImported, onOpenDocuments }: Props) {
  const [sourceId, setSourceId] = useState("");
  const [fields, setFields] = useState<MetricFields>({ ...emptyMetricFields });
  const [preview, setPreview] = useState<MetricPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [metadataOpen, setMetadataOpen] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  const sources = documents.filter((document) => isProjectMetricSource(document, projectId));
  const source = sources.find((document) => document.document_id === sourceId);
  const disabled = locked || !apiToken || busy;
  const plan = preview?.result.plan;
  let canConfirm = false;
  if (source && preview && !disabled) {
    try { metricConfirmationRequest(preview, projectId, source, fields); canConfirm = true; } catch { /* No valid preview, no write. */ }
  }

  useEffect(() => {
    requestRef.current?.abort();
    requestRef.current = null;
    setPreview(null);
    setBusy(false);
    setMessage("");
    return () => { requestRef.current?.abort(); requestRef.current = null; };
  }, [projectId, locked, apiBaseUrl, apiToken]);

  const changeField = (field: keyof MetricFields, value: string) => {
    setFields((current) => ({ ...current, [field]: value }));
    setPreview(null);
    setMessage("");
  };

  const submit = async (confirmed: boolean) => {
    if (disabled || requestRef.current || !source || (confirmed && !preview)) return;
    const controller = new AbortController();
    try {
      const request = confirmed
        ? metricConfirmationRequest(preview!, projectId, source, fields)
        : metricPreviewRequest(projectId, source, fields);
      requestRef.current = controller;
      setBusy(true);
      setMessage("");
      if (!confirmed) setPreview(null);
      const response = await fetch(`${apiBaseUrl}/projects/${encodeURIComponent(projectId)}/experiments/metrics`, {
        method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-SoulDrive-Token": apiToken! },
        body: JSON.stringify(request),
      });
      const result = await response.json() as MetricImportResult & { error?: string };
      if (controller.signal.aborted) return;
      if (!response.ok) {
        if (response.status === 409) setPreview(null);
        throw new Error(result.error || `指标请求返回 HTTP ${response.status}`);
      }
      if (confirmed) {
        if (result.status !== "imported" || !result.run_id) throw new Error("返回结果不完整，请刷新运行列表核对。");
        setPreview(null);
        setMessage(`已确认入账：${result.run_id}${result.warning ? ` · ${result.warning}` : ""}`);
        onImported();
      } else {
        if (!result.plan || !Array.isArray(result.plan.rows) || !Array.isArray(result.plan.missing_fields)) {
          throw new Error("预览格式不完整，不能确认入账。");
        }
        setPreview({ projectId, request: { ...request }, result });
        setMetadataOpen(result.plan.missing_fields.length > 0);
        setMessage(result.plan.ready ? "请核对以下实际口径，再明确确认入账。" : "缺失项尚未补齐；当前没有写入运行。");
      }
    } catch (error) {
      if (!controller.signal.aborted) setMessage(`${confirmed ? "尚未确认请求结果，可重试同一预览或刷新运行列表核对。" : "预览未完成。"}${error instanceof Error ? error.message : "请检查本地服务。"}`);
    } finally {
      if (requestRef.current === controller) { requestRef.current = null; setBusy(false); }
    }
  };

  return <section className="experiment-metric-import" aria-label="实验指标导入">
    <strong>预览并确认指标入账</strong>
    <small>只使用当前项目原件。预览不写入；条件留空时只采用文件内的明确值，不猜日期、单位或版本。</small>
    <label>指标来源
      <select aria-label="指标来源文件" value={sourceId} disabled={disabled} onChange={(event) => { setSourceId(event.target.value); setPreview(null); setMessage(""); }}>
        <option value="">请选择已导入的指标文件</option>
        {sources.map((document) => <option key={document.document_id} value={document.document_id}>{document.name} · {document.document_id}</option>)}
      </select>
    </label>
    {!sources.length && <button type="button" onClick={onOpenDocuments} disabled={disabled}>打开项目资料库，先导入指标文件</button>}
    <label>实验名称
      <input aria-label="指标实验名称" value={fields.experiment_name} maxLength={160} disabled={disabled} onChange={(event) => changeField("experiment_name", event.target.value)} />
    </label>
    <details open={metadataOpen} onToggle={(event) => setMetadataOpen(event.currentTarget.open)}>
      <summary>补充文件中缺失的口径</summary>
      {metadataFields.map(([field, label, maxLength]) => <label key={field}>{label}
        {field === "direction"
          ? <select aria-label={label} value={fields[field]} disabled={disabled} onChange={(event) => changeField(field, event.target.value)}>
            <option value="">未提供，不推断</option><option value="higher">越大越好</option><option value="lower">越小越好</option>
          </select>
          : <input aria-label={label} value={fields[field]} maxLength={maxLength} placeholder={field === "performed_at" ? "2026-09-12T10:00:00+08:00" : "留空则仅使用文件中的值"} disabled={disabled} onChange={(event) => changeField(field, event.target.value)} />}
      </label>)}
    </details>
    <button type="button" onClick={() => void submit(false)} disabled={disabled || !source || !fields.experiment_name.trim()}>预览指标（不写入）</button>
    {preview && plan && <div className="metric-import-preview" aria-label="指标入账预览">
      <strong>{preview.request.source_name} · {plan.rows.length}/{plan.row_count} 行可用</strong>
      <small>来源版本：{preview.result.source_version_id || "未绑定，不能确认"}</small>
      {plan.missing_fields.length > 0 && <p role="alert">待补齐：{plan.missing_fields.join("、")}。缺少指标名或数值时需要更正原件。</p>}
      {plan.rows.map((row, index) => <dl key={index}>
        {[["指标", row.metric_name], ["值 / 单位", `${row.value} / ${row.unit}`], ["方向 / 定义 / 划分", `${row.direction} / ${row.definition_version} / ${row.split}`],
          ["时间", row.performed_at], ["数据集 / 脚本", `${row.dataset_version} / ${row.evaluation_script_ref}`], ["配置 / 环境", `${row.config_ref || "未提供"} / ${row.environment_ref || "未提供"}`], ["来源定位", row.source_locator]]
          .map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{String(value ?? "未提供")}</dd></div>)}
      </dl>)}
      <button type="button" onClick={() => void submit(true)} disabled={!canConfirm}>确认以上口径并入账</button>
    </div>}
    {message && <p role="status" aria-live="polite">{message}</p>}
  </section>;
}
