# 蜂仔翻新 · 门店短视频流水线 —— Windows 一键安装
# 由项目根目录的 windows_install.bat 调用，可以重复运行：已经装好的部分会跳过，已填的密钥不会被覆盖。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # 关掉进度条，否则下载会很慢
$AppDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $AppDir
$Mirror = 'https://mirrors.aliyun.com/pypi/simple'
$Utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Step($msg) { Write-Host ''; Write-Host "==== $msg ====" -ForegroundColor Cyan }
function Fail($msg) { Write-Host ''; Write-Host $msg -ForegroundColor Red; exit 1 }

function Test-Python($exe, $argsList) {
    try {
        $out = & $exe @argsList -c 'import sys; print(sys.version_info >= (3, 10))' 2>$null
        return ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq 'True')
    } catch { return $false }
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-Python 'py' @('-3')) { return @{ Exe = 'py'; Args = @('-3') } }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        if (Test-Python 'python' @()) { return @{ Exe = 'python'; Args = @() } }
    }
    foreach ($ver in '313', '312', '311', '310') {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$ver\python.exe"
        if ((Test-Path $p) -and (Test-Python $p @())) { return @{ Exe = $p; Args = @() } }
    }
    return $null
}

# ---------- 1. Python ----------
Step '1/6 检查 Python'
$py = Find-Python
if (-not $py) {
    Write-Host '没有找到 Python 3.10 以上版本，正在自动安装 Python 3.11（需要几分钟）……'
    $installer = Join-Path $env:TEMP 'python-3.11.9-amd64.exe'
    $urls = @(
        'https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-amd64.exe',
        'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe'
    )
    $ok = $false
    foreach ($u in $urls) {
        try {
            Write-Host "  下载：$u"
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $u -OutFile $installer -UseBasicParsing
            $ok = $true; break
        } catch { Write-Host '  这个地址下载失败，换一个试试' }
    }
    if (-not $ok) { Fail '下载 Python 失败。请手动从 https://www.python.org 下载安装 Python 3.11（安装时勾选 Add python.exe to PATH），然后重新运行本程序。' }
    Start-Process -FilePath $installer -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1' -Wait
    $py = Find-Python
    if (-not $py) { Fail 'Python 安装后仍然找不到。请重启电脑后再运行一次本程序。' }
}
Write-Host "  使用 Python：$($py.Exe) $($py.Args -join ' ')"

# ---------- 2. 依赖 ----------
Step '2/6 创建运行环境并安装依赖（第一次需要几分钟）'
$venvPy = Join-Path $AppDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    & $py.Exe @($py.Args) -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail '创建运行环境失败。' }
}
& $venvPy -m pip install -q --upgrade pip -i $Mirror
& $venvPy -m pip install -q -r requirements.txt -i $Mirror
if ($LASTEXITCODE -ne 0) { Fail '安装依赖失败，请检查网络后重新运行。' }

# ---------- 3. 字体 ----------
Step '3/6 准备字体'
$fontsDir = Join-Path $AppDir 'assets\fonts'
$hasFont = @(Get-ChildItem $fontsDir -File | Where-Object { $_.Extension -match '^\.(ttf|otf|ttc)$' }).Count -gt 0
$testFont = $false
if (-not $hasFont) {
    foreach ($f in 'msyh.ttc', 'msyhbd.ttc') {
        $src = Join-Path $env:WINDIR "Fonts\$f"
        if (Test-Path $src) { Copy-Item $src $fontsDir; $testFont = $true }
    }
    if ($testFont) {
        Write-Host '  assets\fonts 里还没有字体，先临时用系统自带的「微软雅黑」做测试。' -ForegroundColor Yellow
        Write-Host '  注意：微软雅黑不能免费商用。正式发布到视频号之前，请换成有商用授权的字体（见 assets\fonts\README.md）。' -ForegroundColor Yellow
    } else {
        Write-Host '  没有找到可用字体，请把字体文件放进 assets\fonts 后再运行。' -ForegroundColor Yellow
    }
} else {
    Write-Host '  assets\fonts 里已有字体'
}

# ---------- 4. 密钥和配置 ----------
Step '4/6 填写密钥'
$envPath = Join-Path $AppDir '.env'
$lines = @()
if (Test-Path $envPath) { $lines = @([IO.File]::ReadAllLines($envPath, $Utf8)) }
$fields = [ordered]@{
    'FEISHU_APP_ID'     = '飞书 App ID（cli_ 开头）'
    'FEISHU_APP_SECRET' = '飞书 App Secret'
    'FEISHU_APP_TOKEN'  = '多维表格链接里 base/ 后面那一串'
    'ARK_API_KEY'       = '豆包（火山方舟）API Key'
    'ARK_MODEL'         = '豆包模型名（doubao- 开头或 ep- 开头）'
}
foreach ($key in $fields.Keys) {
    $existing = $lines | Where-Object { $_ -match "^$key=." }
    if ($existing) { Write-Host "  $key 已填写，跳过"; continue }
    $value = ''
    while (-not $value) { $value = (Read-Host "  请输入 $($fields[$key])").Trim() }
    $lines = @($lines | Where-Object { $_ -notmatch "^$key=" }) + "$key=$value"
}
[IO.File]::WriteAllLines($envPath, [string[]]$lines, $Utf8)

$cfgPath = Join-Path $AppDir 'config.yaml'
if (-not (Test-Path $cfgPath)) {
    $cfg = [IO.File]::ReadAllText((Join-Path $AppDir 'config.example.yaml'), $Utf8)
    if ($testFont) { $cfg = $cfg.Replace('"WenQuanYi Zen Hei"', '"Microsoft YaHei"') }
    [IO.File]::WriteAllText($cfgPath, $cfg, $Utf8)
    Write-Host '  已生成 config.yaml'
}

# ---------- 5. 初始化飞书表格 ----------
Step '5/6 初始化飞书表格（建「门店资料」「母版」「今日任务」，给各门店页补字段）'
$env:PYTHONUTF8 = '1'
& $venvPy -m fengzai_video setup
if ($LASTEXITCODE -ne 0) {
    Fail ("初始化失败。常见原因：应用没发布、没把应用加进多维表格、权限没开通、密钥填错。`n" +
          "要改密钥：用记事本打开 $envPath 修改后，重新双击 windows_install.bat。")
}

# ---------- 6. 开机自启 ----------
Step '6/6 设置开机自动启动'
$answer = Read-Host '  电脑开机后自动运行？输入 Y 回车表示要，直接回车表示不要'
if ($answer -match '^[Yy]') {
    $startup = [Environment]::GetFolderPath('Startup')
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path $startup 'fengzai-video.lnk'))
    $lnk.TargetPath = Join-Path $AppDir 'windows_start.bat'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Save()
    Write-Host '  已设置：以后开机会自动运行'
}

Write-Host ''
Write-Host '安装完成！' -ForegroundColor Green
Write-Host '现在双击项目文件夹里的 windows_start.bat 开始运行。运行时会有一个黑色窗口，不要关掉它（可以最小化）。'
Write-Host '另外请在「设置 → 系统 → 电源」里把电脑设为「从不睡眠」，否则电脑睡着后系统也会停。'
