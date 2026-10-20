param(
    [string]$StateDir,
    [string]$Workspace,
    [string]$Url = 'http://127.0.0.1:8765/mcp',
    [string]$ContextFile,
    [switch]$ConfigureOnly,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$OmpArguments
)
$ErrorActionPreference = 'Stop'
$serviceRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $serviceRoot)
if (-not $StateDir) { $StateDir = if ($env:INO_MCP_STATE) { $env:INO_MCP_STATE } else { Join-Path $workspaceRoot 'work/mcp_p0/state' } }
if (-not $Workspace) { $Workspace = Join-Path $workspaceRoot 'work/mcp_p0/omp' }
$tokenFile = Join-Path $StateDir 'token'
if (-not (Test-Path -LiteralPath $tokenFile)) { throw 'Start the MCP controller first.' }
$tokenValue = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
if ($tokenValue.Length -lt 32) { throw 'Invalid local token file.' }
$configDir = Join-Path $Workspace '.omp'
$skillDir = Join-Path $configDir 'skills/inoproshop'
New-Item -ItemType Directory -Force -Path $configDir,$skillDir | Out-Null
$configFile = Join-Path $configDir 'mcp.json'
$configLock = $null
try {
    $lockDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not $configLock) {
        try { $configLock = [System.IO.File]::Open((Join-Path $configDir 'inoproshop-config.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $lockDeadline) { throw 'Another OMP configuration update is still running.' }
            Start-Sleep -Milliseconds 200
        }
    }
if (Test-Path -LiteralPath $configFile) {
    $config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
} else { $config = [pscustomobject]@{mcpServers = [pscustomobject]@{}} }
if (-not $config.PSObject.Properties['mcpServers']) { $config | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{}) }
$entry = [pscustomobject]@{ type = 'http'; url = $Url; headers = [pscustomobject]@{Authorization = 'INOAI_MCP_AUTHORIZATION'}; timeout = 180000 }
$config.mcpServers | Add-Member -MemberType NoteProperty -Name inoproshop -Value $entry -Force
# Windows PowerShell 5.1 adds a BOM with Set-Content -Encoding UTF8, which
# ohmypi's JSON parser rejects. Use explicit UTF-8 without a BOM on both shells.
$configJson = $config | ConvertTo-Json -Depth 20
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$temporaryConfig = $configFile + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
try {
    [System.IO.File]::WriteAllText($temporaryConfig, $configJson, $utf8NoBom)
    if (Test-Path -LiteralPath $configFile) {
        [System.IO.File]::Replace($temporaryConfig, $configFile, ($configFile + '.inoproshop-backup'))
    } else { [System.IO.File]::Move($temporaryConfig, $configFile) }
} finally {
    if (Test-Path -LiteralPath $temporaryConfig) { Remove-Item -LiteralPath $temporaryConfig }
}
Copy-Item -LiteralPath (Join-Path $serviceRoot 'skills/inoproshop/SKILL.md') -Destination (Join-Path $skillDir 'SKILL.md')
} finally { if ($configLock) { $configLock.Dispose() } }
$contextArguments = @()
if ($ContextFile) {
    $connection = Get-Content -LiteralPath $ContextFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $connection.host_id -or -not $connection.host_instance_id -or $connection.url -ne $Url) { throw 'Invalid launcher host context.' }
    if ([System.IO.Path]::GetFullPath($connection.state_dir) -ne [System.IO.Path]::GetFullPath($StateDir)) { throw 'Launcher context belongs to a different controller.' }
    $hostContext = [pscustomobject]@{host_id=$connection.host_id; host_instance_id=$connection.host_instance_id} | ConvertTo-Json -Compress
    # Session-specific instructions prevent a second OMP window from changing
    # the first window's host selection. This text never contains a token.
    $contextArguments = @('--append-system-prompt', ('InoProShop launcher selected this IDE: ' + $hostContext + '. Before PLC tool calls, verify these exact host identities with ino_list_hosts, then call ino_list_projects for that host. If it closed, ask for a new selection; do not silently use another IDE. Rediscover project_id after the user opens or switches a project. An empty IDE is a valid connection; ask the user to open a project there.'))
}
if ($ConfigureOnly) { Write-Output 'OMP configuration prepared; no model was invoked.'; exit 0 }
$previousAuthorization = $env:INOAI_MCP_AUTHORIZATION
try {
    $env:INOAI_MCP_AUTHORIZATION = 'Bearer ' + $tokenValue
    & omp --cwd $Workspace @OmpArguments @contextArguments
    $exitCode = $LASTEXITCODE
} finally {
    $env:INOAI_MCP_AUTHORIZATION = $previousAuthorization
    $tokenValue = $null
}
exit $exitCode

