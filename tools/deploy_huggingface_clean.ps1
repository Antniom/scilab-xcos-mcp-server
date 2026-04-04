param(
    [string]$Remote = "huggingface",
    [string]$RemoteBranch = "main",
    [string]$SourceRef = "HEAD",
    [string]$TempRoot = $env:TEMP,
    [switch]$KeepWorktree,
    [switch]$SkipRemoteSmokeTest,
    [string]$SmokeTestMcpUrl = "https://notsn-scilab-xcos-mcp-server.hf.space/mcp",
    [string]$SmokeTestFixturePath = "",
    [string]$SmokeTestReadyUrl = "https://notsn-scilab-xcos-mcp-server.hf.space/workflow-ui/deploy_marker.json",
    [int]$SmokeTestWaitTimeoutSeconds = 900,
    [int]$SmokeTestPollIntervalSeconds = 10
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

function Wait-ForDeployMarker {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedSourceRef,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedDeployCommit,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)]
        [int]$PollIntervalSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $rawResponse = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec ([Math]::Min($PollIntervalSeconds, 30))
            $rawContent = [string]$rawResponse.Content
            if (-not [string]::IsNullOrWhiteSpace($rawContent)) {
                $normalizedContent = $rawContent.TrimStart([char]0xFEFF).Trim()
                $response = $normalizedContent | ConvertFrom-Json
                $markerSourceRef = [string]$response.source_ref
                $markerDeployCommit = [string]$response.deploy_commit
                if ($markerSourceRef -eq $ExpectedSourceRef -and $markerDeployCommit -eq $ExpectedDeployCommit) {
                    Write-Host "Deployment marker is live at $Url"
                    return
                }
                Write-Host ("Deployment marker not ready yet. Expected source_ref={0}, deploy_commit={1}; got source_ref={2}, deploy_commit={3}" -f $ExpectedSourceRef, $ExpectedDeployCommit, $markerSourceRef, $markerDeployCommit)
            }
            else {
                Write-Host "Deployment marker endpoint returned no body yet: $Url"
            }
        }
        catch {
            Write-Host ("Deployment marker not ready yet at {0}: {1}" -f $Url, $_.Exception.Message)
        }

        Start-Sleep -Seconds $PollIntervalSeconds
    }

    throw "Timed out after $TimeoutSeconds seconds waiting for deployment marker at $Url"
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SmokeTestScript = Join-Path $RepoRoot "tools\remote_hf_smoke_test.py"
$TempName = "xcos-hf-clean-" + [guid]::NewGuid().ToString()
$WorktreePath = Join-Path $TempRoot $TempName
$CleanBranch = "hf-space-clean-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
$ResolvedSourceRef = ((Get-GitOutput -Args @("rev-parse", $SourceRef) -WorkingDirectory $RepoRoot) | Select-Object -First 1).Trim()
$DeployIssuedAt = (Get-Date).ToUniversalTime().ToString("o")
$ForbiddenPatterns = @(
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp", "*.icns",
    "*.wav", "*.ttf", "*.otf",
    "*.xls", "*.xlsx", "*.mat", "*.dat", "*.fig",
    "*.cos", "*.scg", "*.sod", "*.ssp", "*.odt",
    "*.zip", "*.7z", "*.exe", "*.dll", "*.bin"
)

Write-Host "Creating temporary worktree at $WorktreePath"
Invoke-Git -Args @("worktree", "add", "--detach", $WorktreePath, $ResolvedSourceRef) -WorkingDirectory $RepoRoot

try {
    Write-Host "Creating orphan deployment branch"
    Invoke-Git -Args @("switch", "--orphan", $CleanBranch) -WorkingDirectory $WorktreePath
    Invoke-Git -Args @("checkout", $ResolvedSourceRef, "--", ".") -WorkingDirectory $WorktreePath

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
        Write-Host "Removing $($trackedToRemove.Count) tracked binary file(s) from deployment snapshot"
        foreach ($path in $trackedToRemove) {
            Invoke-Git -Args @("rm", "-f", "--", $path) -WorkingDirectory $WorktreePath
        }
    }
    else {
        Write-Host "No forbidden tracked binaries found in deployment snapshot"
    }

    $markerDirectory = Join-Path $WorktreePath "ui"
    if (-not (Test-Path $markerDirectory)) {
        New-Item -ItemType Directory -Path $markerDirectory | Out-Null
    }

    Invoke-Git -Args @("add", "-A") -WorkingDirectory $WorktreePath
    Invoke-Git -Args @("commit", "-m", "Deploy Space snapshot without binary assets") -WorkingDirectory $WorktreePath

    $newCommit = ((Get-GitOutput -Args @("rev-parse", "HEAD") -WorkingDirectory $WorktreePath) | Select-Object -First 1).Trim()
    $markerPath = Join-Path $markerDirectory "deploy_marker.json"
    $markerPayload = [ordered]@{
        source_ref = $ResolvedSourceRef
        deploy_commit = $newCommit
        deployed_at_utc = $DeployIssuedAt
        remote = $Remote
        remote_branch = $RemoteBranch
    } | ConvertTo-Json
    Set-Content -Path $markerPath -Value $markerPayload -Encoding UTF8
    Invoke-Git -Args @("add", "--", "ui/deploy_marker.json") -WorkingDirectory $WorktreePath
    Invoke-Git -Args @("commit", "--amend", "--no-edit") -WorkingDirectory $WorktreePath
    $newCommit = ((Get-GitOutput -Args @("rev-parse", "HEAD") -WorkingDirectory $WorktreePath) | Select-Object -First 1).Trim()

    Write-Host "Force pushing clean snapshot $newCommit to $Remote/$RemoteBranch"
    Invoke-Git -Args @("push", "--force", $Remote, "$CleanBranch`:$RemoteBranch") -WorkingDirectory $WorktreePath

    Write-Host ""
    Write-Host "Hugging Face deployment complete."
    Write-Host "Remote: $Remote/$RemoteBranch"
    Write-Host "Commit: $newCommit"

    if (-not $SkipRemoteSmokeTest) {
        if ([string]::IsNullOrWhiteSpace($SmokeTestFixturePath)) {
            $SmokeTestFixturePath = Join-Path $RepoRoot "pendulo_simples_fiel_raw.xcos"
        }
        if (-not (Test-Path $PythonExe)) {
            throw "Python executable not found at $PythonExe"
        }
        if (-not (Test-Path $SmokeTestScript)) {
            throw "Remote smoke test script not found at $SmokeTestScript"
        }
        if (-not (Test-Path $SmokeTestFixturePath)) {
            throw "Remote smoke test fixture not found at $SmokeTestFixturePath"
        }

        Write-Host ""
        Write-Host "Waiting for live deployment marker at $SmokeTestReadyUrl"
        Wait-ForDeployMarker -Url $SmokeTestReadyUrl -ExpectedSourceRef $ResolvedSourceRef -ExpectedDeployCommit $newCommit -TimeoutSeconds $SmokeTestWaitTimeoutSeconds -PollIntervalSeconds $SmokeTestPollIntervalSeconds

        Write-Host ""
        Write-Host "Running remote MCP smoke test against $SmokeTestMcpUrl"
        Push-Location $RepoRoot
        try {
            & $PythonExe $SmokeTestScript --mcp-url $SmokeTestMcpUrl --fixture-path $SmokeTestFixturePath
            if ($LASTEXITCODE -ne 0) {
                throw "Remote smoke test failed with exit code $LASTEXITCODE"
            }
        }
        finally {
            Pop-Location
        }
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
