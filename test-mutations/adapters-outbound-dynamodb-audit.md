# Mutation review — src/adapters/outbound/dynamodb/audit.py

- Commit: 6980208310085cb14ddec9eef3af33093b754f6f
- Date: 2026-09-26
- Tool: cosmic-ray 8.7.0
- Command: `make mutate MODULE=src/adapters/outbound/dynamodb/audit.py TESTS="tests/unit/test_record_family_stores.py tests/unit/test_agent_quality_audit.py"`
- Mutants: 821 generated, 507 killed, 314 survived
- Survivors: 198 open, 95 killed since, 116 equivalent

Baseline: the first run of this command recorded 409 survivors. Three defects that run exposed are fixed since, without a rerun (2026-09-27): the unwrapped finding decode (S-90 note), the query bounds that counted one short at exactly `Limit` (S-37, S-90, S-100 notes), and the admission read that accepted a blank audit ID, now refused by `tests/unit/test_record_family_stores.py::test_a_blank_audit_id_is_refused_before_any_read[get_evaluation_admission]`. Line numbers below are the run commit's. Remediation covered the record-family tables, the receipts table reads, correlation attestation, and the result link resolver; decoder, projection and legacy aggregate survivors remain OPEN and untriaged.

## S-01 · EQUIVALENT · <module> equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:51
- Mutants: 1 surviving at this location
- Mutation: `if TYPE_CHECKING:` becomes `if not TYPE_CHECKING:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — the guarded imports are used only in annotations, which `from __future__ import annotations` never evaluates

## S-02 · KILLED · <module> survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:75
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `AUDIT_UNREADABLE = ErrorCode("AGENT_QUALITY", 1)` becomes `AUDIT_UNREADABLE = ErrorCode("AGENT_QUALITY", 2)`
- Survives because: before this pass, no test asserted the code of an unreadable-audit DependencyError
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_family_read_that_fails_in_the_sdk_is_unreadable[findings]`

## S-03 · KILLED · <module> survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:76
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `AUDIT_UNWRITABLE = ErrorCode("AGENT_QUALITY", 2)` becomes `AUDIT_UNWRITABLE = ErrorCode("AGENT_QUALITY", 3)`
- Survives because: before this pass, no test asserted the code of an unwritable-audit DependencyError
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_write_failure_other_than_the_condition_is_unwritable_and_never_read_back[findings-denied]`

## S-04 · KILLED · <module> survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:77
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `AUDIT_CONFLICT = ErrorCode("AGENT_QUALITY", 3)` becomes `AUDIT_CONFLICT = ErrorCode("AGENT_QUALITY", 4)`
- Survives because: before this pass, no test asserted the code of an audit-conflict InvariantError
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_different_item_already_under_the_key_is_a_conflict_not_a_replay[findings]`

## S-05 · EQUIVALENT · <module> equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:99
- Mutants: 1 surviving at this location
- Mutation: `@dataclass(frozen=True, slots=True, kw_only=True)` becomes `@dataclass(frozen=False, slots=True, kw_only=True)`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — no code assigns to a `_LinkageItemRequest` field after construction

## S-06 · EQUIVALENT · <module> equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:99
- Mutants: 1 surviving at this location
- Mutation: `@dataclass(frozen=True, slots=True, kw_only=True)` becomes `@dataclass(frozen=True, slots=False, kw_only=True)`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — dataclass slots argument

## S-07 · EQUIVALENT · <module> equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:99
- Mutants: 1 surviving at this location
- Mutation: `@dataclass(frozen=True, slots=True, kw_only=True)` becomes `@dataclass(frozen=True, slots=True, kw_only=False)`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — every construction site passes each field by keyword

