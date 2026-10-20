param([string]$Python = 'python', [int]$TimeoutSeconds = 180)
$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'native_verify.py'
& $Python $runner --timeout $TimeoutSeconds
exit $LASTEXITCODE
