param(
    [string]$StateDir,
    [string]$ProjectRoot,
    [string]$Neo4jCredentials,
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$serviceRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $serviceRoot)
if (-not $StateDir) { $StateDir = if ($env:INO_MCP_STATE) { $env:INO_MCP_STATE } else { Join-Path $workspaceRoot 'work/mcp_p0/state' } }
if (-not $ProjectRoot) { $ProjectRoot = if ($env:INO_MCP_PROJECTS) { $env:INO_MCP_PROJECTS } else { Join-Path $workspaceRoot 'work/mcp_p0/projects' } }
if (-not $Neo4jCredentials) { $Neo4jCredentials = Join-Path $workspaceRoot 'work/mcp_p0/neo4j/credentials.json' }
$python = Join-Path $serviceRoot '.venv/Scripts/python.exe'
if (Test-Path -LiteralPath (Join-Path $workspaceRoot 'offline')) { $python = Join-Path $workspaceRoot 'runtime/python/python.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Run scripts/Install.ps1 first.' }
$previousNeo4j = @($env:NEO4J_URI, $env:NEO4J_USERNAME, $env:NEO4J_PASSWORD)
$previousPythonPath = $env:PYTHONPATH
$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:PYTHONPATH = Join-Path $serviceRoot 'src'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    if (Test-Path -LiteralPath $Neo4jCredentials) {
        $graphCredentials = Get-Content -LiteralPath $Neo4jCredentials -Raw | ConvertFrom-Json
        if (-not $graphCredentials.uri -or -not $graphCredentials.username -or -not $graphCredentials.password) { throw 'Invalid local graph credentials configuration.' }
        $env:NEO4J_URI = $graphCredentials.uri
        $env:NEO4J_USERNAME = $graphCredentials.username
        $env:NEO4J_PASSWORD = $graphCredentials.password
    }
    & $python -m inoproshop_mcp serve --state-dir $StateDir --project-root $ProjectRoot --port $Port
    $exitCode = $LASTEXITCODE
} finally {
    $env:NEO4J_URI = $previousNeo4j[0]
    $env:NEO4J_USERNAME = $previousNeo4j[1]
    $env:NEO4J_PASSWORD = $previousNeo4j[2]
    $graphCredentials = $null
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
}
exit $exitCode

