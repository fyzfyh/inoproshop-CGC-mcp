param([switch]$SkipTests, [switch]$WithoutGraph, [string]$TestReport)
$ErrorActionPreference = 'Stop'
$serviceRoot = Split-Path -Parent $PSScriptRoot
Push-Location $serviceRoot
try {
    $syncArgs = @('sync','--frozen','--extra','test')
    if (-not $WithoutGraph) { $syncArgs += @('--extra','cgc') }
    & uv @syncArgs
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if (-not $SkipTests) {
        # Keep nested snapshot/checkpoint test artifacts below Windows path
        # limits even when this source checkout was extracted deeply.
        $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('ino-mcp-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
        if (Test-Path -LiteralPath $testRoot) { throw 'Test temporary directory already exists; run installation again to allocate a fresh path.' }
        $pytestArgs = @('-m','pytest','tests','-q','--basetemp',$testRoot)
        if ($TestReport) { $pytestArgs += @('--junitxml',$TestReport) }
        & '.venv/Scripts/python.exe' @pytestArgs
        if ($LASTEXITCODE -ne 0) { throw 'Local verification failed.' }
    }
    Write-Output 'Installation complete. Start the controller with scripts/Start-Service.ps1.'
} finally { Pop-Location }

