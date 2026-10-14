#!/bin/sh
# One job of the suite: note how many jobs are running now (this one included) in $LOG, work for
# a moment, then finish. $RUNNING is a directory with one file per running job.
set -eu
touch "$RUNNING/$1"
ls "$RUNNING" | wc -l >>"$LOG"
sleep 0.3
rm "$RUNNING/$1"