## S-08 · OPEN · <module> survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:220
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `"public_block_count": 0,` becomes `"public_block_count": 1,`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-09 · OPEN · _canonical survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:227
- Mutants: 3 surviving at this location
- Mutation: ReplaceFalseWithTrue, ReplaceTrueWithFalse; e.g. `document, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False` becomes `document, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-10 · OPEN · _project_v2_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:248
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `if filled.get("runtime_version") != MANAGED_HARNESS_RUNTIME_V1:` becomes `if filled.get("runtime_version") > MANAGED_HARNESS_RUNTIME_V1:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-11 · OPEN · _decode_record_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:293
- Mutants: 1 surviving at this location
- Mutation: `elif version == _RECORD_V4:` becomes `elif version >= _RECORD_V4:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-12 · OPEN · _decode_record_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:298
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is; e.g. `if version == _RECORD_V4:` becomes `if version > _RECORD_V4:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-13 · OPEN · _decode_record_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:300
- Mutants: 1 surviving at this location
- Mutation: `elif version == _RECORD_V3:` becomes `elif version >= _RECORD_V3:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-14 · OPEN · _decode_record_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:308
- Mutants: 1 surviving at this location
- Mutation: `if canonical != payload:` becomes `if canonical < payload:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-15 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:328
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `set(item) != expected` becomes `set(item) > expected`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-16 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:329
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, AUDIT_ID) != audit_id` becomes `and _read_text(item, AUDIT_ID) != audit_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-17 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:330
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, ITEM_KEY) != RECORD` becomes `and _read_text(item, ITEM_KEY) != RECORD`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-18 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:331
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, "record_type") != RECORD` becomes `and _read_text(item, "record_type") != RECORD`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-19 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:332
- Mutants: 1 surviving at this location
- Mutation: `or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)` becomes `or _read_text(item, PAYLOAD_DIGEST) < _digest(payload)`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-20 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:333
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `or record.audit_id != audit_id` becomes `or record.audit_id > audit_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-21 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:334
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, CORRELATION_ID) != record.correlation_id` becomes `and _read_text(item, CORRELATION_ID) != record.correlation_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-22 · OPEN · _decode_record survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:335
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, CORRELATION_AUDIT_ID) != record.audit_id` becomes `and _read_text(item, CORRELATION_AUDIT_ID) != record.audit_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-23 · OPEN · _decode_finding_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:345
- Mutants: 1 surviving at this location
- Mutation: `if version == _FINDING_V2:` becomes `if version <= _FINDING_V2:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-24 · OPEN · _decode_finding_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:347
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_LtE; e.g. `elif version == _FINDING_V3:` becomes `elif version >= _FINDING_V3:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-25 · OPEN · _decode_finding_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:352
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_Eq_Gt, ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is; e.g. `canonical = contract_to_json(finding) if version == _FINDING_V3 else _canonical(document)` becomes `canonical = contract_to_json(finding) if version > _FINDING_V3 else _canonical(document)`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-26 · OPEN · _decode_finding_payload survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:353
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `if canonical != payload:` becomes `if canonical > payload:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-27 · OPEN · _decode_finding survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:362
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `set(item) != expected` becomes `set(item) > expected`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-28 · OPEN · _decode_finding survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:363
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, AUDIT_ID) != finding.audit_id` becomes `and _read_text(item, AUDIT_ID) != finding.audit_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-29 · OPEN · _decode_finding survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:364
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, ITEM_KEY) != f"{FINDING_PREFIX}{finding.finding_id}"` becomes `and _read_text(item, ITEM_KEY) != f"{FINDING_PREFIX}{finding.finding_id}"`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-30 · OPEN · _decode_finding survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:365
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, "record_type") != "FINDING"` becomes `and _read_text(item, "record_type") != "FINDING"`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-31 · OPEN · _decode_finding survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:366
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)` becomes `and _read_text(item, PAYLOAD_DIGEST) != _digest(payload)`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-32 · KILLED · _decode_linkage survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:374
- Mutants: 1 surviving at this location
- Mutation: `if item != expected:` becomes `if item is expected:`
- Survives because: before this pass, no store-level test read this decoder's rejected input
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[deliveries]`

