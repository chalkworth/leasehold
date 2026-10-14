"""The exit codes `leasehold` commands share, beyond a wrapped command's own.

| Code | Meaning |
|---|---|
| 69 | the daemon could not be reached (`EX_UNAVAILABLE`) |
| 75 | still waiting at `--timeout`: busy, not failed (`EX_TEMPFAIL`) |
| 76 | the lease was revoked (or, for `lend`, lost) |
| 77 | refused: the request could never be granted, or the daemon answered it with an error |
| 127 | the command could not be started |
"""

EXIT_UNAVAILABLE = 69
EXIT_BUSY = 75
EXIT_REVOKED = 76
EXIT_REFUSED = 77
EXIT_NOT_STARTED = 127
