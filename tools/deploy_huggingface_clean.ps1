param(
    [string]$Remote = "huggingface",
    [string]$RemoteBranch = "main",
    [string]$SourceRef = "HEAD",
    [string]$TempRoot = $env:TEMP,
    [switch]$KeepWorktree
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
$TempName = "xcos-hf-clean-" + [guid]::NewGuid().ToString()
$WorktreePath = Join-Path $TempRoot $TempName
$CleanBranch = "hf-space-clean-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
$ResolvedSourceRef = ((Get-GitOutput -Args @("rev-parse", $SourceRef) -WorkingDirectory $RepoRoot) | Select-Object -First 1).Trim()
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

    Invoke-Git -Args @("add", "-A") -WorkingDirectory $WorktreePath
    Invoke-Git -Args @("commit", "-m", "Deploy Space snapshot without binary assets") -WorkingDirectory $WorktreePath

    $newCommit = ((Get-GitOutput -Args @("rev-parse", "HEAD") -WorkingDirectory $WorktreePath) | Select-Object -First 1).Trim()
    Write-Host "Force pushing clean snapshot $newCommit to $Remote/$RemoteBranch"
    Invoke-Git -Args @("push", "--force", $Remote, "$CleanBranch`:$RemoteBranch") -WorkingDirectory $WorktreePath

    Write-Host ""
    Write-Host "Hugging Face deployment complete."
    Write-Host "Remote: $Remote/$RemoteBranch"
    Write-Host "Commit: $newCommit"
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