## S-33 · KILLED · _decode_admission survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:392
- Mutants: 9 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_LtE; e.g. `if value.audit_id != audit_id or value.admission_ref != admission_ref:` becomes `if not value.audit_id != audit_id or value.admission_ref != admission_ref:`
- Survives because: before this pass, no store-level test read this decoder's rejected input
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_recorded_delivery_disposition_and_admission_read_back_exactly`

## S-34 · OPEN · _decode_admission survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:392
- Mutants: 7 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `if value.audit_id != audit_id or value.admission_ref != admission_ref:` becomes `if value.audit_id > audit_id or value.admission_ref != admission_ref:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-35 · OPEN · _decode_delivery survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:411
- Mutants: 7 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `if value.audit_id != audit_id or value.admission_ref != admission_ref:` becomes `if value.audit_id > audit_id or value.admission_ref != admission_ref:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-36 · OPEN · _decode_writer survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:430
- Mutants: 7 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `if value.audit_id != audit_id or value.admission_ref != admission_ref:` becomes `if value.audit_id > audit_id or value.admission_ref != admission_ref:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-37 · KILLED · <module> survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:437
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `_MAX_RESULTS_PER_ADMISSION = 8` becomes `_MAX_RESULTS_PER_ADMISSION = 9`
- Survives because: before this pass, no test asserted the verdict query's `Limit`
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_verdicts_query_one_strong_bounded_prefix_of_the_admission`, `tests/unit/test_record_family_stores.py::test_an_admission_holding_exactly_eight_verdicts_reads_complete`, `tests/unit/test_record_family_stores.py::test_an_admission_holding_nine_verdicts_exceeds_its_bound`
- Note (2026-09-27, not a rerun): the query now requests `Limit` = bound + 1 (9) and treats only more than 8 returned items as exceeded; the first test pins `Limit` 9 and the other two pin the boundary with literal counts.

## S-38 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:455
- Mutants: 1 surviving at this location
- Mutation: `if version == _RESULT_V1:` becomes `if version <= _RESULT_V1:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-39 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:458
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_LtE; e.g. `elif version == _RESULT_V2:` becomes `elif version >= _RESULT_V2:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-40 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:464
- Mutants: 1 surviving at this location
- Mutation: `if version == _RESULT_V2:` becomes `if version >= _RESULT_V2:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-41 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:466
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `if contract_to_json(value) != payload:` becomes `if contract_to_json(value) > payload:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-42 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:468
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `elif _canonical(document) != payload:` becomes `elif _canonical(document) > payload:`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-43 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:472
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `set(item) != expected` becomes `set(item) > expected`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-44 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:473
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, AUDIT_ID) != audit_id` becomes `and _read_text(item, AUDIT_ID) != audit_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-45 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:474
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, ITEM_KEY) != item_key` becomes `and _read_text(item, ITEM_KEY) != item_key`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-46 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:475
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, "record_type") != "EVALUATION_RESULT"` becomes `and _read_text(item, "record_type") != "EVALUATION_RESULT"`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-47 · KILLED · _decode_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:476
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceOrWithAnd; e.g. `or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)` becomes `and _read_text(item, PAYLOAD_DIGEST) != _digest(payload)`
- Survives because: before this pass, no store-level test read this decoder's rejected input
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[verdicts]`

## S-48 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:476
- Mutants: 1 surviving at this location
- Mutation: `or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)` becomes `or _read_text(item, PAYLOAD_DIGEST) < _digest(payload)`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-49 · KILLED · _decode_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:477
- Mutants: 1 surviving at this location
- Mutation: `or value.audit_id != audit_id` becomes `and value.audit_id != audit_id`
- Survives because: before this pass, no store-level test read this decoder's rejected input
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[verdicts]`

## S-50 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:477
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `or value.audit_id != audit_id` becomes `or value.audit_id > audit_id`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-51 · OPEN · _decode_result survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:478
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or value.admission_ref != admission_ref` becomes `and value.admission_ref != admission_ref`
- Survives because: not triaged: pre-existing decoder or projection code outside this remediation
- Verdict: OPEN

## S-52 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:488
- Mutants: 1 surviving at this location
- Mutation: `if (` becomes `if not (`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-53 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:489
- Mutants: 7 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE; e.g. `set(item) != expected` becomes `set(item) == expected`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-54 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:490
- Mutants: 8 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceOrWithAnd; e.g. `or _read_text(item, METRIC_ID) != metric.metric_id` becomes `and _read_text(item, METRIC_ID) != metric.metric_id`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-55 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:491
- Mutants: 8 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceOrWithAnd; e.g. `or _read_text(item, "record_type") != "METRIC"` becomes `and _read_text(item, "record_type") != "METRIC"`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-56 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:492
- Mutants: 8 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceOrWithAnd; e.g. `or _read_text(item, PAYLOAD_DIGEST) != _digest(payload)` becomes `and _read_text(item, PAYLOAD_DIGEST) != _digest(payload)`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-57 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:493
- Mutants: 8 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceOrWithAnd; e.g. `or decoded != metric` becomes `and decoded != metric`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-58 · OPEN · _decode_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:494
- Mutants: 8 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceOrWithAnd; e.g. `or contract_to_json(decoded) != payload` becomes `and contract_to_json(decoded) != payload`
- Survives because: not triaged: legacy metric decoding, excluded from this remediation
- Verdict: OPEN

