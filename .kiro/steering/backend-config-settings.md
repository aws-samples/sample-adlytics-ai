---
inclusion: auto
name: backend-config-settings
description: Governs runtime settings, environment or Parameter Store resolution, and request-serving or operator entrypoints when changing those backend concerns.
---

# Configuration

Decision and the SSM parameter paths: docs/decisions/adr-012-stack-composition-and-config.md.
The single owner of request-serving runtime configuration is
src/adapters/config.py. Operator jobs consume the deployment interfaces owned by
ADR-012.

Two gates cover most of this file's former contents and it states none of what
they check: make check-config-access proves only that module reads the
environment, and tests/unit/test_config.py asserts the failure behaviour — every
missing key reported at once, the error class, the key names in context not in the
description, and that a rejected value never reaches an error, a log or a span.

## Resolution

- In request-serving handlers and their composed adapters, take every configured
  value from adapters.config.settings(). Never read the environment, a file, or
  Parameter Store elsewhere on a request-serving path.
- Call settings() at module scope in a handler, never inside the request path. A
  container that cannot resolve its configuration must fail before it becomes
  healthy; a request-time failure is a partial outage that reads as a bug in
  whichever request arrived first.
- Resolve an operator job's deployed resource targets through ADR-012's
  published Parameter Store interface. Never construct request-serving Settings
  for values the job does not consume.
- Derive anything computable from another value rather than adding a key for it.
  Two keys that must agree eventually will not, and the mismatch presents as an
  outage rather than a typo.

## Secrets

- Declare every new secret as SecretStr so logging the settings object cannot
  reveal it. The one call to get_secret_value is then greppable.
- Never pass the settings object into domain code. A domain function receives the
  values it needs as arguments, so its signature never depends on how
  configuration is shaped.
