param(
    [string]$PlanPath = "",
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
$currentOperation = "startup"
trap {
    Write-Error ("WPS_RENDER_STEP={0}; {1}" -f $currentOperation, $_.Exception.Message)
    break
}

function Get-OfficeRgb {
    param([int]$Red, [int]$Green, [int]$Blue)
    return $Red + ($Green * 256) + ($Blue * 65536)
}

function Add-Rectangle {
    param($Slide, [float]$Left, [float]$Top, [float]$Width, [float]$Height, [int]$Color)
    $shape = $Slide.Shapes.AddShape(1, $Left, $Top, $Width, $Height)
    $shape.Fill.ForeColor.RGB = $Color
    $shape.Fill.Solid()
    $shape.Line.Visible = 0
    return $shape
}

function Add-TextBox {
    param(
        $Slide,
        [string]$Text,
        [float]$Left,
        [float]$Top,
        [float]$Width,
        [float]$Height,
        [float]$FontSize,
        [int]$Color,
        [bool]$Bold = $false,
        [int]$Alignment = 1
    )
    $shape = $Slide.Shapes.AddTextbox(1, $Left, $Top, $Width, $Height)
    $shape.TextFrame.MarginLeft = 0
    $shape.TextFrame.MarginRight = 0
    $shape.TextFrame.MarginTop = 0
    $shape.TextFrame.MarginBottom = 0
    $shape.TextFrame.WordWrap = -1
    $range = $shape.TextFrame.TextRange
    $range.Text = $Text
    $range.Font.Name = "Microsoft YaHei"
    try { $range.Font.NameFarEast = "Microsoft YaHei" } catch {}
    $range.Font.Size = $FontSize
    $range.Font.Bold = $(if ($Bold) { -1 } else { 0 })
    $range.Font.Color.RGB = $Color
    $range.ParagraphFormat.Alignment = $Alignment
    return $shape
}

$ink = Get-OfficeRgb 28 38 36
$green = Get-OfficeRgb 23 63 59
$greenSoft = Get-OfficeRgb 229 239 234
$gold = Get-OfficeRgb 218 171 74
$paper = Get-OfficeRgb 248 247 242
$white = Get-OfficeRgb 255 255 255
$muted = Get-OfficeRgb 96 105 101
$line = Get-OfficeRgb 215 213 204

$application = $null
$presentation = $null
try {
    $currentOperation = "read_plan"
    if ($PlanPath) {
        if (-not (Test-Path -LiteralPath $PlanPath)) {
            throw "Presentation plan does not exist."
        }
        $rawPlan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8
    } else {
        $rawPlan = [Console]::In.ReadToEnd()
    }
    $plan = $rawPlan | ConvertFrom-Json
    if (-not $plan.slides -or $plan.slides.Count -lt 1) {
        throw "Presentation plan contains no slides."
    }

    $preferWpsCom = $env:SOULDRIVE_PREFER_WPS_COM -eq "1"
    if ($preferWpsCom) {
        $currentOperation = "create_wps_application"
        $application = New-Object -ComObject "KWPP.Application"
        $renderer = "WPS"
    } else {
        $currentOperation = "create_powerpoint_application"
        $application = New-Object -ComObject "PowerPoint.Application"
        $renderer = "PowerPoint OpenXML"
    }
    try { $application.DisplayAlerts = 0 } catch {}
    try { $application.Visible = 0 } catch {}
    $currentOperation = "create_presentation"
    $presentation = $application.Presentations.Add()
    $currentOperation = "configure_page"
    $presentation.PageSetup.SlideWidth = 960
    $presentation.PageSetup.SlideHeight = 540

    $slideNumber = 0
    foreach ($slidePlan in $plan.slides) {
        $slideNumber += 1
        $currentOperation = "add_slide_$slideNumber"
        $slide = $presentation.Slides.Add($slideNumber, 12)
        $kind = [string]$slidePlan.kind

        if ($kind -eq "cover") {
            Add-Rectangle $slide 0 0 960 540 $green | Out-Null
            Add-Rectangle $slide 58 70 7 330 $gold | Out-Null
            Add-TextBox $slide ([string]$slidePlan.eyebrow) 92 78 760 30 15 $gold $true | Out-Null
            Add-TextBox $slide ([string]$slidePlan.title) 92 132 760 155 34 $white $true | Out-Null
            Add-TextBox $slide ([string]$slidePlan.subtitle) 92 314 690 80 17 $white $false | Out-Null
            Add-TextBox $slide ([string]$plan.generated_at_label) 92 456 420 24 10 $gold $false | Out-Null
            Add-TextBox $slide "SoulDrive" 758 456 110 24 11 $white $true 3 | Out-Null
            continue
        }

        Add-Rectangle $slide 0 0 960 540 $paper | Out-Null
        Add-Rectangle $slide 0 0 960 12 $green | Out-Null
        Add-TextBox $slide ([string]$slidePlan.title) 56 38 790 54 25 $green $true | Out-Null
        Add-Rectangle $slide 56 98 74 4 $gold | Out-Null

        if ($kind -eq "sources") {
            $sourceLines = @()
            foreach ($source in $slidePlan.sources) {
                $sourceLines += (([char]0x2022).ToString() + " " + [string]$source)
            }
            Add-TextBox $slide ($sourceLines -join "`r`n") 58 130 830 310 16 $ink $false | Out-Null
        } else {
            $pointLines = @()
            foreach ($point in $slidePlan.points) {
                $pointLines += (([char]0x2022).ToString() + " " + [string]$point)
            }
            Add-TextBox $slide ($pointLines -join "`r`n`r`n") 58 126 620 330 17 $ink $false | Out-Null

            Add-Rectangle $slide 718 126 182 292 $white | Out-Null
            Add-Rectangle $slide 718 126 182 7 $gold | Out-Null
            Add-TextBox $slide "证据来源" 738 151 142 26 13 $green $true | Out-Null
            $sourceLines = @()
            foreach ($source in $slidePlan.sources) {
                $sourceLines += (([char]0x2022).ToString() + " " + [string]$source)
            }
            Add-TextBox $slide ($sourceLines -join "`r`n`r`n") 738 190 142 190 11 $muted $false | Out-Null
        }

        Add-Rectangle $slide 56 482 844 1 $line | Out-Null
        Add-TextBox $slide ([string]$slidePlan.footer) 56 492 700 22 9 $muted $false | Out-Null
        Add-TextBox $slide ([string]$slideNumber) 854 492 46 22 9 $muted $true 3 | Out-Null
    }

    $outputDirectory = Split-Path -Parent $OutputPath
    if (-not (Test-Path -LiteralPath $outputDirectory)) {
        New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    }
    $currentOperation = "save_presentation"
    $presentation.SaveAs($OutputPath, 24)
    if (-not (Test-Path -LiteralPath $OutputPath)) {
        throw "Presentation renderer did not create the output file."
    }
    [PSCustomObject]@{
        renderer = $renderer
        output_path = $OutputPath
        slide_count = $plan.slides.Count
    } | ConvertTo-Json -Compress
} finally {
    if ($presentation -ne $null) {
        try { $presentation.Close() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation) } catch {}
    }
    if ($application -ne $null) {
        try { $application.Quit() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