## S-59 · EQUIVALENT · __init__ equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:509
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:` becomes `def __init__(self, table_name: str, client: DynamoDBClient + None = None) -> None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-60 · EQUIVALENT · _get_item equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:521
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `def _get_item(self, audit_id: str, item_key: str) -> dict[str, AttributeValueTypeDef] | None:` becomes `def _get_item(self, audit_id: str, item_key: str) -> dict[str, AttributeValueTypeDef] + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-61 · KILLED · _get_item survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:526
- Mutants: 1 surviving at this location
- Mutation: `ConsistentRead=True,` becomes `ConsistentRead=False,`
- Survives because: before this pass, no test asserted `ConsistentRead` on the read-back GetItem
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_replay_reads_back_with_a_strongly_consistent_get_of_its_exact_key[findings]`

## S-62 · KILLED · _put survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:541
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_Is, ReplaceComparisonOperator_Eq_LtE; e.g. `if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":` becomes `if error.response.get("Error", {}).get("Code") >= "ConditionalCheckFailedException":`
- Survives because: before this pass, no test raised a non-conditional ClientError, and the conditional code was an interned literal, so identity matched equality
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_replayed_identical_write_reads_back_its_own_table_as_an_exact_replay[findings]`, `tests/unit/test_record_family_stores.py::test_a_write_failure_other_than_the_condition_is_unwritable_and_never_read_back[findings-denied]`, `tests/unit/test_record_family_stores.py::test_a_write_failure_other_than_the_condition_is_unwritable_and_never_read_back[findings-throttled]`

## S-63 · KILLED · _put survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:544
- Mutants: 1 surviving at this location
- Mutation: `except Exception as error:` becomes `except CosmicRayTestingException as error:`
- Survives because: before this pass, no test raised a non-ClientError from PutItem
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_write_failure_other_than_the_condition_is_unwritable_and_never_read_back[findings-unreachable]`

## S-64 · EQUIVALENT · _query_prefix equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:561
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `) -> list[dict[str, AttributeValueTypeDef]] | None:` becomes `) -> list[dict[str, AttributeValueTypeDef]] + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-65 · KILLED · _query_prefix survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:574
- Mutants: 1 surviving at this location
- Mutation: `ConsistentRead=True,` becomes `ConsistentRead=False,`
- Survives because: before this pass, no test asserted the prefix Query request
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_findings_query_one_strong_bounded_prefix_of_the_audit_partition`

## S-66 · KILLED · _query_prefix survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:576
- Mutants: 1 surviving at this location
- Mutation: `except Exception as error:` becomes `except CosmicRayTestingException as error:`
- Survives because: before this pass, no test raised a non-ClientError from Query
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_family_read_that_fails_in_the_sdk_is_unreadable[findings]`

## S-67 · KILLED · _ImmutableRecordTable survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:590
- Mutants: 1 surviving at this location
- Mutation: ` / @staticmethod` becomes `(removed)`
- Survives because: before this pass, no test reached `_write_error`
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_write_failure_other_than_the_condition_is_unwritable_and_never_read_back[findings-denied]`

## S-68 · EQUIVALENT · get_delivery equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:624
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `def get_delivery(self, audit_id: str) -> TurnDeliveryRecordV1 | None:` becomes `def get_delivery(self, audit_id: str) -> TurnDeliveryRecordV1 + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-69 · KILLED · get_delivery survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:626
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceUnaryOperator_Delete_Not; e.g. `if not audit_id.strip():` becomes `if  audit_id.strip():`
- Survives because: before this pass, no test read a delivery disposition through the store
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_receipts_table_read_is_a_strong_get_of_its_exact_key_and_absent_reads_none[delivery-disposition]`

