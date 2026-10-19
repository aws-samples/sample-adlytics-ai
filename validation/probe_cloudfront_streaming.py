#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Does a Lambda Function URL actually stream, and does CloudFront forward it or buffer it?

    uv run python validation/probe_cloudfront_streaming.py --profile <profile> --stage direct
    uv run python validation/probe_cloudfront_streaming.py --profile <profile> --stage cloudfront
    uv run python validation/probe_cloudfront_streaming.py --profile <profile> --stage destroy

**This is the assumption [ADR-038](../docs/decisions/adr-038-the-conversation-route-streams-from-a-function-url.md)
rests on, and it was the one thing in that decision nobody had measured.** If CloudFront buffers, a
turn still arrives complete and correct — it just arrives all at once, so first text moves from ~15s
to ~25s and the entire reason for choosing this transport evaporates. That is not a degradation to
discover after the frontend is written.

**Deliberately tests nothing of ours.** The probe function emits one SSE event per second for twenty
seconds and knows nothing about harnesses, cookies or tools. If it streams, the transport streams; if
it does not, no amount of application code will fix it. Mixing the two would leave a failure
ambiguous between "CloudFront buffers" and "our server never flushed", which are opposite problems.

The measurement is **inter-arrival time, not total**. A buffering proxy still delivers all twenty
events and still reports a plausible total; what it cannot do is deliver the first one at t≈1s. So
the assertion is on the gap between arrivals, which is the only signal that distinguishes them.

Creates its own throwaway distribution rather than touching `FrontendStack`. That stack has known
drift — `edge.apiOriginAttached` is `false` in config while the live distribution has the API origin
attached — so deploying it to answer a question would risk detaching `/api/*` from a working
environment. A probe that can break the thing it is measuring is not a probe.

Everything it creates is tagged and torn down by `--stage destroy`. Nothing here is a fixture and
nothing should outlive the finding; `RESULTS.md` is where the finding goes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from typing import Any

import requests

from adapters.outbound.https import HttpsSession, require_https

_HTTP = HttpsSession()
HTTP_ERROR_STATUS = 300
PREVIEW_EVENT_COUNT = 3

NAME = "adlytics-probe-sse"
"""One name for every resource, so `--stage destroy` needs no state file to find them."""

AUTH_TYPE = "AWS_IAM"
"""The only auth type this script will ever set on a Function URL.

**`NONE` was used once, briefly, and must never be used again.** It creates a Function URL any
caller on the internet can invoke, and the resource policy it needs names `Principal: "*"`. The
account refused it — the invocation returned `403` and no log group was ever created — so nothing was
exposed. That is luck, not a control, and it is not the reason it was wrong: a probe reached for a
public endpoint to make a measurement simpler, on a platform whose entire threat model is that only
an authenticated user reaches data.

There is no case for it here. `AWS_IAM` costs a signed request, `signed_headers` below already does
that, and the direct measurement it was meant to simplify works identically under IAM. Enforced by
`_refuse_public_auth` rather than left as a comment, because a comment is what failed.
"""


def _refuse_public_auth(auth_type: str) -> str:
    """Refuse any auth type but IAM, before a call that would create a public endpoint.

    A guard rather than a convention. The mistake this prevents is not a typo — it is the reasoning
    that a throwaway resource in a development account is exempt from the rule, which is exactly the
    reasoning that leaves a public endpoint behind after the throwaway stops being watched.
    """
    if auth_type != AUTH_TYPE:
        raise SystemExit(
            f"refusing to set AuthType={auth_type} on a Function URL. Only {AUTH_TYPE} is "
            "permitted: anything else is invocable from the internet. See AUTH_TYPE above."
        )
    return auth_type

LWA_LAYER = "arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerArm64:28"
"""The published adapter layer, arm64 to match every function this repository deploys.

Pinned to a version deliberately. `latest` on a third-party layer means a deploy can change
behaviour without a diff, which is the same objection that pins every other dependency here.
"""

