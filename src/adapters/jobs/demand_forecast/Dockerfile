# SPDX-License-Identifier: MIT-0
#
# The demand-forecast job image (design §4b), adapted from the source engine's inference image.
# Python 3.14 on the pinned linux/amd64 manifest of python:3.14-slim (approval D-2): the platform
# manifest, not the tag or index, so a rebuild can't pick new bytes. ml.m5 instances are x86_64.
# Model weights are not baked in: the Processing job mounts the JumpStart artifact read-only at
# /opt/ml/processing/model and the job checks its digest before loading.
# Build context: the repository root, narrowed to image-paths.txt (the CDK asset's exclude list).
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/forecast/src

WORKDIR /opt/forecast

# Exactly the exported, hashed closure of the forecasting group (approval D-1); no uv, pyproject or
# uv.lock enters the image. The lock digest in every run report is this file's SHA-256.
COPY src/adapters/jobs/demand_forecast/requirements.lock /opt/forecast/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /opt/forecast/requirements.lock

# The runtime set: the policy, the boundary schemas and the listed modules, at their repository paths.
COPY . /opt/forecast/

# The job runs as `forecast` (uid 10001). The image's user stays root only so the entrypoint can hand
# SageMaker's output mounts to it, then drop to it with setpriv (util-linux, in the base image).
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin forecast \
    && install -m 0755 /opt/forecast/src/adapters/jobs/demand_forecast/entrypoint.sh /usr/local/bin/forecast-entrypoint

HEALTHCHECK NONE
ENTRYPOINT ["/usr/local/bin/forecast-entrypoint"]