## S-70 · KILLED · get_delivery survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:629
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_Is_IsNot; e.g. `if item is None:` becomes `if not item is None:`
- Survives because: before this pass, no test read an absent delivery disposition through the store
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_receipts_table_read_is_a_strong_get_of_its_exact_key_and_absent_reads_none[delivery-disposition]`

## S-71 · KILLED · get_delivery survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:633
- Mutants: 16 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE, ReplaceOrWithAnd; e.g. `if record.audit_id != audit_id or item != _delivery_item(record):` becomes `if not record.audit_id != audit_id or item != _delivery_item(record):`
- Survives because: before this pass, no test read a delivery disposition retained under another audit ID or with an inexact envelope
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_delivery_disposition_retained_under_another_audit_id_is_malformed[audit-00]`, `tests/unit/test_record_family_stores.py::test_a_delivery_disposition_retained_under_another_audit_id_is_malformed[audit-02]`, `tests/unit/test_record_family_stores.py::test_a_delivery_disposition_with_an_extra_attribute_is_malformed`, `tests/unit/test_record_family_stores.py::test_a_recorded_delivery_disposition_and_admission_read_back_exactly`

## S-72 · KILLED · get_delivery survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:635
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: before this pass, no test read a malformed delivery disposition, so the handler was never evaluated
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_delivery_disposition_retained_under_another_audit_id_is_malformed[audit-00]`

## S-73 · EQUIVALENT · get equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:639
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `def get(self, audit_id: str) -> ConversationAuditRecordV4 | None:` becomes `def get(self, audit_id: str) -> ConversationAuditRecordV4 + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-74 · KILLED · get survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:648
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: before this pass, no test read a malformed receipt through `get`, so the handler was never evaluated
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_receipts_table_item_whose_digest_was_edited_is_an_invariant_conflict[receipt]`

## S-75 · KILLED · _attest_correlated survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:656
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `if set(item) != {AUDIT_ID, ITEM_KEY, CORRELATION_ID}:` becomes `if set(item) > {AUDIT_ID, ITEM_KEY, CORRELATION_ID}:`
- Survives because: before this pass, no test returned an index item with a missing or extra projected attribute
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[extra-attribute]`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[missing-projection]`

## S-76 · KILLED · _attest_correlated survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:659
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt; e.g. `_read_text(item, ITEM_KEY) != RECORD` becomes `_read_text(item, ITEM_KEY) > RECORD`
- Survives because: before this pass, no test returned a non-RECORD index key or a parsed (non-identical) RECORD key
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_parsed_from_a_response_attests_its_base_receipt`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[key-above-record]`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[key-below-record]`

## S-77 · KILLED · _attest_correlated survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:660
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `or _read_text(item, CORRELATION_ID) != correlation_id` becomes `and _read_text(item, CORRELATION_ID) != correlation_id`
- Survives because: before this pass, no test returned an index item naming another correlation
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_parsed_from_a_response_attests_its_base_receipt`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[correlation-above]`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[correlation-below]`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_that_is_not_an_exact_record_key_is_a_conflict[key-below-record]`

## S-78 · KILLED · _attest_correlated survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:664
- Mutants: 4 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt, ReplaceOrWithAnd; e.g. `if record is None or record.correlation_id != correlation_id:` becomes `if record is None and record.correlation_id != correlation_id:`
- Survives because: before this pass, no test returned an index item whose base receipt was absent or disagreed
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_whose_base_receipt_disagrees_fails_attestation[absent]`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_whose_base_receipt_disagrees_fails_attestation[correlation-above]`, `tests/unit/test_record_family_stores.py::test_a_correlation_index_item_whose_base_receipt_disagrees_fails_attestation[correlation-below]`

## S-79 · OPEN · discover survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:673
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceTrueWithFalse; e.g. `while True:` becomes `while False:`
- Survives because: not triaged: unpaginated `discover` is unchanged from main and outside this remediation
- Verdict: OPEN

## S-80 · OPEN · discover survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:684
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_Is_IsNot; e.g. `if start is None` becomes `if not start is None`
- Survives because: not triaged: unpaginated `discover` is unchanged from main and outside this remediation
- Verdict: OPEN

## S-81 · OPEN · discover survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:694
- Mutants: 1 surviving at this location
- Mutation: `except Exception as error:` becomes `except CosmicRayTestingException as error:`
- Survives because: not triaged: unpaginated `discover` is unchanged from main and outside this remediation
- Verdict: OPEN

