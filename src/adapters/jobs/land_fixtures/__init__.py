# SPDX-License-Identifier: MIT-0
"""Land committed fixtures in the raw zone as conforming deliveries.

**This stands in for the extraction team, and only for testing.** It writes exactly what
[ADR-030](../../../../docs/decisions/adr-030-the-raw-zone-landing-contract.md) says a producer
writes: data objects at a delivery prefix derived from the manifest, then the manifest last.
Nothing here parses a source API, holds a credential or decides what to extract.

Why it exists rather than a shortcut into the lake: writing rows straight into Iceberg skips every
assertion, the DQ ruleset, the freshness registry, the watermark and quarantine, so a row
production would reject gets inserted without complaint. A shortcut like that used to be the only
way to fill a dev table and has been retired. Landing a delivery instead means the *real* path
runs, and the only thing still simulated is who put the bytes in the bucket.

It is also the reference example. The bundle this produces is what a conforming delivery looks
like, so it can be handed to whoever runs extraction as an artefact rather than as prose.
"""
