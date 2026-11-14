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

function Write-Rainbow([string]$Text, [int]$DelayMs = 15, [int]$Offset = 0, [switch]$NoNewline) {
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
        Write-Host -NoNewline:$NoNewline "$out$esc[0m"
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
    if (-not $NoNewline) { Write-Host '' }
}

# Prints a box of text in rainbow colors, then shimmers it in place: the colors
# slide along each row. The shimmer is skipped if the box scrolled the buffer.
function Show-Shimmer([string[]]$Art, [int]$DurationMs = 1000) {
    $top = [Console]::CursorTop
    foreach ($line in $Art) { Write-Rainbow $line 1 }
    if ($top + $Art.Count -ge [Console]::BufferHeight) { return }
    $cursorWasVisible = [Console]::CursorVisible
    try {
        [Console]::CursorVisible = $false
        $clock = [Diagnostics.Stopwatch]::StartNew()
        for ($frame = 1; $clock.ElapsedMilliseconds -lt $DurationMs; $frame++) {
            [Console]::SetCursorPosition(0, $top)
            for ($r = 0; $r -lt $Art.Count; $r++) {
                Write-Rainbow $Art[$r] 0 ($frame * 2 + $r)
            }
            Start-Sleep -Milliseconds 5
        }
    } finally {
        [Console]::CursorVisible = $cursorWasVisible
    }
}

# Final screen: a filling progress bar and a shimmering "all done" box (or a red
# box if a step failed), then one line per step saying what it did.
function Show-Completion([object[]]$Steps) {
    $failed = @($Steps | Where-Object { $_.State -eq 'fail' }).Count -gt 0
    Write-Host ''
    if ($animate) {
        $width = 30
        $cursorWasVisible = [Console]::CursorVisible
        try {
            [Console]::CursorVisible = $false
            for ($n = 0; $n -le $width; $n++) {
                $pct = '{0,3}%' -f [int](100 * $n / $width)
                $bar = '  [' + ('#' * $n) + ('.' * ($width - $n)) + '] ' + $pct
                Write-Host -NoNewline "`r"
                Write-Rainbow $bar 0 $n -NoNewline
                Start-Sleep -Milliseconds 25
            }
        } finally {
            [Console]::CursorVisible = $cursorWasVisible
        }
        Write-Host ''
        Write-Host ''
        if ($failed) {
            Write-Host '  +------------------+' -ForegroundColor Red
            Write-Host '  | done, with errors|' -ForegroundColor Red
            Write-Host '  +------------------+' -ForegroundColor Red
        } else {
            Show-Shimmer @(
                '  +------------------+'
                '  |    all done!     |'
                '  +------------------+'
            ) 1200
        }
        Write-Host ''
    }
    foreach ($step in $Steps) {
        $mark, $color = switch ($step.State) {
            'ok' { '[ok]', 'Green' }
            'fail' { '[!!]', 'Red' }
            default { '[--]', 'DarkGray' }
        }
        Write-Host "  $mark $($step.Text)" -ForegroundColor $color
        if ($animate) { Start-Sleep -Milliseconds 120 }
    }
}

if ($animate) {
    Show-Shimmer @(
        '  +------------------+'
        '  |    sync-check    |'
        '  +------------------+'
    )
    Write-Rainbow '          repo status + sync' 8
    Write-Host ''
}

$steps = @()

# repo_status exits 1 when something needs attention, which is a report, not a failure.
uv run --project $project (Join-Path $project 'repo_status.py') --fetch --root $root
$steps += if ($LASTEXITCODE -eq 2) {
    @{ State = 'fail'; Text = 'repo status: some repos could not be inspected' }
} else {
    @{ State = 'ok'; Text = 'repo status checked' }
}
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
    $steps += if ($LASTEXITCODE -eq 0) {
        @{ State = 'ok'; Text = 'repos synced' }
    } else {
        @{ State = 'fail'; Text = "sync_repos did not finish cleanly (exit $LASTEXITCODE)" }
    }
} else {
    $steps += @{ State = 'skip'; Text = 'sync skipped' }
}

# Refresh the shared CLAUDE.md copy and install/update the koysor-skills plugin.
Write-Host ''
uv run --project $project (Join-Path $project 'install_workspace.py') --update --root $root
$installExit = $LASTEXITCODE
$steps += if ($installExit -eq 0) {
    @{ State = 'ok'; Text = 'workspace CLAUDE.md and koysor-skills plugin updated' }
} else {
    @{ State = 'fail'; Text = "install_workspace failed (exit $installExit)" }
}

Show-Completion $steps

# Keep the window open (it closes on exit when sync-check.cmd is double-clicked).
if ($animate -and -not [Console]::IsInputRedirected) {
    Write-Host ''
    Write-Host -NoNewline '  Press any key to close...' -ForegroundColor DarkGray
    try { [void][Console]::ReadKey($true) } catch { }
    Write-Host ''
}
exit $installExit