## S-82 · OPEN · discover survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:698
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_Is_IsNot; e.g. `start = None if last is None else dict(last)` becomes `start = None if not last is None else dict(last)`
- Survives because: not triaged: unpaginated `discover` is unchanged from main and outside this remediation
- Verdict: OPEN

## S-83 · OPEN · discover survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:699
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_Is_IsNot; e.g. `if start is None:` becomes `if not start is None:`
- Survives because: not triaged: unpaginated `discover` is unchanged from main and outside this remediation
- Verdict: OPEN

## S-84 · OPEN · discover survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:700
- Mutants: 1 surviving at this location
- Mutation: `break` becomes `continue`
- Survives because: not triaged: unpaginated `discover` is unchanged from main and outside this remediation
- Verdict: OPEN

## S-85 · EQUIVALENT · discover_bounded equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:706
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `) -> tuple[ConversationAuditRecordV4, ...] | None:` becomes `) -> tuple[ConversationAuditRecordV4, ...] + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-86 · KILLED · discover_bounded survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:717
- Mutants: 1 surviving at this location
- Mutation: `except Exception as error:` becomes `except CosmicRayTestingException as error:`
- Survives because: before this pass, no test raised a non-ClientError from the correlation index Query
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_receipts_table_read_that_fails_in_the_sdk_is_unreadable[correlation-index]`

## S-87 · EQUIVALENT · get_evaluation_admission equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:737
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `) -> EvaluationAdmissionV1 | None:` becomes `) -> EvaluationAdmissionV1 + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-88 · KILLED · get_evaluation_admission survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:740
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_Is_IsNot; e.g. `if item is None:` becomes `if not item is None:`
- Survives because: before this pass, no test read an absent admission through the store
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_receipts_table_read_is_a_strong_get_of_its_exact_key_and_absent_reads_none[admission]`

## S-89 · KILLED · get_evaluation_admission survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:744
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: before this pass, no test read a malformed admission, so the handler was never evaluated
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_receipts_table_item_whose_digest_was_edited_is_an_invariant_conflict[admission]`

## S-90 · KILLED · get_findings survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:755
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `items = self._query_prefix(audit_id, FINDING_PREFIX, 20)` becomes `items = self._query_prefix(audit_id, FINDING_PREFIX, 21)`
- Survives because: before this pass, no test asserted the findings query's `Limit`
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_findings_query_one_strong_bounded_prefix_of_the_audit_partition`, `tests/unit/test_record_family_stores.py::test_a_findings_partition_holding_exactly_twenty_reads_complete`, `tests/unit/test_record_family_stores.py::test_a_findings_partition_holding_twenty_one_exceeds_its_bound`
- Note (2026-09-27, not a rerun): the literal moved to `_FINDINGS_BOUND = 20`, the query requests `Limit` 21, and only more than 20 returned items exceed the bound. A malformed finding now maps to the conflict `InvariantError` instead of escaping as `ValueError`, killed by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[findings]`.

## S-91 · KILLED · get_findings survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:762
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_Lt; e.g. `if finding.audit_id != audit_id:` becomes `if finding.audit_id > audit_id:`
- Survives because: before this pass, no test returned a finding from another audit partition
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_finding_returned_from_another_audit_partition_is_a_conflict[audit-00]`, `tests/unit/test_record_family_stores.py::test_a_finding_returned_from_another_audit_partition_is_a_conflict[audit-02]`

## S-92 · EQUIVALENT · get_evaluation_delivery equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:801
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `) -> EvaluationDeliveryReceiptV1 | None:` becomes `) -> EvaluationDeliveryReceiptV1 + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-93 · KILLED · get_evaluation_delivery survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:808
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: before this pass, no test read a malformed delivery receipt, so the handler was never evaluated
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[deliveries]`

## S-94 · EQUIVALENT · get_evaluation_writer_receipt equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:838
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `) -> EvaluationWriterReceiptV1 | None:` becomes `) -> EvaluationWriterReceiptV1 + None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-95 · KILLED · get_evaluation_writer_receipt survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:845
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: before this pass, no test read a malformed writer receipt, so the handler was never evaluated
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[writer-receipts]`

