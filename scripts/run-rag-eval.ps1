param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,

    [string]$Dataset = "eval/retrieval_quality_blind_v0.3.jsonl",

    [string]$ResultPath = "eval/results/retrieval_baseline_v0.3.jsonl",

    [int]$TopK = 3,

    [string]$Passphrase = "",

    [switch]$IncludeAnswers,

    [switch]$SkipPreflight
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"

$repoRoot = Split-Path -Parent $PSScriptRoot
$datasetPath = Join-Path $repoRoot $Dataset
$resultPath = Join-Path $repoRoot $ResultPath
$evalScript = Join-Path $repoRoot "scripts/eval_rag.py"
$preflightScript = Join-Path $repoRoot "scripts/check_rag_eval_workspace.py"
$workspaceManifest = Join-Path $Workspace "config/workspace.json"

if (-not (Test-Path -LiteralPath $datasetPath)) {
    throw "Dataset not found: $datasetPath"
}
if (-not (Test-Path -LiteralPath $workspaceManifest)) {
    throw "SoulDrive workspace manifest not found: $workspaceManifest"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $resultPath) | Out-Null

if (-not $SkipPreflight) {
    $preflightArgs = @(
        "run", "--no-capture-output", "-n", "souldrive", "python", $preflightScript,
        "--workspace", $Workspace,
        "--manifest", (Join-Path $repoRoot "eval/corpus_manifest_v0.2.json")
    )
    if ($Passphrase) {
        $preflightArgs += @("--passphrase", $Passphrase)
    }
    & conda @preflightArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Workspace preflight failed. Import all v0.2 corpus PDFs before running baseline."
    }
}

$evalArgs = @(
    "run", "--no-capture-output", "-n", "souldrive", "python", $evalScript,
    "--dataset", $datasetPath,
    "--workspace", $Workspace,
    "--top-k", "$TopK",
    "--save-results", $resultPath
)
if ($Passphrase) {
    $evalArgs += @("--passphrase", $Passphrase)
}
if ($IncludeAnswers) {
    $evalArgs += "--include-answers"
}

& conda @evalArgs
exit $LASTEXITCODE
