# Use Adlytics in Amazon Quick

A demo slice, dev only, as of 2026-10-22. Live acceptance and the Amazon Quick hop are pending.

## 1. What it is

The external MCP gateway lets a signed-in member of `mcp-tool-users` ask Adlytics' governed tools
questions from Amazon Quick chat, with their own Cognito identity at every hop and no shared service
identity. The tools are the same five read-only catalog and measurement tools the in-app assistant
uses, and they return the same governed figures. Why it is built this way, its accepted risk and its
limits are in [ADR-150](decisions/adr-150-expose-the-governed-tools-to-amazon-quick-through-a-user-authorized-mcp-gateway.md);
which credential is valid where is in [authentication](concepts/authentication.md#credential-binding-adr-078).

## 2. Prerequisites

- **Amazon Quick Enterprise in the deployment's own AWS account and Region.** MCP integration needs
  Enterprise, and results may go only to a Quick instance in the same account and geography
  (ADR-150, data residency). An individual plan signed up by email has no AWS account and cannot be
  used.
- **The Cognito caveat.** Even with Enterprise, Quick's sign-in against Cognito is unproven; Cognito
  does not publish the RFC 8414 metadata Quick looks for first, so the connection depends on the
  manually entered URLs; if it fails, the hop is reported pending and the demo uses the MCP-client
  branch (§5.5).
- **The flag, in two passes**, each deployed with `make deploy-all PROFILE=<profile> ENV=dev`, the
  diff read, then the same command with `APPROVE=1`:
  1. Pass 1, the gate with no tools: in `.env`, `ADLYTICS_EXTERNAL_MCP_GATEWAY=true`,
     `ADLYTICS_EXTERNAL_MCP_TARGETS_ATTACHED=false` and
     `ADLYTICS_EXTERNAL_MCP_WAF_RATE_LIMIT_PER_FIVE_MINUTES=1000`. Then add the synthetic `probe-a`
     persona to `mcp-tool-users` and run `validation/probe_external_mcp.py --case pass1`
     ([validation README](../validation/README.md#running-the-external-mcp-gateway-probe)).
  2. Pass 2, after every pass-1 case passes: commit `EXTERNAL_MCP_WAF_MODE = 'block'` in
     `infra/lib/constructs/agent/external-mcp-gateway.ts`, set
     `ADLYTICS_EXTERNAL_MCP_TARGETS_ATTACHED=true`, deploy, and run `--case pass2`. Synth refuses
     the tools while any web ACL rule still counts.

## 3. Connect Quick

Amazon Quick has no API for an MCP connector, so this is a console path for the owner. It reads
values from three SSM parameters, never from this page: `/adlytics/dev/agent/external-mcp-gateway-url`,
`/adlytics/dev/auth/mcp-client-id` and `/adlytics/dev/auth/domain`.

1. Confirm the subscription: `aws quicksight describe-account-subscription --aws-account-id
   <account> --profile <profile> --region us-east-1` returns edition `ENTERPRISE`, and the Quick
   console shows that same account. If either check fails, stop: the Quick hop is pending.
2. Add your dev user to `mcp-tool-users`: `aws cognito-idp admin-add-user-to-group --user-pool-id
   <user pool id> --username <your user> --group-name mcp-tool-users --profile <profile>`.
3. In the Quick console, open **Connectors**, then **Create for your team**, then **Model Context
   Protocol (MCP)**.
4. Name it `Adlytics AI (dev)`. Set **MCP server endpoint** to the gateway URL parameter, and both
   **Connection type** and **Auth server connection type** to **Public network**. Choose **Next**.
5. Choose **User authentication**. Set **Client ID** to the client ID parameter and tick **Public
   OAuth client**. Set **Token URL** to `https://<login domain>/oauth2/token` and **Authorization
   URL** to `https://<login domain>/oauth2/authorize`, with the login domain from the domain
   parameter. Record the **Redirect URL** Quick shows; if it is not
   `https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback`, the client's callback is fixed
   forward before you continue. Choose **Create and continue** and sign in.
6. After pass 2: review the five actions, choose **Next**, share with nobody, choose **Done**, then
   **Sync**.

## 4. What Quick may and may not do with the figures

Quick is told, in each tool's description and the server instructions, to quote every figure as
returned with what qualifies it and never to total, recompute or sum it across networks: the rule is
`external_client_rule` in [`contracts/generated/tools.json`](../contracts/generated/tools.json), and
the figure-verification guarantee covers in-app chat only
([semantic accuracy](concepts/semantic-accuracy.md#3-how-it-works)). Five attributed metrics state no
attribution window in their results, a [known gap](decisions/adr-150-expose-the-governed-tools-to-amazon-quick-through-a-user-authorized-mcp-gateway.md#known-gaps)
the demo avoids.

## 5. Demo script

### 5.1 Preconditions

- The subscription read-back in §3 step 1 shows the deployment's account and Enterprise.
- Your user is in `mcp-tool-users`.
- The connector shows five actions and has been synced.
- `validation/RESULTS.md` holds a pass-2 table, every case PASS, dated within 24 hours.

### 5.2 Three questions

| Ask | Tool and arguments | Point at |
|---|---|---|
| "What can Adlytics measure for me?" | `catalog_list_metrics`, no arguments | Each row's `label` and `measurement_basis`. A catalog answer states no period, so say so rather than name one |
| "Show ad spend by campaign for last month." | `measurement_metric_by_grain`: `metric_reference` "ad spend", `grains` ["campaign"], `period_reference` "last month" | The one claim label "Ad spend", `measurement_basis`, `requested_coverage_intervals`, and any `coverage_holes`. Ad spend has no attribution window |
| "Which five campaigns had the highest ROAS last month?" | `measurement_top_n`: `metric_reference` "ROAS", `grains` ["campaign"], `rank_size` 5, `rank_order` "VALUE", `period_reference` "last month" | The label "ROAS (attributed, 14 day)", the `attribution_window` qualifier of 14 days, `requested_coverage_intervals` and any `coverage_holes` |

If dev has not published last month, the tool refuses it; ask again without a period, and the answer
names the latest published coverage it used.

### 5.3 The governance moment

Ask: "What were total attributed sales across all networks?" The governed call is
`measurement_metric_by_grain` with `metric_reference` "attributed revenue", `grains` ["ecosystem"]
and `period_reference` "last month". Point at the label "Attributed revenue (14 day, advertised
products)" and at `data.additivity`, the rule that these per-network figures don't add up, beside
the figures. If dev holds one network, the pass-2 table records "single network in dev" and there is
nothing to sum. Then point at the tool description's rule. How Quick's own model words the answer is
the owner-accepted risk in ADR-150; say so.

### 5.4 The security moment

From the sanitized pass-2 table:
- an anonymous request is refused, with its status and the layer that refused it;
- `probe-b`, a signed-in user outside `mcp-tool-users`, is refused at the gateway (403, `-32002`);
- the same user's token on a direct tool invoke is refused by the tool (`ADLY-CALLER-0001`,
  `entitlement_missing`).

### 5.5 The fallback branch: Quick hop pending

State the recorded pending reason, one of:
- "Quick Enterprise is not subscribed in the deployment account";
- "Quick discovery or sign-in against Cognito failed: <Quick's exact error>";
- "the Quick protected-resource metadata check failed";
- "Cognito refuses RFC 8707 resource at /oauth2/token";
- "AWS WAF refused Quick's traffic (`-32002 Authorization error - Request forbidden`, rule <id>); a
  WAF scoping decision is with the owner".

Then present the pass-2 case table, and the three questions and the governance moment as the
probe's MCP `tools/call` results, pointing at the same fields.

## 6. After the demo

Unless the owner promotes the integration:
1. Remove `probe-a` and your user from `mcp-tool-users` (`admin-remove-user-from-group`).
2. On the owner's go, set `ADLYTICS_EXTERNAL_MCP_GATEWAY=false` and redeploy, diff first. That
   removes the gateway, web ACL, interceptor, client, group and parameters.
3. If a Quick subscription was created for the demo, remove it before any trial ends. Its
   termination protection must be turned off first, which is a separate owner approval.
