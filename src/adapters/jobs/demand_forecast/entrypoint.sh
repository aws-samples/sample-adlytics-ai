#!/bin/sh
# SPDX-License-Identifier: MIT-0
#
# The demand-forecast job's entrypoint (adapted from the source engine). It runs as root only to hand
# the job's writable paths to the unprivileged user `forecast`: SageMaker mounts the Processing output
# directory and /opt/ml/output when the job starts, owned by root, so ownership set in the image
# doesn't survive. Then it replaces itself with the job, run as `forecast` with no capabilities and no
# way to regain privileges. No job code, model or data runs as root. Any failure here exits non-zero
# before the job starts.
set -eu
for path in /opt/ml/processing/output /opt/ml/output; do
  mkdir -p "$path"
  chown -R forecast:forecast "$path"
done
# A private temporary directory: loading the model imports a torch module that makes one, and the
# image's /tmp can be unusable to an unprivileged user under SageMaker.
JOB_TMPDIR=/home/forecast/tmp
install -d -o forecast -g forecast -m 0700 "$JOB_TMPDIR"
HOME=/home/forecast TMPDIR="$JOB_TMPDIR" exec setpriv --reuid=forecast --regid=forecast --init-groups \
  --no-new-privs --inh-caps=-all python -m adapters.jobs.demand_forecast.entry "$@"
