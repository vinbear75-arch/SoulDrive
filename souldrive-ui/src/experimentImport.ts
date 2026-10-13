export interface MetricSource {
  name: string;
  document_id?: string;
  scope?: string;
  project_id?: string | null;
  project_ids?: string[];
}

export const emptyMetricFields = {
  experiment_name: "", performed_at: "", dataset_version: "", config_ref: "",
  evaluation_script_ref: "", environment_ref: "", unit: "", direction: "",
  definition_version: "", split: "",
};
export type MetricFields = typeof emptyMetricFields;
export type MetricRequest = MetricFields & {
  source_name: string;
  source_document_id: string;
  source_version_id?: string;
  confirmed: boolean;
};
export interface MetricImportResult {
  status: string;
  source_document_id?: string | null;
  source_version_id?: string;
  run_id?: string;
  warning?: string;
  plan?: {
    ready: boolean;
    requires_confirmation: boolean;
    writes: boolean;
    confirmed: boolean;
    row_count: number;
    missing_fields: string[];
    rows: Array<Record<string, string | number | null>>;
  };
}
export interface MetricPreview {
  projectId: string;
  request: MetricRequest;
  result: MetricImportResult;
}

export function isProjectMetricSource(source: MetricSource, projectId: string): boolean {
  return source.scope === "project" && Boolean(source.document_id)
    && (source.project_id === projectId || Boolean(source.project_ids?.includes(projectId)))
    && /\.(csv|tsv|xlsx|json|ya?ml|log|txt|md|zip)$/i.test(source.name);
}

export function metricPreviewRequest(projectId: string, source: MetricSource, fields: MetricFields): MetricRequest {
  if (!projectId || !isProjectMetricSource(source, projectId)) throw new Error("请选择当前项目中有稳定来源 ID 的指标文件。");
  if (!fields.experiment_name.trim()) throw new Error("请填写实验名称。");
  return { ...fields, source_name: source.name, source_document_id: source.document_id!, confirmed: false };
}

export function metricConfirmationRequest(preview: MetricPreview, projectId: string, source: MetricSource, fields: MetricFields): MetricRequest {
  const request = metricPreviewRequest(projectId, source, fields);
  const { result } = preview;
  if (preview.projectId !== projectId || JSON.stringify(preview.request) !== JSON.stringify(request)) {
    throw new Error("项目、来源或条件已变化，请重新预览。");
  }
  if (result.status !== "confirmation_required" || !result.plan?.ready || !result.plan.requires_confirmation
      || result.plan.writes !== false || result.plan.confirmed !== false || result.plan.missing_fields.length
      || !result.plan.rows.length || result.plan.rows.length !== result.plan.row_count
      || !result.source_version_id || result.source_document_id !== request.source_document_id) {
    throw new Error("预览不完整或来源版本未绑定，不能确认入账。");
  }
  return { ...request, source_version_id: result.source_version_id, confirmed: true };
}
