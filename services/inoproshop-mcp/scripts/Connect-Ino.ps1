param(
    [switch]$Check,
    [string]$InoExe,
    [string]$Profile,
    [string]$Workspace,
    [string]$StateDir,
    [string]$ProjectRoot,
    [ValidateScript({ $_ -eq 0 -or ($_ -ge 1024 -and $_ -le 65535) })] [int]$Port = 0,
    [switch]$NoOmp,
    [switch]$NonInteractive
)

# Keep this file compatible with Windows PowerShell 5.1. Dot sourcing exposes
# the orchestration functions for isolated tests without starting applications.
$ErrorActionPreference = 'Stop'
$script:InoServiceRoot = Split-Path -Parent $PSScriptRoot

function Get-InoHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-InoFileHash([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Write-InoJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Remove-InoRuntimeItem([string]$Path, [string]$RuntimeRoot) {
    $resolvedRoot = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $resolvedTarget = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedTarget.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing cleanup outside the private runtime directory.'
    }
    if (Test-Path -LiteralPath $resolvedTarget) { Remove-Item -LiteralPath $resolvedTarget -Force -Recurse }
}

function Get-InoLockedArchive($Artifact, [string]$Destination, [string]$RuntimeRoot) {
    if ($Artifact.archive_url -notmatch '^https://' -or $Artifact.archive_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Invalid dependency release lock.'
    }
    if (Test-Path -LiteralPath $Destination) {
        if ((Get-InoFileHash $Destination) -eq $Artifact.archive_sha256) { return }
        Remove-InoRuntimeItem $Destination $RuntimeRoot
    }
    $partial = $Destination + '.partial'
    Remove-InoRuntimeItem $partial $RuntimeRoot
    $previousProgress = $ProgressPreference
    try {
        # Windows PowerShell 5.1 progress rendering can make binary downloads
        # take minutes; the surrounding stage messages already show progress.
        $ProgressPreference = 'SilentlyContinue'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $Artifact.archive_url -OutFile $partial -TimeoutSec 180
        if ((Get-InoFileHash $partial) -ne $Artifact.archive_sha256) {
            throw 'Downloaded archive checksum differs from the release lock. Run Connect-Ino.cmd again to retry.'
        }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
    } finally { $ProgressPreference = $previousProgress; Remove-InoRuntimeItem $partial $RuntimeRoot }
}

function Get-InoUv($Lock, [string]$RuntimeRoot) {
    $uvDirectory = Join-Path $RuntimeRoot ('uv-' + $Lock.uv.version)
    $uv = Join-Path $uvDirectory 'uv.exe'
    $manifest = Join-Path $uvDirectory 'verified.json'
    if ((Test-Path -LiteralPath $uv) -and (Test-Path -LiteralPath $manifest)) {
        try {
            $verified = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($verified.archive_sha256 -eq $Lock.uv.archive_sha256 -and
                $verified.executable_sha256 -eq (Get-InoFileHash $uv)) { return $uv }
        } catch { Write-Host '[准备] 下载工具缓存需要修复。' }
    }
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    $archive = Join-Path $RuntimeRoot ('uv-' + $Lock.uv.version + '.zip')
    Write-Host ('[准备] 准备下载工具 uv ' + $Lock.uv.version + '。')
    Get-InoLockedArchive $Lock.uv $archive $RuntimeRoot
    $staging = Join-Path $RuntimeRoot ('uv-extract-' + [Guid]::NewGuid().ToString('N'))
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $staging)
        $candidates = @(Get-ChildItem -LiteralPath $staging -Filter uv.exe -File -Recurse)
        if ($candidates.Count -ne 1) { throw 'The locked uv archive does not contain one executable.' }
        New-Item -ItemType Directory -Path $uvDirectory -Force | Out-Null
        Copy-Item -LiteralPath $candidates[0].FullName -Destination $uv -Force
        Write-InoJson $manifest @{ archive_sha256 = $Lock.uv.archive_sha256; executable_sha256 = (Get-InoFileHash $uv) }
        return $uv
    } finally { Remove-InoRuntimeItem $staging $RuntimeRoot }
}

