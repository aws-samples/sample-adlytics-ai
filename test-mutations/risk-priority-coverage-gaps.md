# Risk-priority mutation coverage gaps

- Commit: `3d5d6eafc55a820911669de5a4c5d67cdc6e5c25`
- Date: 2026-09-11
- Status: NOT RUN

## `src/adapters/inbound/tool_handlers/measurement_entrypoint.py`

- Status: NOT RUN directly. Its deployed entrypoint remains integration-only because it composes Athena, DynamoDB, S3, and publication adapters at import.
- Local owner: `src/adapters/inbound/tool_handlers/measurement_dispatch.py` and `tests/unit/test_measurement_dispatch.py` own declared tool selection, typed refusal rendering, and semantic-request context construction without AWS composition. Its Wave 0 mutation review recorded 30 generated mutants, 26 killed, and 4 survivors in `test-mutations/adapters-inbound-tool_handlers-measurement_dispatch.md`.
- Existing evidence: `tests/integration/test_measurement_entrypoint.py` exercises the built bundle and transport behavior.
- Required before direct entrypoint mutation: explicit non-production AWS integration authorization and a bounded retained integration mutation strategy. A local mutation run of the composed module would either fail at import or replace the code under test with mocks.

## `src/adapters/inbound/conversation/server.py`

- Status: local owner and mutation review complete. `tests/unit/test_conversation_server.py` owns health/unknown GET, identity denial, classified pre-open errors, inbound Guardrail intervention, empty/invalid runtime, chunked valid response, and started-stream dependency terminalization.
- Mutation evidence: 144 generated mutants, 59 killed, and 85 survivors recorded in `test-mutations/adapters-inbound-conversation-server.md`.
- Existing adjacent coverage: `tests/unit/test_structured_conversation_route.py` remains the focused `sse` relay owner.
