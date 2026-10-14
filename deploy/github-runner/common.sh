# Shared by job-started.sh and job-completed.sh (sourced, not run).
# shellcheck shell=sh

LEASEHOLD=${LEASEHOLD:-leasehold}

# say MESSAGE: one line to the job's log (stderr) and, when logger exists, to the journal.
say() {
	printf 'leasehold runner hook: %s\n' "$1" >&2
	if command -v logger >/dev/null 2>&1; then
		logger -t leasehold-runner -p user.warning -- "$1" 2>/dev/null || true
	fi
}

# lease_id: the same id in both hooks of one job, from what the runner sets for them; at most
# 64 of [A-Za-z0-9_.:-], as leasehold requires.
lease_id() {
	printf 'gh.%s.%s.%s.%s' "${RUNNER_NAME:-runner}" "${GITHUB_RUN_ID:-0}" \
		"${GITHUB_RUN_ATTEMPT:-1}" "${GITHUB_JOB:-job}" |
		tr -c 'A-Za-z0-9_.:-' '_' | cut -c1-64
}

# have_leasehold: whether the leasehold command is installed at all.
have_leasehold() {
	command -v "$LEASEHOLD" >/dev/null 2>&1
}