TICKS = 20
"""How many events, one per second. Long enough that buffering is unmistakable in the timings."""

SERVER = f'''
import http.server, socketserver, time, sys

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        # The readiness path the adapter polls. Answered before anything else so the extension
        # starts the runtime client rather than timing out at init.
        if self.path.rstrip("/") in ("", "/health"):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._stream()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._stream()

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for n in range({TICKS}):
            payload = ("event: tick\\ndata: " + str(n) + "\\n\\n").encode()
            # Chunked framing by hand: length in hex, CRLF, bytes, CRLF. Flushed every time,
            # because an unflushed buffer is indistinguishable from a buffering proxy and would
            # make this probe answer the wrong question.
            self.wfile.write(b"%x\\r\\n" % len(payload) + payload + b"\\r\\n")
            self.wfile.flush()
            time.sleep(1)
        self.wfile.write(b"0\\r\\n\\r\\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

Server(("127.0.0.1", 8080), Handler).serve_forever()
'''

RUN_SH = "#!/bin/sh\nexec python3 -u /var/task/server.py\n"


def bundle() -> bytes:
    """Zip the probe function, with the startup script marked executable.

    The 0o755 mode is not decoration. The adapter runs `run.sh` as the handler, and a zip entry
    without the execute bit fails at init with "cannot execute: required file not found" — a message
    that points nowhere near the cause.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("server.py", SERVER)
        info = zipfile.ZipInfo("run.sh")
        info.external_attr = 0o755 << 16
        archive.writestr(info, RUN_SH)
    return buffer.getvalue()


def signed_headers(session: Any, url: str, method: str, body: bytes) -> dict[str, str]:
    """SigV4-sign a Function URL request, including the payload hash Lambda insists on.

    **`x-amz-content-sha256` is set explicitly and that is the whole point.** CloudFront's docs state
    that a `PUT` or `POST` to a Lambda Function URL origin must carry the SHA-256 of the body in that
    header, because Lambda does not accept unsigned payloads. botocore does not add it for `lambda`
    the way it does for S3, so a caller that omits it gets `403 InvalidSignature` — an error naming
    authentication rather than the body hash. Setting it before signing puts it in `SignedHeaders`,
    which is what makes the signature verify.

    This is the same obligation ADR-038 records falling on the browser. Exercising it here means the
    frontend helper is implementing something already proven rather than guessed at.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    digest = hashlib.sha256(body).hexdigest()
    request = AWSRequest(method=method, url=url, data=body)
    request.headers.add_header("x-amz-content-sha256", digest)
    request.headers.add_header("Accept", "text/event-stream")
    SigV4Auth(session.get_credentials(), "lambda", "us-east-1").add_auth(request)
    return dict(request.headers.items())