## S-96 · KILLED · get_evaluation_results survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:882
- Mutants: 7 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE; e.g. `if key != exact and not key.startswith(f"{exact}#"):` becomes `if not key != exact and not key.startswith(f"{exact}#"):`
- Survives because: before this pass, no test returned a sibling admission's verdict or a V1 verdict through the store
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_sibling_admission_sharing_the_verdict_prefix_is_skipped_in_any_order`, `tests/unit/test_record_family_stores.py::test_a_stored_v1_verdict_reads_through_the_store_under_its_admission_only_key`

## S-97 · EQUIVALENT · get_evaluation_results equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:882
- Mutants: 1 surviving at this location
- Mutation: `if key != exact and not key.startswith(f"{exact}#"):` becomes `if key > exact and not key.startswith(f"{exact}#"):`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — every key a `begins_with(item_key, exact)` query returns sorts at or after `exact`, so `key > exact` and `key != exact` agree on every reachable key

## S-98 · KILLED · get_evaluation_results survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:883
- Mutants: 1 surviving at this location
- Mutation: `continue  # a sibling admission whose reference merely shares this prefix` becomes `break  # a sibling admission whose reference merely shares this prefix`
- Survives because: before this pass, no test returned a sibling admission's verdict ahead of this admission's
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_sibling_admission_sharing_the_verdict_prefix_is_skipped_in_any_order`

## S-99 · KILLED · get_evaluation_results survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:886
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: before this pass, no test read a malformed verdict, so the handler was never evaluated
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_a_retained_family_item_whose_digest_was_edited_is_an_invariant_conflict[verdicts]`

## S-100 · KILLED · resolve_evaluation_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:907
- Mutants: 2 surviving at this location
- Mutation: NumberReplacer; e.g. `AuditCorrelationRequest(correlation_id=request.correlation_id), 2` becomes `AuditCorrelationRequest(correlation_id=request.correlation_id), 3`
- Survives because: before this pass, no test asserted the resolver's correlation bound
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_result_ingress_bounds_its_correlation_index_query_to_two_receipts`
- Note (2026-09-27, not a rerun): the literal moved to `_CORRELATION_BOUND = 2` and `discover_bounded` requests `Limit` = bound + 1, so the test now pins `Limit` 3; `tests/unit/test_record_family_stores.py::test_a_correlation_holding_exactly_two_receipts_attests_both` and `tests/unit/test_record_family_stores.py::test_a_correlation_holding_three_receipts_is_truncated_without_base_reads` pin the boundary.

## S-101 · KILLED · resolve_evaluation_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:914
- Mutants: 1 surviving at this location
- Mutation: `pending = False` becomes `pending = True`
- Survives because: before this pass, no test resolved a result whose writer receipt named another trace
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_result_ingress_does_not_link_a_writer_receipt_for_another_trace[trace-below]`

## S-102 · KILLED · resolve_evaluation_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:918
- Mutants: 1 surviving at this location
- Mutation: `continue` becomes `break`
- Survives because: before this pass, no test placed an unadmitted receipt ahead of an admitted one
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_result_ingress_links_the_admitted_receipt_after_an_unadmitted_one`

## S-103 · KILLED · resolve_evaluation_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:924
- Mutants: 1 surviving at this location
- Mutation: `continue` becomes `break`
- Survives because: before this pass, no test placed a receipt awaiting its writer ahead of a linked one
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_result_ingress_links_past_an_admitted_receipt_still_awaiting_its_writer`

