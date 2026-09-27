# 蜂仔翻新 · 门店短视频流水线 —— 一键更新到最新代码
# 由项目根目录的 windows_update.bat 调用。只替换程序文件，不会动密钥（.env）、配置（config.yaml）、字体、音乐和运行记录。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$AppDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Branch = 'claude/stoic-planck-s67mdb'
$ZipUrl = "https://codeload.github.com/nrgrl1024-commits/--/zip/refs/heads/$Branch"
$Mirror = 'https://mirrors.aliyun.com/pypi/simple'
$Keep = @('.env', 'config.yaml', '.venv', 'work', 'windows_update.bat')
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Fail($msg) { Write-Host ''; Write-Host $msg -ForegroundColor Red; exit 1 }

Write-Host '==== 1/3 下载最新代码 ====' -ForegroundColor Cyan
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = Join-Path $env:TEMP 'fengzai-video-update.zip'
$ok = $false
for ($i = 1; $i -le 5 -and -not $ok; $i++) {
    try {
        Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -UseBasicParsing -TimeoutSec 120
        $ok = $true
    } catch {
        Write-Host "  第 $i 次下载失败，10 秒后重试……"
        Start-Sleep -Seconds 10
    }
}
if (-not $ok) { Fail '下载失败（GitHub 在国内有时连不上）。可以稍后再试，或者用浏览器下载 ZIP 手动覆盖。' }

Write-Host '==== 2/3 替换程序文件 ====' -ForegroundColor Cyan
$tmp = Join-Path $env:TEMP 'fengzai-video-update'
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
if (-not $src) { Fail '压缩包内容不对，请稍后重试。' }
foreach ($item in Get-ChildItem $src.FullName -Force) {
    if ($Keep -contains $item.Name) { continue }
    Copy-Item $item.FullName -Destination $AppDir -Recurse -Force
}
Remove-Item $tmp -Recurse -Force
Remove-Item $zip -Force

Write-Host '==== 3/3 更新依赖 ====' -ForegroundColor Cyan
$venvPy = Join-Path $AppDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { Fail '还没安装过，请先双击 windows_install.bat。' }
& $venvPy -m pip install -q -r (Join-Path $AppDir 'requirements.txt') -i $Mirror
if ($LASTEXITCODE -ne 0) { Fail '更新依赖失败，请检查网络后重新运行。' }

Write-Host ''
Write-Host '更新完成！' -ForegroundColor Green
Write-Host '请关掉正在运行的黑色窗口（fengzai-video），再双击 windows_start.bat 重新启动。'