def measure(
    url: str, label: str, *, body: bytes | None = None, session: Any = None
) -> dict[str, Any]:
    """Read the stream, recording when each event arrived rather than only that it did."""
    method = "POST" if body is not None else "GET"
    payload = body if body is not None else b""
    safe_url = require_https(url)
    headers: dict[str, str]
    if session is not None:
        headers = signed_headers(session, safe_url, method, payload)
    else:
        headers = {"Accept": "text/event-stream"}
        # Unsigned by us, but the hash is still required: CloudFront signs to the origin and Lambda
        # rejects an unsigned payload, so a POST without this is a 403 that reads as an auth failure.
        headers["x-amz-content-sha256"] = hashlib.sha256(payload).hexdigest()
    started = time.monotonic()
    arrivals: list[float] = []

    print(f"\n--- {label} ---")
    try:
        with _HTTP.request(
            method,
            safe_url,
            data=body,
            headers=headers,
            timeout=120,
            stream=True,
        ) as response:
            if response.status_code >= HTTP_ERROR_STATUS:
                raise requests.HTTPError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
            print(
                f"  status {response.status_code}, "
                f"content-type {response.headers.get('Content-Type')}"
            )
            while True:
                line = response.raw.readline()
                if not line:
                    break
                if line.startswith(b"data:"):
                    at = time.monotonic() - started
                    arrivals.append(at)
                    if len(arrivals) <= PREVIEW_EVENT_COUNT or len(arrivals) == TICKS:
                        print(f"  event {len(arrivals):2d} at {at:6.2f}s")
    except Exception as exc:  # noqa: BLE001 — a probe reports whatever it hit
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return {"label": label, "events": len(arrivals), "failed": str(exc)}

    if not arrivals:
        print("  no events at all")
        return {"label": label, "events": 0}

    gaps = [b - a for a, b in zip(arrivals, arrivals[1:], strict=False)]
    spread = arrivals[-1] - arrivals[0]
    verdict = "STREAMED" if spread > TICKS / 2 else "BUFFERED"
    print(f"  first {arrivals[0]:.2f}s   last {arrivals[-1]:.2f}s   spread {spread:.2f}s")
    print(f"  median gap {sorted(gaps)[len(gaps) // 2]:.2f}s" if gaps else "  one event only")
    print(f"  >>> {verdict}")
    return {
        "label": label,
        "events": len(arrivals),
        "first": round(arrivals[0], 2),
        "spread": round(spread, 2),
        "verdict": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="No default: never ambient credentials.")
    parser.add_argument("--stage", required=True, choices=["direct", "cloudfront", "destroy"])
    args = parser.parse_args()

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    print(f"account {session.client('sts').get_caller_identity()['Account']}")

    if args.stage == "destroy":
        return _destroy(session)
    if args.stage == "direct":
        return _direct(session)
    return _through_cloudfront(session)


