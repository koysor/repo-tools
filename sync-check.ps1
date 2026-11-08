# Runs repo_status, then asks whether to run sync_repos, then install_workspace
# (all from this repo, repo-tools) against the folder that holds the sibling repos.
# Without a usable console (-NonInteractive, piped input) it skips the prompt and
# does not sync, unless -Yes is given.
param(
    [switch]$Yes
)

$project = $PSScriptRoot
$root = Split-Path $PSScriptRoot -Parent

# Animate only on a real console; piped or redirected output gets plain text.
$animate = [Environment]::UserInteractive -and -not [Console]::IsOutputRedirected

# Windows Terminal (and other truecolor terminals) get a smooth, fully saturated
# 24-bit hue sweep; anything else falls back to the 16 console colors.
$truecolor = $animate -and ($env:WT_SESSION -or $env:COLORTERM -eq 'truecolor')
$esc = [char]27

function Get-HueEscape([double]$Hue) {
    $h = (($Hue % 360) + 360) % 360 / 60
    $x = [int](255 * (1 - [math]::Abs($h % 2 - 1)))
    $r, $g, $b = switch ([int][math]::Floor($h)) {
        0 { 255, $x, 0 }
        1 { $x, 255, 0 }
        2 { 0, 255, $x }
        3 { 0, $x, 255 }
        4 { $x, 0, 255 }
        default { 255, 0, $x }
    }
    "$esc[38;2;${r};${g};${b}m"
}

function Write-Rainbow([string]$Text, [int]$DelayMs = 15, [int]$Offset = 0) {
    $i = $Offset
    if ($truecolor) {
        $out = ''
        foreach ($ch in $Text.ToCharArray()) {
            if ($ch -eq ' ') { $out += ' '; continue }
            $piece = (Get-HueEscape ($i++ * 12)) + $ch
            if ($DelayMs -gt 0) {
                Write-Host -NoNewline $piece
                Start-Sleep -Milliseconds $DelayMs
            } else {
                $out += $piece
            }
        }
        Write-Host "$out$esc[0m"
        return
    }
    $colors = 'Red', 'Yellow', 'Green', 'Cyan', 'Blue', 'Magenta'
    foreach ($ch in $Text.ToCharArray()) {
        if ($ch -eq ' ') {
            Write-Host -NoNewline ' '
        } else {
            Write-Host -NoNewline $ch -ForegroundColor $colors[$i++ % $colors.Count]
            if ($DelayMs -gt 0) { Start-Sleep -Milliseconds $DelayMs }
        }
    }
    Write-Host ''
}

if ($animate) {
    $art = @(
        '  +------------------+'
        '  |    sync-check    |'
        '  +------------------+'
    )
    $top = [Console]::CursorTop
    foreach ($line in $art) { Write-Rainbow $line 1 }

    # Shimmer: redraw the banner in place with the colors sliding along each row.
    # Skipped if the banner scrolled off the top of the buffer.
    if ($top + $art.Count -lt [Console]::BufferHeight) {
        $cursorWasVisible = [Console]::CursorVisible
        try {
            [Console]::CursorVisible = $false
            $clock = [Diagnostics.Stopwatch]::StartNew()
            for ($frame = 1; $clock.ElapsedMilliseconds -lt 1000; $frame++) {
                [Console]::SetCursorPosition(0, $top)
                for ($r = 0; $r -lt $art.Count; $r++) {
                    Write-Rainbow $art[$r] 0 ($frame * 2 + $r)
                }
                Start-Sleep -Milliseconds 5
            }
        } finally {
            [Console]::CursorVisible = $cursorWasVisible
        }
    }
    Write-Rainbow '          repo status + sync' 8
    Write-Host ''
}

# repo_status exits 1 when something needs attention, which is a report, not a failure.
uv run --project $project (Join-Path $project 'repo_status.py') --fetch --root $root
if ($LASTEXITCODE -eq 2) {
    Write-Warning 'repo_status could not inspect one or more repos.'
}

Write-Host ''
if ($Yes) {
    $run = $true
} else {
    try {
        if ($animate) { Write-Rainbow 'Ready to sync?' 12 }
        $run = (Read-Host 'Run sync_repos? [y/N]') -match '^(y|yes)$'
    } catch {
        Write-Host 'No interactive console; skipping sync_repos (use -Yes to run it).'
        $run = $false
    }
}

if ($run) {
    uv run --project $project (Join-Path $project 'sync_repos.py') --root $root
}

# Refresh the shared CLAUDE.md copy and install/update the koysor-skills plugin.
Write-Host ''
uv run --project $project (Join-Path $project 'install_workspace.py') --update --root $root
