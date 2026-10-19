---
inclusion: auto
name: backend-typing-aws-sdk
description: Governs boto3 or botocore clients, typed AWS responses, and types-boto3 dependencies when changing AWS SDK integration.
---

# Typing AWS SDK calls

Why both packages are needed and what was verified: the dependency comments in
pyproject.toml. Port and boundary rules are meta-architecture-hexagonal.

- Adding an adapter for an AWS service the repository has not called before is a
  dependency change: add that service to the `types-boto3` extras before writing
  the call, and verify the stub resolves rather than assuming coverage.
  pyproject.toml's dependency comments own which services need naming separately.
- Never silence an untyped SDK call with a per-line ignore. An `Any` from boto3
  means the stub for that service is missing, and the suppression hides a
  dependency defect while spreading to every adapter written afterwards.
- Never relax the type checker's settings for `src/adapters` to accommodate an SDK
  call. The adapters are where every AWS mistake is made and are the last place to
  lower the checker.
- Treat a `TypedDict` key error from a response as a real finding, not stub noise.
  It means the response shape being read is not the shape the service returns.
