[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status", "logs")]
    [string]$Action = "start"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$projectName = "quality-flow-demo"
$consoleUrl = "http://127.0.0.1:18000/ui/"
$baseArgs = @("compose", "-p", $projectName, "-f", $composeFile)

function Invoke-DockerCommand {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code $LASTEXITCODE. Run '.\qualityflow.ps1 status' or '.\qualityflow.ps1 logs' for diagnostics."
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install and start Docker Desktop, then try again."
}

switch ($Action) {
    "start" {
        & docker info --format "{{.ServerVersion}}" *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Docker Engine is unavailable. Start Docker Desktop, wait until it is ready, then try again."
        }
        Write-Host "Starting QualityFlow..." -ForegroundColor Cyan
        Invoke-DockerCommand ($baseArgs + @(
            "up", "-d", "--build", "--wait", "--wait-timeout", "180"
        ))
        try {
            $ready = Invoke-RestMethod -Uri "http://127.0.0.1:18000/health/ready" -TimeoutSec 5
            if ($ready.status -ne "ready") {
                throw "unexpected readiness response"
            }
        }
        catch {
            throw "QualityFlow containers started, but the API is not ready. Run '.\qualityflow.ps1 logs' to inspect the services."
        }
        Write-Host "QualityFlow is ready: $consoleUrl" -ForegroundColor Green
        Start-Process $consoleUrl
    }
    "stop" {
        Write-Host "Stopping QualityFlow while preserving data volumes..." -ForegroundColor Cyan
        Invoke-DockerCommand ($baseArgs + @("down", "--remove-orphans"))
    }
    "status" {
        Invoke-DockerCommand ($baseArgs + @("ps", "--all"))
    }
    "logs" {
        Invoke-DockerCommand ($baseArgs + @("logs", "--no-color", "--tail", "200"))
    }
}
