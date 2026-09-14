param(
    [switch]$InstallTask
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$pythonPath = "C:\Users\teraw_rp58jwl\anaconda3\python.exe"
$taskName = "台美股資金流向每日更新"
$taskTimes = @("11:30", "16:00", "17:00", "18:00", "21:30")

if ($InstallTask) {
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $triggers = $taskTimes | ForEach-Object {
        New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($_, "HH:mm", $null))
    }
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Description "每日重建 FinLab 台股與 LSEG 美股資金流資料並發布網站" `
        -Force | Out-Null
    Write-Host "已建立排程：$taskName，每天 11:30、16:00、17:00、18:00 與 21:30 執行。"
    return
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "找不到 Python：$pythonPath"
}

Set-Location -LiteralPath $projectRoot

& $pythonPath ".\update_us_industry_data.py"
if ($LASTEXITCODE -ne 0) { throw "美股資料更新失敗。" }

& $pythonPath ".\update_tw_industry_data.py"
if ($LASTEXITCODE -ne 0) { throw "台股資料更新失敗。" }

$indexHtml = [System.IO.File]::ReadAllText((Join-Path $projectRoot "dist\index.html"))
$dataJs = [System.IO.File]::ReadAllText((Join-Path $projectRoot "dist\data.js"))
$dataPrefix = "window.FLOW_DATA="
if (-not $indexHtml.Contains('<script src="./data.js?v=') -or -not $dataJs.StartsWith($dataPrefix)) {
    throw "網站資料驗證失敗。"
}
$jsonText = $dataJs.Substring($dataPrefix.Length).TrimEnd()
if ($jsonText.EndsWith(";")) {
    $jsonText = $jsonText.Substring(0, $jsonText.Length - 1)
}
try {
    $null = ConvertFrom-Json -InputObject $jsonText
} catch {
    throw "網站資料 JSON 驗證失敗：$($_.Exception.Message)"
}

& git add -- "dist/index.html" "dist/data.js"
& git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "資料沒有變更，不需發布。"
    return
}

$stamp = Get-Date -Format "yyyy-MM-dd"
& git commit -m "Update market flow data $stamp"
if ($LASTEXITCODE -ne 0) { throw "建立每日更新版本失敗。" }

& git push origin HEAD
if ($LASTEXITCODE -ne 0) { throw "推送 GitHub 失敗。" }

Write-Host "資料更新並發布完成：$stamp"
