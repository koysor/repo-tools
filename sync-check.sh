#!/usr/bin/env bash
# Linux/macOS counterpart of sync-check.ps1: runs repo_status, then asks whether
# to run sync_repos, then install_workspace --update (all from this repo,
# repo-tools) against the folder that holds the sibling repos.
# Without an interactive terminal it skips the prompt and does not sync, unless
# -y/--yes is given.
set -uo pipefail

usage() {
    echo "Usage: $(basename "$0") [-y|--yes]"
    echo "  -y, --yes   run sync_repos without the first prompt (it still asks per repo)"
}

yes=false
for arg in "$@"; do
    case "$arg" in
        -y | --yes) yes=true ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

project="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$project")"

if ! command -v uv > /dev/null; then
    echo "sync-check: uv was not found on PATH" >&2
    exit 2
fi

# Animate only on a real terminal; piped or redirected output gets plain text.
animate=false
if [ -t 1 ] && [ "${TERM:-dumb}" != dumb ]; then
    animate=true
    # Restore the cursor if the script is interrupted mid-animation.
    trap 'printf "\033[?25h"' EXIT
fi

# Truecolor terminals get a smooth 24-bit hue sweep; anything else falls back to
# the 6 basic ANSI colors.
truecolor=false
case "${COLORTERM:-}" in
    truecolor | 24bit) $animate && truecolor=true ;;
esac

# Sets REPLY to the 24-bit foreground escape for a hue in degrees.
hue_escape() {
    local h=$((($1 % 360 + 360) % 360))
    local sector=$((h / 60)) f=$((h % 60)) x r g b
    if ((sector % 2 == 0)); then x=$((255 * f / 60)); else x=$((255 * (60 - f) / 60)); fi
    case $sector in
        0) r=255 g=$x b=0 ;;
        1) r=$x g=255 b=0 ;;
        2) r=0 g=255 b=$x ;;
        3) r=0 g=$x b=255 ;;
        4) r=$x g=0 b=255 ;;
        *) r=255 g=0 b=$x ;;
    esac
    REPLY=$'\033'"[38;2;${r};${g};${b}m"
}

# Prints TEXT in rainbow colors (no newline), starting the sweep at OFFSET.
# With DELAY (seconds), types it out one character at a time.
rainbow() {
    local text=$1 i=${2:-0} delay=${3:-} out='' ch k piece
    local colors=(31 33 32 36 34 35)
    for ((k = 0; k < ${#text}; k++)); do
        ch=${text:k:1}
        if [ "$ch" = ' ' ]; then
            piece=' '
        else
            if $truecolor; then
                hue_escape $((i * 12))
                piece="$REPLY$ch"
            else
                piece=$'\033'"[${colors[i % 6]}m$ch"
            fi
            i=$((i + 1))
        fi
        if [ -n "$delay" ]; then
            printf '%s' "$piece"
            [ "$ch" != ' ' ] && sleep "$delay"
        else
            out+=$piece
        fi
    done
    printf '%s\033[0m' "$out"
}

# Prints a box of text in rainbow colors, then shimmers it in place: the colors
# slide along each row for FRAMES frames.
shimmer() {
    local frames=$1 frame r
    shift
    local art=("$@")
    for r in "${!art[@]}"; do
        rainbow "${art[r]}" "$r"
        echo
    done
    printf '\033[?25l'
    for ((frame = 1; frame <= frames; frame++)); do
        printf '\033[%dA\r' "${#art[@]}"
        for r in "${!art[@]}"; do
            rainbow "${art[r]}" $((frame * 2 + r))
            echo
        done
        sleep 0.02
    done
    printf '\033[?25h'
}

step_state=()
step_text=()
add_step() {
    step_state+=("$1")
    step_text+=("$2")
}

# Final screen: a filling progress bar and a shimmering "all done" box (or a red
# box if a step failed), then one line per step saying what it did.
show_completion() {
    local failed=false i n width=30 bar mark color
    for i in "${!step_state[@]}"; do
        [ "${step_state[i]}" = fail ] && failed=true
    done
    echo
    if $animate; then
        printf '\033[?25l'
        for ((n = 0; n <= width; n++)); do
            bar="  ["
            for ((i = 0; i < width; i++)); do
                if ((i < n)); then bar+='#'; else bar+='.'; fi
            done
            bar+=$(printf '] %3d%%' $((100 * n / width)))
            printf '\r'
            rainbow "$bar" "$n"
            sleep 0.025
        done
        printf '\033[?25h\n\n'
        if $failed; then
            printf '\033[31m%s\n%s\n%s\033[0m\n' \
                '  +------------------+' \
                '  | done, with errors|' \
                '  +------------------+'
        else
            shimmer 50 \
                '  +------------------+' \
                '  |    all done!     |' \
                '  +------------------+'
        fi
        echo
    fi
    for i in "${!step_state[@]}"; do
        case "${step_state[i]}" in
            ok) mark='[ok]' color=32 ;;
            fail) mark='[!!]' color=31 ;;
            *) mark='[--]' color=90 ;;
        esac
        if $animate; then
            printf '  \033[%sm%s %s\033[0m\n' "$color" "$mark" "${step_text[i]}"
            sleep 0.12
        else
            printf '  %s %s\n' "$mark" "${step_text[i]}"
        fi
    done
}

if $animate; then
    shimmer 40 \
        '  +------------------+' \
        '  |    sync-check    |' \
        '  +------------------+'
    rainbow '          repo status + sync' 0 0.008
    echo
else
    echo "== repo status + sync =="
fi
echo

# repo_status exits 1 when something needs attention, which is a report, not a failure.
uv run --project "$project" "$project/repo_status.py" --fetch --root "$root"
if [ $? -eq 2 ]; then
    echo "warning: repo_status could not inspect one or more repos." >&2
    add_step fail "repo status: some repos could not be inspected"
else
    add_step ok "repo status checked"
fi

echo
run=false
if $yes; then
    run=true
elif [ -t 0 ] && [ -t 1 ]; then
    read -r -p "Run sync_repos? [y/N] " answer
    case "$answer" in
        [yY] | [yY][eE][sS]) run=true ;;
    esac
else
    echo "No interactive terminal; skipping sync_repos (use --yes to run it)."
fi

if $run; then
    uv run --project "$project" "$project/sync_repos.py" --root "$root"
    sync_exit=$?
    if [ "$sync_exit" -eq 0 ]; then
        add_step ok "repos synced"
    else
        add_step fail "sync_repos did not finish cleanly (exit $sync_exit)"
    fi
else
    add_step skip "sync skipped"
fi

# Refresh the shared CLAUDE.md copy and install/update the koysor-skills plugin.
echo
uv run --project "$project" "$project/install_workspace.py" --update --root "$root"
install_exit=$?
if [ "$install_exit" -eq 0 ]; then
    add_step ok "workspace CLAUDE.md and koysor-skills plugin updated"
else
    add_step fail "install_workspace failed (exit $install_exit)"
fi

show_completion

# Keep the window open when it was launched by double-clicking or a launcher.
if $animate && [ -t 0 ]; then
    echo
    printf '  \033[90mPress any key to close...\033[0m'
    read -r -s -n 1 _
    echo
fi
exit "$install_exit"