function Test-InoPython([string]$Python, [switch]$Dependencies) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { return $false }
    $code = 'import sys; assert sys.version_info[:2] == (3,12)'
    if ($Dependencies) {
        $code += '; import mcp,pydantic,uvicorn,neo4j,codegraphcontext,pytest,httpx; from importlib.metadata import version; assert version(''mcp'') == ''1.30.0''; assert version(''codegraphcontext'') == ''0.6.13''; assert version(''neo4j'') == ''5.28.2'''
    }
    try {
        & $Python -B -c $code 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Invoke-InoUv([string]$Uv, [string[]]$Arguments) {
    & $Uv @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw '依赖准备失败。请检查网络后重新运行 Connect-Ino.cmd；已有数据会保留。' }
}

function Get-InoPythonPath([string]$ServiceRoot) {
    $root = Split-Path -Parent (Split-Path -Parent $ServiceRoot)
    if (Test-Path -LiteralPath (Join-Path $root 'offline')) {
        return (Join-Path $root 'runtime/python/python.exe')
    }
    return (Join-Path $ServiceRoot '.venv/Scripts/python.exe')
}

function Initialize-InoEnvironment([string]$ServiceRoot, [string]$RuntimeRoot, $Lock) {
    $root = Split-Path -Parent (Split-Path -Parent $ServiceRoot)
    if (Test-Path -LiteralPath (Join-Path $root 'offline')) {
        $portable = Get-InoPythonPath $ServiceRoot
        if (-not (Test-Path -LiteralPath $portable -PathType Leaf)) {
            throw '离线包的 Python 文件缺失，请完整重新解压发布包。'
        }
        Write-Host '[离线] 正在校验随包运行环境，无需下载或安装依赖。'
        & $portable -I -B (Join-Path $ServiceRoot 'scripts/verify-offline.py') --root $root | Out-Host
        if ($LASTEXITCODE -ne 0) { throw '离线包校验失败，请完整重新解压；不会联网下载替代文件。' }
        if (-not (Test-InoPython $portable -Dependencies)) { throw '离线 Python 依赖检查失败，请完整重新解压发布包。' }
        return $portable
    }
    $python = Join-Path $ServiceRoot '.venv/Scripts/python.exe'
    $marker = Join-Path $RuntimeRoot 'environment.json'
    $lockHash = Get-InoFileHash (Join-Path $ServiceRoot 'uv.lock')
    $manifestHash = Get-InoFileHash (Join-Path $ServiceRoot 'pyproject.toml')
    $runtimeHash = Get-InoFileHash (Join-Path $ServiceRoot 'launcher-runtime-lock.json')
    if ((Test-InoPython $python -Dependencies) -and (Test-Path -LiteralPath $marker)) {
        try {
            $installed = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($installed.lock_hash -eq $lockHash -and $installed.manifest_hash -eq $manifestHash -and $installed.runtime_hash -eq $runtimeHash) {
                Write-Host '[准备] 已有 Python 环境可用。'
                return $python
            }
        } catch { Write-Host '[准备] 依赖记录需要更新。' }
    }
    $uv = Get-InoUv $Lock $RuntimeRoot
    $environmentNames = @('UV_PYTHON_INSTALL_DIR', 'UV_CACHE_DIR', 'UV_PYTHON_DOWNLOADS_JSON_URL', 'UV_PYTHON_INSTALL_MIRROR', 'UV_PROJECT_ENVIRONMENT', 'UV_PYTHON', 'UV_NO_MANAGED_PYTHON', 'UV_MANAGED_PYTHON', 'UV_PYTHON_DOWNLOADS', 'UV_CONFIG_FILE')
    $saved = @{}
    foreach ($name in $environmentNames) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process'); [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    try {
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeRoot 'python'
        $env:UV_CACHE_DIR = Join-Path $RuntimeRoot 'uv-cache'
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $ServiceRoot '.venv'
        $targetPython = $python
        if (-not (Test-InoPython $python)) {
            Write-Host ('[准备] 安装项目专用 Python ' + $Lock.python.version + '。')
            # The locked uv executable includes the exact URL and SHA256 in
            # launcher-runtime-lock.json; uv verifies the archive before use.
            Invoke-InoUv $uv @('python', 'install', $Lock.python.managed_key, '--no-bin', '--no-registry', '--no-config', '--no-progress', '--mirror', 'https://github.com/astral-sh/python-build-standalone/releases/download')
            $targetPython = Join-Path $env:UV_PYTHON_INSTALL_DIR ($Lock.python.managed_key + '/python.exe')
            if (-not (Test-InoPython $targetPython)) { throw '项目专用 Python 安装未完成，请重新运行。' }
        }
        Write-Host '[准备] 按发布锁安装 MCP 与数据库依赖，首次下载可能需要几分钟。'
        Invoke-InoUv $uv @('sync', '--project', $ServiceRoot, '--frozen', '--extra', 'cgc', '--extra', 'test', '--python', $targetPython, '--no-python-downloads', '--no-config', '--no-progress')
        if (-not (Test-InoPython $python -Dependencies)) { throw 'Python 依赖检查未通过，请重新运行以修复。' }
        Write-InoJson $marker @{ lock_hash = $lockHash; manifest_hash = $manifestHash; runtime_hash = $runtimeHash }
        return $python
    } finally {
        foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    }
}

