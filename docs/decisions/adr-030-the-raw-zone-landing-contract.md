# ADR-030: The manifest is the completion marker, and a delivery is keyed on its dataset

Date: 2026-08-20
Status: Accepted

> **Source-acquisition amendment, 2026-08-24.** The canonical prefix and manifest-last completion marker remain current, but the default cross-account producer interface no longer asks the external pipeline to construct either. Under [ADR-050](adr-050-finalize-registered-cross-account-landings-into-governed-manifests.md), the producer writes one result and `registration.json` last under `incoming/`; an Adlytics Finalizer copies the exact VersionId-bound bytes to this ADR's canonical prefix and writes the manifest last. Direct canonical-manifest landing remains available only to trusted in-account tooling. The local Semantic V2 loader accepts exactly one data object with a nonblank VersionId, so this ADR's multi-part allowance is historical until a format-specific join rule is decided. Read-only named-stack inspection and a producer-disabled no-change-set diff are complete. Producer-enabled deployment, producer conformance, and destination-readiness acceptance remain pending in [validation results](../../validation/RESULTS.md#pending-acceptance--2026-08-24).

Settles the physical half of the raw-zone landing contract, which [provenance-chain](../concepts/provenance-chain.md) §2.2 had specified in every respect except *where an object goes* and *what tells us it is complete*. Those two gaps are what made [F-01](../decision-coverage-registry.md#finding-register-f-nn) a conversation nobody could finish: the directives were agreeable in principle and not implementable without a layout.

## Context

Extraction belongs to the producer that operates it (outside this repository). We define what a valid landing looks like even though this repository does not contain the producer, because the one property no downstream check can reconstruct — completeness — is established before the file is written.

Three things had to be decided, and each has a plausible wrong answer.

**What identifies a delivery in the key.** The obvious answer is our contract id, one directory per contract. It is wrong for a reason that only surfaces when you look at the vendor contracts: `GET_VENDOR_SALES_REPORT` is read by **two** contracts, `vendor-sales-by-asin` and `vendor-sales-reported-total`, because one vendor document carries two sibling arrays at two different grains. A contract-keyed path cannot express one object serving two contracts without either duplicating the object or inventing a primary. It also forces the producer that operates extraction (outside this repository) to learn our internal ids, which are ours and change when we decompose a contract.

**What triggers the load.** A delivery is at least two objects — data plus manifest — and S3 fires an event per object with no ordering guarantee. Triggering on the data object means the manifest may not exist yet, so the handler needs retry-and-wait, and a multi-part delivery fires several times for one logical delivery.

**Whether the path or the manifest is authoritative.** Both carry the same six identifying facts. If both are read, they can disagree, and the code that resolves the disagreement is a second implementation of the identity format — the defect shape the run ledger already avoids by taking its partition key as an argument rather than re-parsing its own stored identity string.

## Decision

**A delivery lands at a prefix derived from its manifest, and the manifest — written last — is the completion marker and the trigger.**

```
<source_system>/<source_dataset>/marketplace=<m>/window=<start>_<end>/options=<hash>/retrieved=<ts>/
    manifest.json          written last; this object's creation is the trigger
    data-0001.<ext>        one or more parts, as delivered
```

Three rules follow, and they are the whole contract:

- **The prefix keys on `source_dataset`, never on a contract.** The producer names what it ran; `contracts_for` resolves which contracts read it, and there may be two.
- **The manifest is authoritative and the prefix is navigational.** Nothing parses identity out of a key. The prefix is *derived* from the manifest, so comparing where an object landed against where its manifest says it belongs is free — and that comparison turns the path into a checksum on the manifest rather than a second source of truth.
- **`retrieved=` is the last segment, so a re-pull is a new prefix.** Never an overwrite. That was already a directive; this makes it structurally awkward to violate rather than a rule to remember.

Segment order is general to specific, matching `PartitionKey.identity`. `options=` sits above `retrieved=` because two option sets for one window are different *datasets* — a `MANUFACTURING` pull and a `SOURCING` pull describe the same units under two lenses and double-count if merged — while two retrievals of one option set are *versions of one thing*, and versions should be adjacent in a listing.

**A malformed, misplaced or unrecognised landing is a rejected landing, not a quarantine.** Three failure classes now exist and must not share vocabulary: a *rejected landing* means we cannot tell which partition this is, so there is nothing to quarantine; a *skip* means we know exactly what it is and already hold it or something newer; a *quarantine* means it loaded and failed a check. Only the third concerns a partition, which is why `landing.py` raises where `assertions.py` returns a `LoadOutcome`.

## Consequences

**The instruction to the producer is one sentence.** Write the manifest last, after every data object for that delivery is completely uploaded. It is verifiable, it needs no coordination protocol, and it works with multipart uploads because a large data object's own events are simply not subscribed.

**The S3 rule is a suffix filter on `manifest.json` and nothing else.** No prefix gymnastics to subscribe, and exactly one event per delivery regardless of part count.

**End-to-end testing needs no credentials and no extraction code.** Placing a fixture bundle at a valid delivery prefix and writing the manifest last runs the real production path. That is a material improvement on the previous position, where the only way to get rows into the lake was a seed job that wrote straight into Iceberg and deliberately skipped every assertion, the DQ ruleset, the freshness registry, the watermark and quarantine.

**Two contracts per document get two ledger entries and quarantine independently**, which is right — one grain failing a check says nothing about the other. The exception is the control-total assertion, which is *about* the disagreement between them; its scope is [ADR-031](adr-031-a-control-total-failure-quarantines-every-grain.md).

**The manifest is now a validated domain type rather than a documented shape.** Every field required, none defaulted, and `option_set_hash` checked against `option_set` rather than trusted. The existing hand-built trap manifests do not conform — they carry five ad-hoc fields including our internal `contract_id` — so they are landing-contract fixtures to be regenerated, not evidence the contract works.

**`declared_columns` is required even where nothing reads it today.** Only the header assertion consumes it, and that assertion is scoped to contracts declaring suppression, which is AMC only. Requiring it for vendor deliveries too is deliberate: it is how a workflow other than the one we registered is detected, and that risk is not AMC-specific.

## Alternatives rejected

| Rejected | Why |
|---|---|
| **One directory per contract** | Cannot express one object read by two contracts, which is the actual shape of every vendor report. Also leaks our internal ids into the producer's code |
| **Trigger on the data object, wait for the manifest** | Needs retry-and-wait for a race that manifest-last removes entirely, and fires N times for an N-part delivery. The waiting is unbounded in principle: "the manifest has not arrived yet" and "the manifest is never coming" are indistinguishable |
| **Parse identity out of the key, treat the manifest as optional metadata** | Tempting because the path is right there. It makes the key format a second implementation of identity, and a producer that renames a segment then loads a delivery as something it is not |
| **Hive-style partitioning of the raw zone** | Nothing queries the raw zone — it is read once, whole, by one handler on an event. Partition pruning has nothing to prune. `key=value` naming is used for two segments purely because the producer, outside this repository, reads these paths and `marketplace=US` is unambiguous where `US` is not |
| **A `_SUCCESS` marker separate from the manifest** | The Hadoop convention, and it needs the manifest anyway. Two markers where one does both jobs, and the failure mode is a `_SUCCESS` beside a manifest that describes different objects |
| **Overwrite the same key on a re-pull, keep versions in S3 object versioning** | Makes restatement invisible in a listing and moves provenance into bucket configuration, where a policy change silently discards history. `retrieved=` keeps it in the key where it is legible |
| **Let the producer choose the layout and configure ours to match** | Considered, since they own extraction. It makes the layout per-deployment configuration, so every customer's raw zone differs and no runbook, IAM policy or debugging instruction is portable |

## Review trigger

Revisit if a source delivers a window it cannot bound — a continuous feed rather than a pull for a period. `window=` assumes a closed window, and every current source pulls one. A streaming source would need a different segment, and probably a different completion rule than a manifest, since there is no last object.
