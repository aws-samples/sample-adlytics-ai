#!/bin/bash
# SPDX-License-Identifier: MIT-0
#
# The conversation function's entry point. Declared as the Lambda handler, because under the Lambda
# Web Adapter the handler is a startup script rather than a Python symbol: the adapter runs as an
# extension, `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap` hands it control at init, and it execs this to
# bring up the server it then forwards each invocation to.
#
# Lives beside the module it starts, and `scripts/build-lambdas.sh` copies it to the root of the
# deployment package because that is where Lambda resolves a script handler from.
#
# `set -e` is deliberate but almost decorative: the last line is an `exec`, so this script either
# fails before it or ceases to exist. What matters is that a failure here is an init failure, which
# means the adapter's readiness check never passes and the container never takes a request.
set -euo pipefail

# `python3` rather than `python`. Both exist in the managed runtime today; only one is guaranteed by
# the image's contract, and a missing interpreter presents as an init timeout rather than a not-found.
#
# PYTHONPATH is stated in full rather than appended to. Three roots, and each is load-bearing:
#   $LAMBDA_TASK_ROOT   our own packages — adapters, domain, ports — and the contracts beside them
#   /opt/python         the dependency layer this function shares with the rest of the domain
#   $LAMBDA_RUNTIME_DIR boto3 and botocore, which the runtime provides and the layer deliberately omits
#
# Appending would inherit whatever the wrapper left set, so the import order would depend on the
# adapter's implementation rather than on this file.
#
# PYTHONUNBUFFERED because the log lines are the only view into a streaming turn. Buffered stdout
# arrives after the invocation ends, which is precisely when it stops being useful.
PYTHONPATH="${LAMBDA_TASK_ROOT}:/opt/python:${LAMBDA_RUNTIME_DIR}" \
  PYTHONUNBUFFERED=1 \
  exec python3 -m adapters.inbound.conversation.server
