---
inclusion: fileMatch
fileMatchPattern: ["src/**/*.py"]
description: Governs hexagonal dependency direction, domain and adapter responsibilities, typed request and result signatures, and outbound port boundaries when editing Python application code.
---

# Hexagonal boundaries

Layout and rationale: docs/repo-structure.md §2.

## The dependency rule

Imports point inward only: adapters to domain, ports to domain, domain to
nothing outside domain. make lint-boundaries is the gate, and the import-linter
contracts in pyproject.toml are the statement of which modules are banned where.
Never restate that list here — it has already drifted once.

- Never let an AWS SDK type appear in a port signature, including as an
  annotation. import-linter catches the import, not a name resolved from a stubs
  package.
- Every domain module must be testable with no AWS credentials, no network and
  no mocking of AWS. When a domain function needs a clock, a client or a span,
  the design is wrong rather than the test.

## What belongs where

- Business logic and maths go in src/domain. Anything that talks to AWS, an API,
  a filesystem or a clock goes in src/adapters.
- A job that reads data, calls a library and writes results is an adapter. The
  maths it calls is domain.
- Never duplicate a calculation between a job and a tool. If both need it, it is
  domain.
- Never put a metric definition in code. Metrics are declared in
  contracts/shared/metric-dictionary.yaml and read from there.

## Signatures take one typed object

Past the arity ruff's PLR0913 permits, use a named request object rather than a
longer signature. Adding a field then costs a default value rather than an edit at
every call site, and the object carries its own validation so a caller cannot
bypass it.

- Name them <Operation>Request and <Operation>Result. QueryRequest and
  QueryResult on the query port are the reference.
- Put validation in __post_init__ on the request object. Never validate the same
  rule in each caller.
- Return a typed result object whenever a method returns more than one value.
  Never return a bare tuple or an untyped dict from a port or a domain function.
- Never wrap a one- or two-parameter call in a request object. Wrapping get(key)
  in a GetRequest is ceremony, not clarity.
- Never add an optional parameter to a port method to extend it. Add a field to
  its request object with a default.

ruff enforces the arity half of this with PLR0913. The naming, validation
placement and result-object rules it cannot check, which is why they are here.

## Ports

- Never give a port a way to carry SQL. A query port takes a validated plan
  expressed in domain terms; composing a statement is an adapter's job. What a
  tool may then do with it is owned by agent-contract-tool, which loads where a
  tool handler is edited and is not restated here.
