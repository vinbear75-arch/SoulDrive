$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
chcp 65001 | Out-Null

$Root = Split-Path -Parent $PSScriptRoot

function Invoke-SoulDrivePython {
    param([Parameter(Mandatory=$true)][string[]]$Arguments)

    if ($env:SOULDRIVE_PYTHON) {
        & $env:SOULDRIVE_PYTHON @Arguments
    } elseif (Get-Command conda -ErrorAction SilentlyContinue) {
        & conda run --no-capture-output -n souldrive python @Arguments
    } else {
        & python @Arguments
    }
}

function Assert-LastCommandSucceeded {
    param([Parameter(Mandatory=$true)][string]$Step)

    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

Push-Location $Root
try {
    Invoke-SoulDrivePython -Arguments @("-m", "pytest", "-q")
    Assert-LastCommandSucceeded "backend tests"

    Invoke-SoulDrivePython -Arguments @("-m", "compileall", "-q", "core", "tests")
    Assert-LastCommandSucceeded "python compileall"

    New-Item -ItemType Directory -Force -Path (Join-Path $Root "build/quality-gates") | Out-Null
    Invoke-SoulDrivePython -Arguments @("scripts/eval_memory_quality.py", "--output", "build/quality-gates/memory-quality.json")
    Assert-LastCommandSucceeded "memory quality gate"

    Invoke-SoulDrivePython -Arguments @("scripts/eval_graphrag_route.py", "--output", "build/quality-gates/graphrag-route.json")
    Assert-LastCommandSucceeded "GraphRAG route quality gate"

    Invoke-SoulDrivePython -Arguments @("scripts/eval_workflow_roles.py", "--output", "build/quality-gates/workflow-roles.json")
    Assert-LastCommandSucceeded "workflow role quality gate"

    Invoke-SoulDrivePython -Arguments @("scripts/eval_episodic_workflow_routing.py", "--runtime", "--output", "build/quality-gates/episodic-workflow-routing.json")
    Assert-LastCommandSucceeded "episodic workflow routing gate"

    Push-Location "souldrive-ui"
    try {
        npm.cmd run build
        Assert-LastCommandSucceeded "frontend build"

        npm.cmd test
        Assert-LastCommandSucceeded "frontend tests"

        Push-Location "src-tauri"
        try {
            cargo check --locked
            Assert-LastCommandSucceeded "locked cargo check"
        }
        finally {
            Pop-Location
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}
