$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
chcp 65001 | Out-Null

$Root = Split-Path -Parent $PSScriptRoot
$Failed = $false

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

function Ensure-RustSecAdvisoryDb {
    $dbPath = Join-Path $Root "build\rustsec-advisory-db"
    if (Test-Path -LiteralPath (Join-Path $dbPath ".git")) {
        git -C $dbPath fetch --depth 1 origin | Out-Null
        if ($LASTEXITCODE -ne 0) { return $null }
        git -C $dbPath reset --hard origin/main | Out-Null
        if ($LASTEXITCODE -ne 0) { return $null }
        return $dbPath
    }

    $parent = Split-Path -Parent $dbPath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    git clone --depth 1 https://github.com/RustSec/advisory-db.git $dbPath | Out-Null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $dbPath
}

Push-Location $Root
try {
    Invoke-SoulDrivePython -Arguments @("-m", "pip", "check")
    if ($LASTEXITCODE -ne 0) { $Failed = $true }

    # PYSEC-2026-311: ChromaDB server API issue; SoulDrive uses embedded PersistentClient only.
    # CVE-2025-69872: diskcache pickle issue; transitive llama.cpp dependency, not an app-readable untrusted cache.
    $pipAuditArguments = @(
        "-m", "pip_audit",
        "--progress-spinner", "off",
        "--ignore-vuln", "PYSEC-2026-311",
        "--ignore-vuln", "CVE-2025-69872"
    )
    for ($attempt = 1; $attempt -le 3; $attempt += 1) {
        Invoke-SoulDrivePython -Arguments $pipAuditArguments
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -lt 3) {
            Write-Warning "pip-audit failed on attempt $attempt; retrying."
            Start-Sleep -Seconds 2
        }
    }
    if ($LASTEXITCODE -ne 0) { $Failed = $true }

    Push-Location "souldrive-ui"
    try {
        npm.cmd audit --omit=dev
        if ($LASTEXITCODE -ne 0) { $Failed = $true }

        Push-Location "src-tauri"
        try {
            if (Get-Command cargo-audit -ErrorAction SilentlyContinue) {
                $rustSecDb = Ensure-RustSecAdvisoryDb
                if ($null -eq $rustSecDb) {
                    Write-Warning "RustSec advisory database could not be prepared."
                    $Failed = $true
                } else {
                    # RUSTSEC-2026-0194/0195: quick-xml 0.39.4 is pulled only by rfd's Linux Wayland scanner path.
                    # SoulDrive's shipped desktop target is Windows, and the active plist path uses quick-xml 0.41.0.
                    $cargoAuditArguments = @(
                        "audit",
                        "--db", $rustSecDb,
                        "--no-fetch",
                        "--ignore", "RUSTSEC-2026-0194",
                        "--ignore", "RUSTSEC-2026-0195"
                    )
                    cargo @cargoAuditArguments
                }
                if ($LASTEXITCODE -ne 0) { $Failed = $true }
            } else {
                Write-Warning "cargo-audit is not installed; Rust advisory audit cannot run."
                $Failed = $true
            }
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

if ($Failed) {
    exit 1
}