## S-104 · KILLED · resolve_evaluation_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:925
- Mutants: 3 surviving at this location
- Mutation: ReplaceComparisonOperator_Eq_GtE, ReplaceComparisonOperator_Eq_IsNot, ReplaceComparisonOperator_Eq_LtE; e.g. `if writer.trace_id == request.trace_id:` becomes `if writer.trace_id >= request.trace_id:`
- Survives because: before this pass, no test resolved a result against a writer receipt for a lower or higher trace
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_result_ingress_does_not_link_a_writer_receipt_for_another_trace[trace-above]`, `tests/unit/test_record_family_stores.py::test_result_ingress_does_not_link_a_writer_receipt_for_another_trace[trace-below]`

## S-105 · KILLED · resolve_evaluation_result survivor killed

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:932
- Mutants: 1 surviving at this location
- Mutation: `if len(targets) == 1:` becomes `if len(targets) >= 1:`
- Survives because: before this pass, no test resolved a trace linked by two receipts
- Verdict: KILLED by `tests/unit/test_record_family_stores.py::test_result_ingress_refuses_to_choose_between_two_receipts_linked_to_one_trace`

## S-106 · EQUIVALENT · resolve_evaluation_result equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:935
- Mutants: 1 surviving at this location
- Mutation: `target=targets[0],` becomes `target=targets[ -1],`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — the branch runs only when `len(targets) == 1`, where `targets[0]` is `targets[-1]`

## S-107 · EQUIVALENT · __init__ equivalent mutation

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:949
- Mutants: 11 surviving at this location
- Mutation: ReplaceBinaryOperator_BitOr_Add, ReplaceBinaryOperator_BitOr_BitAnd, ReplaceBinaryOperator_BitOr_BitXor, ReplaceBinaryOperator_BitOr_Div, ReplaceBinaryOperator_BitOr_FloorDiv, ReplaceBinaryOperator_BitOr_LShift, ReplaceBinaryOperator_BitOr_Mod, ReplaceBinaryOperator_BitOr_Mul, ReplaceBinaryOperator_BitOr_Pow, ReplaceBinaryOperator_BitOr_RShift, ReplaceBinaryOperator_BitOr_Sub; e.g. `def __init__(self, table_name: str, client: DynamoDBClient | None = None) -> None:` becomes `def __init__(self, table_name: str, client: DynamoDBClient + None = None) -> None:`
- Survives because: no reachable input distinguishes the mutant from the original
- Verdict: EQUIVALENT — type annotation under `from __future__ import annotations`; never evaluated

## S-108 · OPEN · DynamoAgentQualityAggregateStore survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:955
- Mutants: 1 surviving at this location
- Mutation: ` / @property` becomes `(removed)`
- Survives because: not triaged: legacy aggregate store, excluded from this remediation
- Verdict: OPEN

## S-109 · OPEN · _dynamodb survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:957
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_Is_IsNot; e.g. `if self._client is None:` becomes `if not self._client is None:`
- Survives because: not triaged: legacy aggregate store's lazy client, excluded from this remediation
- Verdict: OPEN

## S-110 · OPEN · write_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:970
- Mutants: 1 surviving at this location
- Mutation: `except ClientError as error:` becomes `except CosmicRayTestingException as error:`
- Survives because: not triaged: legacy aggregate write, excluded from this remediation
- Verdict: OPEN

## S-111 · OPEN · write_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:971
- Mutants: 8 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_NotEq_Eq, ReplaceComparisonOperator_NotEq_Gt, ReplaceComparisonOperator_NotEq_GtE, ReplaceComparisonOperator_NotEq_Is, ReplaceComparisonOperator_NotEq_IsNot, ReplaceComparisonOperator_NotEq_Lt, ReplaceComparisonOperator_NotEq_LtE; e.g. `if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":` becomes `if not error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":`
- Survives because: not triaged: legacy aggregate write, excluded from this remediation
- Verdict: OPEN

## S-112 · OPEN · write_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:981
- Mutants: 1 surviving at this location
- Mutation: `ConsistentRead=True,` becomes `ConsistentRead=False,`
- Survives because: not triaged: legacy aggregate write, excluded from this remediation
- Verdict: OPEN

## S-113 · OPEN · write_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:984
- Mutants: 2 surviving at this location
- Mutation: AddNot, ReplaceComparisonOperator_IsNot_Is; e.g. `if found is not None:` becomes `if not found is not None:`
- Survives because: not triaged: legacy aggregate write, excluded from this remediation
- Verdict: OPEN

## S-114 · OPEN · write_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:987
- Mutants: 1 surviving at this location
- Mutation: `except TypeError, ValueError:` becomes `except CosmicRayTestingException, ValueError:`
- Survives because: not triaged: legacy aggregate write, excluded from this remediation
- Verdict: OPEN

## S-115 · OPEN · write_metric survivor

- Track: mechanical
- Location: src/adapters/outbound/dynamodb/audit.py:998
- Mutants: 1 surviving at this location
- Mutation: `except Exception as error:` becomes `except CosmicRayTestingException as error:`
- Survives because: not triaged: legacy aggregate write, excluded from this remediation
- Verdict: OPEN
