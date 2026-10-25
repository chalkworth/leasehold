#!/bin/sh
# leasehold's Claude Code hooks, without starting Python for a Bash command that cannot need
# a lease. Usage: prefilter.sh pre|post|start, with the hook's event JSON on stdin.
#
# pre: exits 0 at once when a lease is held already (LEASEHOLD_LEASE_ID), or when no word of
# the command is in the word list `leasehold commands --programs --write` writes (and the
# Python hook keeps in step). A word is a run of A-Z a-z 0-9 . _ + -, read after quotes and
# backslashes are dropped, so `.venv/bin/pytest`, `"py"test` and `sh -c 'cargo test'` all show
# their program. With no list, or no command found in the event, the Python hook decides.
#
# post: exits 0 at once when `pre` noted no lease for this tool call
# ($XDG_RUNTIME_DIR/leasehold-hooks-<uid>/cc.<tool_use_id>); `pre` takes no lease it cannot
# note there. Without XDG_RUNTIME_DIR, or with an id `pre` would have rewritten, the Python
# hook decides.
#
# start (SessionStart): writes the Bash tool's timeout, in seconds, to the session's
# CLAUDE_ENV_FILE as LEASEHOLD_TOOL_TIMEOUT_S: BASH_DEFAULT_TIMEOUT_MS when set, else 120. A
# PATH shim inside a Bash command then waits in line at most half of it, so the agent hears
# why its command did not run before the tool's timeout ends it. A value already set is kept.
# Then the Python hook adds the line that puts a leased command's shell in its lease's scope
# before the command runs. For a command with no lease, that line costs one directory test.
#
# Either way, without `leasehold` on PATH it does nothing.

stage=${1:-pre}
command -v leasehold >/dev/null 2>&1 || exit 0
event=$(cat)

hook() {
    printf '%s\n' "$event" | leasehold hook claude-code "$stage"
    exit 0
}

case $stage in
start)
    [ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
    if [ -z "${LEASEHOLD_TOOL_TIMEOUT_S:-}" ]; then
        ms=${BASH_DEFAULT_TIMEOUT_MS:-120000}
        case $ms in '' | *[!0-9]*) ms=120000 ;; esac
        [ "$ms" -gt 0 ] || ms=120000
        printf 'export LEASEHOLD_TOOL_TIMEOUT_S=%s\n' "$((ms / 1000))" >>"$CLAUDE_ENV_FILE"
    fi
    hook
    ;;
pre)
    [ -n "${LEASEHOLD_LEASE_ID:-}" ] && exit 0
    if [ -n "${XDG_CACHE_HOME:-}" ]; then
        list=$XDG_CACHE_HOME/leasehold/programs
    elif [ -n "${HOME:-}" ]; then
        list=$HOME/.cache/leasehold/programs
    else
        hook
    fi
    [ -f "$list" ] || hook
    # The command's JSON string, unescaped just enough to split it into words: an escaped
    # backslash goes (as the shell drops one), \uXXXX and \n \r \t \b \f end a word, and the
    # remaining backslashes and quotes go.
    words=$(printf '%s\n' "$event" | LC_ALL=C sed -n \
        -e '/"command"[[:space:]]*:/!d' \
        -e 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"\\]*\(\\.[^"\\]*\)*\)".*/\1/' \
        -e 's/\\\\//g' \
        -e 's/\\u[0-9A-Fa-f]\{4\}/ /g' \
        -e 's/\\[nrtbf]/ /g' \
        -e "s/[\\\\\"']//g" \
        -e p)
    [ -n "$words" ] || hook
    printf '%s\n' "$words" | LC_ALL=C tr -cs 'A-Za-z0-9._+-' '\n' | grep -qxF -f "$list" && hook
    exit 0
    ;;
post)
    [ -n "${XDG_RUNTIME_DIR:-}" ] || hook
    tool_id=$(printf '%s\n' "$event" | LC_ALL=C sed -n \
        's/.*"tool_use_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_.:-]*\)".*/\1/p')
    [ -n "$tool_id" ] && [ "${#tool_id}" -le 61 ] || hook
    [ -e "$XDG_RUNTIME_DIR/leasehold-hooks-$(id -u)/cc.$tool_id" ] && hook
    exit 0
    ;;
esac
hook
