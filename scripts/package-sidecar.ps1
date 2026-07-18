$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
chcp 65001 | Out-Null

$Root = Split-Path -Parent $PSScriptRoot
$Entry = Join-Path $Root "scripts\sidecar_entry.py"
$SidecarDir = Join-Path $Root "souldrive-ui\src-tauri\sidecars"
$BuildDir = Join-Path $Root "build\pyinstaller"

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

function Remove-FileInsideDirectory {
    param(
        [Parameter(Mandatory=$true)][string]$RootDirectory,
        [Parameter(Mandatory=$true)][string]$TargetPath
    )

    if (-not (Test-Path -LiteralPath $TargetPath)) {
        return
    }

    $resolvedRoot = (Resolve-Path -LiteralPath $RootDirectory).Path.TrimEnd('\')
    $resolvedTarget = (Resolve-Path -LiteralPath $TargetPath).Path
    if (-not $resolvedTarget.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside sidecar package: $resolvedTarget"
    }

    Remove-Item -LiteralPath $resolvedTarget -Force
}

function Remove-PortableSidecarBloat {
    param([Parameter(Mandatory=$true)][string]$PackageRoot)

    if (-not (Test-Path -LiteralPath $PackageRoot)) {
        return
    }

    $exactFiles = @(
        "_internal\llama_cpp\lib\ggml-cuda.lib",
        "_internal\cublas64_12.dll",
        "_internal\cublasLt64_12.dll",
        "_internal\cudart64_12.dll"
    )

    foreach ($relativePath in $exactFiles) {
        Remove-FileInsideDirectory -RootDirectory $PackageRoot -TargetPath (Join-Path $PackageRoot $relativePath)
    }

    $llamaLibDir = Join-Path $PackageRoot "_internal\llama_cpp\lib"
    if (Test-Path -LiteralPath $llamaLibDir) {
        Get-ChildItem -File -LiteralPath $llamaLibDir -Filter "*.lib" |
            ForEach-Object { Remove-FileInsideDirectory -RootDirectory $PackageRoot -TargetPath $_.FullName }
    }

    $cv2Dir = Join-Path $PackageRoot "_internal\cv2"
    if (Test-Path -LiteralPath $cv2Dir) {
        Get-ChildItem -File -LiteralPath $cv2Dir -Filter "opencv_videoio_ffmpeg*.dll" |
            ForEach-Object { Remove-FileInsideDirectory -RootDirectory $PackageRoot -TargetPath $_.FullName }
    }
}

Push-Location $Root
try {
    $PyInstallerArguments = @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name", "souldrive-sidecar",
        "--distpath", $SidecarDir,
        "--workpath", $BuildDir,
        "--specpath", $BuildDir,
        "--collect-all", "docling",
        "--copy-metadata", "docling",
        "--copy-metadata", "docling_slim",
        # docling_parse package data includes pdf_resources; collect it without a local site-packages path.
        "--collect-all", "docling_parse",
        "--collect-all", "rapidocr",
        "--collect-all", "onnxruntime",
        "--collect-all", "chromadb",
        "--collect-all", "sentence_transformers",
        "--collect-all", "transformers",
        "--collect-all", "tokenizers",
        "--collect-all", "llama_cpp",
        "--exclude-module", "tensorboard",
        "--exclude-module", "matplotlib",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        "--exclude-module", "hypothesis",
        "--exclude-module", "tkinter",
        "--hidden-import", "core.mcp_server",
        "--hidden-import", "core.indexer_worker",
        "--hidden-import", "core.gpu_smoke",
        "--hidden-import", "core.knowledge_base",
        "--hidden-import", "core.rag_engine",
        "--hidden-import", "core.graph_extractor",
        "--hidden-import", "chromadb.telemetry.product.posthog",
        "--hidden-import", "chromadb.api.rust",
        $Entry
    )
    Invoke-SoulDrivePython -Arguments $PyInstallerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller sidecar packaging failed with exit code $LASTEXITCODE"
    }

    Remove-PortableSidecarBloat -PackageRoot (Join-Path $SidecarDir "souldrive-sidecar")
}
finally {
    Pop-Location
}
