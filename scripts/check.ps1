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
    Invoke-SoulDrivePython -Arguments @("-m", "unittest", "discover", "-s", "tests", "-v")
    Assert-LastCommandSucceeded "backend tests"

    Invoke-SoulDrivePython -Arguments @("-m", "compileall", "-q", "core", "tests")
    Assert-LastCommandSucceeded "python compileall"

    Push-Location "souldrive-ui"
    try {
        npm.cmd run build
        Assert-LastCommandSucceeded "frontend build"

        Push-Location "src-tauri"
        try {
            cargo check
            Assert-LastCommandSucceeded "cargo check"
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