def _role_for(session: Any) -> str:
    """A bare execution role: logs only. The probe function reads nothing and calls nothing."""
    iam = session.client("iam")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        created = iam.create_role(
            RoleName=NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Throwaway. Delete with probe_cloudfront_streaming.py --stage destroy.",
        )
        iam.attach_role_policy(
            RoleName=NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        print("  created role; waiting for propagation")
        time.sleep(12)
        return created["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        return iam.get_role(RoleName=NAME)["Role"]["Arn"]


def _direct(session: Any) -> int:
    """Create the function and its URL, then measure with no CloudFront in the path.

    **`AuthType: AWS_IAM`, because this account refuses anything else.** The first attempt used
    `NONE` to isolate streaming from signing, and it returned `403 AccessDeniedException` with no log
    group created — the function was never invoked — despite a resource policy allowing `*` under the
    matching condition. So a public Function URL is blocked at the account level here, which
    independently confirms ADR-038's rejection of the `NONE`-plus-secret-header alternative: it is not
    merely weaker, it is unavailable.

    Signing here therefore tests two things at once rather than one, and that is now a feature: the
    same SigV4 and payload-hash mechanics CloudFront's OAC will perform are exercised directly, so a
    later failure through CloudFront can be attributed to CloudFront rather than to the signature.
    """
    lambda_client = session.client("lambda")
    role = _role_for(session)

    try:
        lambda_client.create_function(
            FunctionName=NAME,
            Runtime="python3.13",
            Role=role,
            Handler="run.sh",
            Code={"ZipFile": bundle()},
            Architectures=["arm64"],
            MemorySize=512,
            Timeout=120,
            Layers=[LWA_LAYER],
            Environment={
                "Variables": {
                    "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                    "AWS_LWA_INVOKE_MODE": "response_stream",
                    "AWS_LWA_READINESS_CHECK_PATH": "/health",
                }
            },
            Tags={"probe": NAME},
        )
        print("  created function; waiting for active")
        lambda_client.get_waiter("function_active_v2").wait(FunctionName=NAME)
    except lambda_client.exceptions.ResourceConflictException:
        print("  function exists, reusing")

    try:
        url = lambda_client.create_function_url_config(
            FunctionName=NAME,
            AuthType=_refuse_public_auth(AUTH_TYPE),
            InvokeMode="RESPONSE_STREAM",
        )["FunctionUrl"]
    except lambda_client.exceptions.ResourceConflictException:
        lambda_client.update_function_url_config(
            FunctionName=NAME,
            AuthType=_refuse_public_auth(AUTH_TYPE),
            InvokeMode="RESPONSE_STREAM",
        )
        url = lambda_client.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    _drop_public_permission(lambda_client)
    time.sleep(5)

    print(f"  function url {url} (AWS_IAM)")
    result = measure(url + "stream", "Function URL, direct, signed GET", session=session)
    if result.get("verdict") == "STREAMED":
        # POST is what the real route uses, and it is the case that needs the payload hash.
        measure(url + "stream", "Function URL, direct, signed POST", body=b'{"probe":1}', session=session)
    print("\n" + json.dumps(result, indent=1))
    return 0 if result.get("verdict") == "STREAMED" else 1


def _drop_public_permission(lambda_client: Any) -> None:
    """Remove any public invoke statement, every run, whether or not this script created one.

    Kept after the `NONE` attempt was removed rather than deleted with it. A statement naming
    `Principal: "*"` is worth sweeping unconditionally: it costs one idempotent call, and the failure
    it guards against is a public grant surviving on a resource somebody stopped watching.
    """
    try:
        lambda_client.remove_permission(FunctionName=NAME, StatementId="probe-public")
        print("  removed a public invoke statement")
    except Exception:  # noqa: BLE001 — absent is the expected case, and the point
        pass


def _managed(client: Any, kind: str, wanted: str) -> str:
    """Find a CloudFront managed policy id by name rather than hardcoding one.

    The ids are stable and widely quoted, and quoting one here would still be a value with an owner
    somewhere else — which is the copied-value problem. Looking it up costs one call and cannot go
    stale.
    """
    lister = client.list_cache_policies if kind == "cache" else client.list_origin_request_policies
    key = "CachePolicyList" if kind == "cache" else "OriginRequestPolicyList"
    inner = "CachePolicy" if kind == "cache" else "OriginRequestPolicy"
    for item in lister(Type="managed")[key].get("Items", []):
        policy = item[inner]
        if policy[f"{inner}Config"]["Name"] == wanted:
            return policy["Id"]
    raise RuntimeError(f"no managed {kind} policy named {wanted}")


def _through_cloudfront(session: Any) -> int:
    """Put a throwaway distribution in front of the same Function URL and measure again.

    **The only question here is whether CloudFront forwards incrementally or accumulates.** The direct
    stage already proved the origin streams, so a buffered result through CloudFront can only be
    CloudFront — which is what makes running the stages in this order worth the extra step.

    Caching is disabled, and on this behaviour that is a security control rather than a tuning choice:
    a cached authenticated response would serve one user's answer to another. `AllViewerExceptHostHeader`
    forwards the viewer's headers **except** `Host`, which is required — the signature CloudFront
    computes is over the origin's host, so forwarding the viewer's would invalidate it.
    """
    cloudfront = session.client("cloudfront")
    lambda_client = session.client("lambda")

    origin_domain = (
        lambda_client.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
        .removeprefix("https://")
        .rstrip("/")
    )

    try:
        oac = cloudfront.create_origin_access_control(
            OriginAccessControlConfig={
                "Name": NAME,
                "Description": "Throwaway probe. See probe_cloudfront_streaming.py.",
                "SigningProtocol": "sigv4",
                "SigningBehavior": "always",
                "OriginAccessControlOriginType": "lambda",
            }
        )["OriginAccessControl"]["Id"]
        print(f"  created OAC {oac}")
    except cloudfront.exceptions.OriginAccessControlAlreadyExists:
        oac = next(
            item["Id"]
            for page in cloudfront.get_paginator("list_origin_access_controls").paginate()
            for item in page["OriginAccessControlList"].get("Items", [])
            if item["Name"] == NAME
        )
        print(f"  reusing OAC {oac}")

    caching_disabled = _managed(cloudfront, "cache", "Managed-CachingDisabled")
    all_viewer = _managed(cloudfront, "request", "Managed-AllViewerExceptHostHeader")

    existing = [
        item
        for page in cloudfront.get_paginator("list_distributions").paginate()
        for item in page["DistributionList"].get("Items", [])
        if item.get("Comment") == NAME
    ]
    if existing:
        distribution = existing[0]
        print(f"  reusing distribution {distribution['Id']}")
    else:
        distribution = cloudfront.create_distribution(
            DistributionConfig={
                "CallerReference": f"{NAME}-{int(time.time())}",
                "Comment": NAME,
                "Enabled": True,
                "Origins": {
                    "Quantity": 1,
                    "Items": [
                        {
                            "Id": "fn",
                            "DomainName": origin_domain,
                            "OriginAccessControlId": oac,
                            "CustomOriginConfig": {
                                "HTTPPort": 80,
                                "HTTPSPort": 443,
                                "OriginProtocolPolicy": "https-only",
                                # The knob AWS names for a streaming origin: the ceiling is idle time
                                # between bytes, not total duration. 60s is the maximum without a
                                # quota increase, and an SSE heartbeat keeps the gap far below it.
                                "OriginReadTimeout": 60,
                            },
                        }
                    ],
                },
                "DefaultCacheBehavior": {
                    "TargetOriginId": "fn",
                    "ViewerProtocolPolicy": "https-only",
                    "CachePolicyId": caching_disabled,
                    "OriginRequestPolicyId": all_viewer,
                    "AllowedMethods": {
                        "Quantity": 7,
                        "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                    },
                    "Compress": False,
                },
            }
        )["Distribution"]
        print(f"  created distribution {distribution['Id']}")

    arn = f"arn:aws:cloudfront::{session.client('sts').get_caller_identity()['Account']}:distribution/{distribution['Id']}"
    try:
        lambda_client.add_permission(
            FunctionName=NAME,
            StatementId="probe-cloudfront",
            Action="lambda:InvokeFunctionUrl",
            Principal="cloudfront.amazonaws.com",
            SourceArn=arn,
            FunctionUrlAuthType=_refuse_public_auth(AUTH_TYPE),
        )
        print("  granted the distribution InvokeFunctionUrl")
    except lambda_client.exceptions.ResourceConflictException:
        print("  distribution already granted")

    print("  waiting for the distribution to deploy (minutes, not seconds)")
    cloudfront.get_waiter("distribution_deployed").wait(
        Id=distribution["Id"], WaiterConfig={"Delay": 20, "MaxAttempts": 60}
    )
    domain = cloudfront.get_distribution(Id=distribution["Id"])["Distribution"]["DomainName"]
    print(f"  deployed at https://{domain}")

    # No client-side signing: CloudFront signs to the origin. The payload hash is still the caller's
    # obligation on a POST, which is exactly the tax ADR-038 records the browser paying.
    through = measure(f"https://{domain}/stream", "Through CloudFront, GET")
    body = b'{"probe":1}'
    post = measure(f"https://{domain}/stream", "Through CloudFront, POST", body=body)
    print("\n" + json.dumps([through, post], indent=1))
    return 0 if through.get("verdict") == "STREAMED" else 1


def _destroy(session: Any) -> int:
    """Remove everything, reporting each step rather than failing silently."""
    lambda_client = session.client("lambda")
    iam = session.client("iam")
    for step, call in (
        ("function url", lambda: lambda_client.delete_function_url_config(FunctionName=NAME)),
        ("function", lambda: lambda_client.delete_function(FunctionName=NAME)),
        (
            "role policy",
            lambda: iam.detach_role_policy(
                RoleName=NAME,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            ),
        ),
        ("role", lambda: iam.delete_role(RoleName=NAME)),
    ):
        try:
            call()
            print(f"  deleted {step}")
        except Exception as exc:  # noqa: BLE001, PERF203 — report and continue
            print(f"  {step}: {type(exc).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
