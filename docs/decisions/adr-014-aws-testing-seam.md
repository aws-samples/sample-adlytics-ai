# ADR-014 — Where the test seam sits against AWS

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-08-18 |
| **Decision** | **The unit tier neither touches AWS nor fakes it. Adapters are validated against real AWS resources in the integration tier — never against a mock, stub or emulator.** Adversarial inputs the real service will not produce are synthesised with real cryptography, which is not the same as faking the service. No AWS emulator is introduced, in any tier. |
| **Related** | [repo-structure](../repo-structure.md) §6 · [ADR-007](adr-007-auth-cookie-bff.md) · [ADR-012](adr-012-stack-composition-and-config.md) · [ADR-013](adr-013-mutation-testing.md) |

## Context

The question that forced this: how do we develop the Cognito auth flow? Either point the dev loop at a real user pool, or put a fake behind the identity port. Both looked defensible.

AWS has published guidance that settles it, in [Prescriptive Guidance on testing serverless applications](https://docs.aws.amazon.com/prescriptive-guidance/latest/serverless-application-testing/best-practices.html), read 2026-08-18. [verified]

> Don't use mocks of cloud services to validate the proper implementation of those service integrations.

Alongside that: prioritise testing in the cloud, because it gives the most reliable and complete coverage; mocks earn their place for complex internal logic where inputs do not change the pattern of calls to other services; and anything covered by a mocked unit test should **also** be covered in the cloud, because the mock runs in a differently-configured environment.

Two of our existing decisions were independently validated by the same page. It recommends the Lambda handler act as a thin adapter that delegates to a function with no Lambda dependencies — which is the hexagonal rule we already enforce — and it recommends Parameter Store for per-stack configuration such as API endpoints, which is [ADR-012](adr-012-stack-composition-and-config.md) arrived at from a different direction.

## The wrinkle that makes "just use a real pool" wrong too

A real Cognito user pool **only ever mints valid tokens for itself.** It will not issue a token with the wrong `client_id`, a token signed with `alg: none`, a token signed by an unknown `kid`, an already-expired token, or a tampered payload. Those are exactly the inputs where an authorizer bug becomes an authentication bypass.

So the security-critical half of the authorizer cannot be tested against the real service at all. It has to be tested with tokens we sign ourselves, using a locally generated RSA keypair and a synthetic JWKS document.

That is not mocking a cloud service. It is exercising real RS256 verification and real claim logic against inputs we control. AWS's prohibition is about faking the *integration* — pretending a call succeeded — not about constructing hostile inputs.

## Decision

| Concern | Tier | Real AWS? |
|---|---|---|
| Business logic, cookie build and parse, claim rules, configuration validation | `unit` | no, and no AWS fake either |
| Signature verification, `kid` mismatch, algorithm confusion, expiry, `client_id`, `token_use` | `unit` | no — self-signed keys, because the real service cannot produce these |
| Data contracts against fixtures whose provenance class permits structural conformance | `contract` | no |
| JWKS fetch and cache, token exchange, revoke, every SDK call | `integration` | **yes** |
| Full journeys and agent behaviour | `evaluation` | yes |

Integration tests **skip** rather than fail when credentials are absent, so the inner loop stays credential-free as the hexagonal rules require, and `make verify` never needs an AWS profile.

Per-developer isolation follows the same guidance: separate stacks, with configuration read from the SSM paths in ADR-012 whose `<env>` segment already supports it.

### Rejected

| Rejected | Why |
|---|---|
| LocalStack for Cognito | Cognito emulation sits behind the paid tier, and an emulator-first strategy blocks new service features until the vendor reaches parity |
| moto for Cognito | An in-process fake. Passing tests would prove our fake works, which is precisely what the AWS rule prohibits |
| Mocked boto3 clients as the adapter test strategy | Same reason. **This withdraws `make test-adapters` as previously described in [repo-structure](../repo-structure.md) §7** — adapter correctness is an integration concern |
| Negative auth cases against a real pool | The service will not issue an invalid token, so the cases cannot be constructed there |
| Integration tests inside `make verify` | Would make the inner loop require credentials and network, breaking the domain-purity guarantee |
| Self-signed tokens as the *only* JWT testing | They prove our verifier's logic, not that we read Cognito's real JWKS shape or handle its rotation. Both halves are required |

## Consequences

The `integration` tier stops being optional. AWS's guidance is explicit that mock-covered code needs cloud coverage too, so an adapter with only unit tests is incomplete rather than adequately covered.

A development AWS account becomes a hard dependency for completing any adapter, which narrows the open half of [Q-09](../decision-coverage-registry.md#question-register-q-nn). The AMC sandbox and fixture questions there are unaffected.

The cost we accept: the first real token exchange happens later than it would with an emulator, and a broken assumption about Cognito's actual responses surfaces at integration time rather than on a laptop. That is the trade AWS's guidance recommends, and the alternative hides the same defect behind a fake that agrees with us.

> **Fixture-provenance amendment, 2026-08-24.** The contract tier's AWS seam remains unchanged, but
> “recorded source output” was too broad: generated contract fixtures are not source evidence and
> hand-built traps prove only their declared scenario. Accepted [ADR-051](adr-051-classify-fixture-provenance-and-evidentiary-status.md)
> owns fixture classes and the claim each may support; real service integration remains required for
> adapter calls.

**Skipping hides its own failures, so every SDK call is also asserted directly.** Found by auditing the tier against this ADR rather than by an incident. The `integration` conftest resolves the deployed environment to build its `settings` fixture, and that resolution reaches Secrets Manager — so the repository's only boto3 call was exercised on every run, transitively, as a precondition of every test here. It was nonetheless the weakest-covered line in the tier: the fixture catches `InvariantError` and skips, which renders an unreadable secret identical to "nothing deployed". The skip that keeps the inner loop credential-free will, on the wrong day, swallow the failure the tier exists to catch.

The rule this yields: **a transitively exercised SDK call is not an asserted one.** Where a fixture's own setup makes a real call, the call needs its own test, or its failure arrives as a skip that reads like an absence. `tests/integration/test_secrets_resolution.py` is that test for the secret fetch — it asserts the value resolved, that it is not the identifier it was fetched by, that `model_copy` did not downgrade the `SecretStr`, and that an unreadable name yields `InvariantError` carrying neither the secret nor an ARN. The redaction assertion checks the recorded cause is a bare type name rather than pinning an exception class, because absent and forbidden raise different types and pinning one would assert the reason instead of the redaction.

