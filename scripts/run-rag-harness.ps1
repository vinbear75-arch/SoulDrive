param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,

    [string]$RetrievalResults = "eval/results/retrieval_candidate_v0.3.jsonl",

    [string]$RetrievalDataset = "eval/retrieval_quality_blind_v0.3.jsonl",

    [string]$IngestionDataset = "eval/ingestion_quality_v0.1.jsonl",

    [string]$ReportPath = "eval/results/rag_harness_candidate_v0.1.json",

    [int]$TopK = 6,

    [string]$Passphrase = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"

$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceManifest = Join-Path $Workspace "config/workspace.json"
$retrievalDatasetPath = Join-Path $repoRoot $RetrievalDataset
$retrievalResultsPath = Join-Path $repoRoot $RetrievalResults
$ingestionDatasetPath = Join-Path $repoRoot $IngestionDataset
$reportFullPath = Join-Path $repoRoot $ReportPath
$harnessScript = Join-Path $repoRoot "scripts/eval_harness.py"

if (-not (Test-Path -LiteralPath $workspaceManifest)) {
    throw "SoulDrive workspace manifest not found: $workspaceManifest"
}
if (-not (Test-Path -LiteralPath $retrievalDatasetPath)) {
    throw "Retrieval dataset not found: $retrievalDatasetPath"
}
if (-not (Test-Path -LiteralPath $retrievalResultsPath)) {
    throw "Retrieval results not found: $retrievalResultsPath"
}
if (-not (Test-Path -LiteralPath $ingestionDatasetPath)) {
    throw "Ingestion dataset not found: $ingestionDatasetPath"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $reportFullPath) | Out-Null

$harnessArgs = @(
    "run", "--no-capture-output", "-n", "souldrive", "python", $harnessScript,
    "--workspace", $Workspace,
    "--retrieval-dataset", $retrievalDatasetPath,
    "--retrieval-results", $retrievalResultsPath,
    "--ingestion-dataset", $ingestionDatasetPath,
    "--top-k", "$TopK",
    "--save-report", $reportFullPath
)
if ($Passphrase) {
    $harnessArgs += @("--passphrase", $Passphrase)
}

& conda @harnessArgs
exit $LASTEXITCODE
