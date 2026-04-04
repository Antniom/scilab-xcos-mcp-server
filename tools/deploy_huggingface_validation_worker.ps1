param(
    [string]$Remote = "huggingface-worker",
    [string]$RemoteBranch = "main",
    [string]$SourceRef = "HEAD",
    [string]$TempRoot = $env:TEMP,
    [switch]$KeepWorktree,
    [int]$SmokeTestDelaySeconds = 210,
    [string]$HealthcheckUrl = ""
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args,
        [string]$WorkingDirectory = $PSScriptRoot
    )

    Push-Location $WorkingDirectory
    try {
        & git @Args
        if ($LASTEXITCODE -ne 0) {
            throw "git $($Args -join ' ') failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-GitOutput {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args,
        [string]$WorkingDirectory = $PSScriptRoot
    )

    Push-Location $WorkingDirectory
    try {
        $output = & git @Args
        if ($LASTEXITCODE -ne 0) {
            throw "git $($Args -join ' ') failed with exit code $LASTEXITCODE"
        }
        return @($output)
    }
    finally {
        Pop-Location
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TempName = "xcos-hf-worker-" + [guid]::NewGuid().ToString()
$WorktreePath = Join-Path $TempRoot $TempName
$CleanBranch = "hf-worker-clean-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
$ResolvedSourceRef = ((Get-GitOutput -Args @("rev-parse", $SourceRef) -WorkingDirectory $RepoRoot) | Select-Object -First 1).Trim()
$WorkerDockerfile = Join-Path $RepoRoot "Dockerfile.validation-worker"
$ForbiddenPatterns = @(
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.icns",
    "*.wav", "*.ttf", "*.otf",
    "*.xls", "*.xlsx", "*.mat", "*.dat", "*.fig",
    "*.cos", "*.scg", "*.sod", "*.ssp", "*.odt",
    "*.zip", "*.7z", "*.exe", "*.dll", "*.bin"
)

Write-Host "Creating temporary worker worktree at $WorktreePath"
Invoke-Git -Args @("worktree", "add", "--detach", $WorktreePath, $ResolvedSourceRef) -WorkingDirectory $RepoRoot

try {
    Write-Host "Creating orphan worker deployment branch"
    Invoke-Git -Args @("switch", "--orphan", $CleanBranch) -WorkingDirectory $WorktreePath
    Invoke-Git -Args @("checkout", $ResolvedSourceRef, "--", ".") -WorkingDirectory $WorktreePath

    if (-not (Test-Path $WorkerDockerfile)) {
        throw "Worker Dockerfile not found at $WorkerDockerfile"
    }
    Copy-Item $WorkerDockerfile (Join-Path $WorktreePath "Dockerfile") -Force

    $trackedToRemove = New-Object System.Collections.Generic.List[string]
    foreach ($pattern in $ForbiddenPatterns) {
        $matches = Get-GitOutput -Args @("ls-files", $pattern) -WorkingDirectory $WorktreePath
        foreach ($match in $matches) {
            if (-not [string]::IsNullOrWhiteSpace($match)) {
                $trackedToRemove.Add($match)
            }
        }
    }

    $trackedToRemove = $trackedToRemove | Sort-Object -Unique
    if ($trackedToRemove.Count -gt 0) {
        Write-Host "Removing $($trackedToRemove.Count) tracked binary file(s) from worker deployment snapshot"
        foreach ($path in $trackedToRemove) {
            Invoke-Git -Args @("rm", "-f", "--", $path) -WorkingDirectory $WorktreePath
        }
    }

    Invoke-Git -Args @("add", "-A") -WorkingDirectory $WorktreePath
    Invoke-Git -Args @("commit", "-m", "Deploy validation worker snapshot without binary assets") -WorkingDirectory $WorktreePath

    $newCommit = ((Get-GitOutput -Args @("rev-parse", "HEAD") -WorkingDirectory $WorktreePath) | Select-Object -First 1).Trim()
    Write-Host "Force pushing worker snapshot $newCommit to $Remote/$RemoteBranch"
    Invoke-Git -Args @("push", "--force", $Remote, "$CleanBranch`:$RemoteBranch") -WorkingDirectory $WorktreePath

    Write-Host ""
    Write-Host "Worker deployment complete."
    Write-Host "Remote: $Remote/$RemoteBranch"
    Write-Host "Commit: $newCommit"

    if (-not [string]::IsNullOrWhiteSpace($HealthcheckUrl)) {
        Write-Host ""
        Write-Host "Waiting $SmokeTestDelaySeconds seconds for the Hugging Face validation worker Space to rebuild"
        Start-Sleep -Seconds $SmokeTestDelaySeconds
        Write-Host "Checking worker health at $HealthcheckUrl"
        $response = Invoke-RestMethod -Method Get -Uri $HealthcheckUrl
        if ($response.status -ne "ok") {
            throw "Worker healthcheck failed: $($response | ConvertTo-Json -Compress)"
        }
        Write-Host "Worker healthcheck passed."
    }

    if ($KeepWorktree) {
        Write-Host "Temporary worktree preserved at: $WorktreePath"
    }
}
finally {
    if (-not $KeepWorktree) {
        Write-Host "Removing temporary worktree"
        try {
            Invoke-Git -Args @("worktree", "remove", "--force", $WorktreePath) -WorkingDirectory $RepoRoot
        }
        catch {
            Write-Warning ("Failed to remove temporary worktree at {0}: {1}" -f $WorktreePath, $_)
        }
    }
}