function Invoke-InoPython([string]$Python, [string[]]$Arguments) {
    $previousOutputEncoding = [Console]::OutputEncoding
    try {
        # Python uses UTF-8; native pipe decoding otherwise follows CP936 on
        # Chinese Windows consoles and corrupts the stage messages.
        [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
        & $Python @Arguments | Out-Host
        return $LASTEXITCODE
    } finally { [Console]::OutputEncoding = $previousOutputEncoding }
}

function Invoke-InoBootstrap {
    param([switch]$Check, [string]$InoExe, [string]$Profile, [string]$Workspace, [string]$StateDir, [string]$ProjectRoot, [ValidateScript({ $_ -eq 0 -or ($_ -ge 1024 -and $_ -le 65535) })] [int]$Port = 0, [switch]$NoOmp, [switch]$NonInteractive)
    $serviceRoot = $script:InoServiceRoot
    $script:InoPreparedContext = $null
    $script:InoPreparedContextFile = $null
    $root = Split-Path -Parent (Split-Path -Parent $serviceRoot)
    if (-not $StateDir) { $StateDir = if ($env:INO_MCP_STATE) { $env:INO_MCP_STATE } else { Join-Path $root 'work/mcp_p0/state' } }
    $StateDir = [IO.Path]::GetFullPath($StateDir)
    $runtimeRoot = Join-Path $root 'work/mcp_p0/launcher-runtime'
    $contextFile = if ($Check) { Join-Path $StateDir 'launcher-context.json' } else { Join-Path $StateDir ('launcher/context-' + [Guid]::NewGuid().ToString('N') + '.json') }
    $python = Get-InoPythonPath $serviceRoot
    $mutex = $null
    $ownsMutex = $false
    $previousPythonPath = $env:PYTHONPATH
    $previousPythonUtf8 = $env:PYTHONUTF8
    $previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
    try {
        $ompAvailable = [bool](Get-Command omp -ErrorAction SilentlyContinue)
        if ($Check) {
            Write-Host '[诊断] 只检查现有环境，不安装或启动组件。'
            if ($ompAvailable) { Write-Host '[ohmypi] 已安装。' } else { Write-Host '[ohmypi] 未找到 omp，请先安装并完成模型配置。' }
            if (-not (Test-InoPython $python -Dependencies)) {
                Write-Host '[Python] 项目环境未就绪。正常运行 Connect-Ino.cmd 会自动准备依赖。'
                Write-Host ('[诊断] 状态目录：' + $StateDir)
                return 1
            }
        } else {
            if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw '首版一键入口仅支持 Windows x64。' }
            if (-not $NoOmp -and -not $ompAvailable) { throw '未找到 ohmypi（omp）。请先安装 ohmypi 并完成模型配置，然后重新运行。' }
            # The virtual environment and download cache belong to the root,
            # so all state directories under that root share one startup lock.
            $mutexName = 'Local\InoProShopConnect-' + (Get-InoHash ([IO.Path]::GetFullPath($root).ToLowerInvariant()))
            $mutex = New-Object Threading.Mutex($false, $mutexName)
            try { $ownsMutex = $mutex.WaitOne(0) }
            catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
            if (-not $ownsMutex) { throw '另一个一键接入正在准备中。请等待原窗口完成后重试。' }
            $lock = Get-Content -LiteralPath (Join-Path $serviceRoot 'launcher-runtime-lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            $python = Initialize-InoEnvironment $serviceRoot $runtimeRoot $lock
        }
        $env:PYTHONPATH = Join-Path $serviceRoot 'src'
        if ($previousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $previousPythonPath }
        $env:PYTHONUTF8 = '1'
        $env:PYTHONDONTWRITEBYTECODE = '1'
        $arguments = @('-B', '-u', '-m', 'inoproshop_mcp.launcher', '--root', $root, '--state-dir', $StateDir, '--context-out', $contextFile)
        foreach ($option in @(@('--ino-exe', $InoExe), @('--profile', $Profile), @('--workspace', $Workspace), @('--project-root', $ProjectRoot))) {
            if ($option[1]) { $arguments += @($option[0], $option[1]) }
        }
        if ($Check) { $arguments += '--check' }
        if ($Port) { $arguments += @('--port', [string]$Port) }
        if ($NonInteractive) { $arguments += '--non-interactive' }
        $exitCode = Invoke-InoPython $python $arguments
        if ($exitCode -ne 0) { return $exitCode }
        # Do not hold the startup lock during the interactive OMP session.
        if ($ownsMutex) { $mutex.ReleaseMutex(); $ownsMutex = $false }
        if (-not $Check) {
            if (-not (Test-Path -LiteralPath $contextFile)) { throw '连接检查未返回启动配置，请查看状态目录中的日志。' }
            $context = Get-Content -LiteralPath $contextFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($NoOmp) {
                & (Join-Path $serviceRoot 'scripts/Start-OMP.ps1') -StateDir $context.state_dir -Workspace $context.workspace -Url $context.url -ContextFile $contextFile -ConfigureOnly | Out-Host
                return $LASTEXITCODE
            }
            $script:InoPreparedContext = $context
            $script:InoPreparedContextFile = $contextFile
        }
        if ($Check -and -not $ompAvailable -and -not $NoOmp) { return 1 }
        return 0
    } catch {
        Write-Host ('[失败] ' + $_.Exception.Message) -ForegroundColor Red
        Write-Host ('[诊断] 状态与日志目录：' + (Join-Path $StateDir 'launcher'))
        return 1
    } finally {
        $env:PYTHONPATH = $previousPythonPath
        $env:PYTHONUTF8 = $previousPythonUtf8
        $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
        if ($ownsMutex) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-InoBootstrap @PSBoundParameters
    if ($result -eq 0 -and -not $Check -and -not $NoOmp) {
        try {
            # Native interactive programs must inherit the terminal directly.
            # Capturing this call or piping it to Out-Host disables stdout TTY.
            $connection = $script:InoPreparedContext
            & (Join-Path $script:InoServiceRoot 'scripts/Start-OMP.ps1') -StateDir $connection.state_dir -Workspace $connection.workspace -Url $connection.url -ContextFile $script:InoPreparedContextFile
            $result = $LASTEXITCODE
        } catch { Write-Host ('[失败] ' + $_.Exception.Message) -ForegroundColor Red; $result = 1 }
    }
    if ($result -ne 0 -and -not $NonInteractive) {
        try {
            # PowerShell also starts .cmd files through cmd /c. Only Explorer
            # ancestry identifies the ordinary double-click case reliably.
            $currentProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $PID)
            $commandProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $currentProcess.ParentProcessId)
            $parentProcess = Get-Process -Id $commandProcess.ParentProcessId -ErrorAction Stop
            if ($commandProcess.Name -eq 'cmd.exe' -and $parentProcess.ProcessName -eq 'explorer') {
                Read-Host '按回车关闭此窗口' | Out-Null
            }
        } catch { }
    }
    exit $result
}
