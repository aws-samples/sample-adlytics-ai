# SPDX-License-Identifier: MIT-0
"""Run one landed delivery through the whole chain, from raw object to curated rows.

**This is layers 2 to 6 of the provenance chain, wired together for the first time.** Every
piece already existed and was unit tested — readers, the mapper, the four assertions, the
watermark decision, the SCD2 merge, the freshness registry — and nothing invoked them from a
landed object. That gap is what this closes.

It is a job rather than a Lambda handler on purpose. A tool or a load reaches AWS through a
stack that does not exist yet (`IngestionStack`), and fixing an event shape against an
unconfigured S3 notification would pin the contract to a guess. What is here is the part that
does not depend on that guess: given a manifest key, do the load. When the notification lands,
the handler's whole job is to pull a key out of an event and call this.

The order it runs in is the order the chain specifies, and each step can only refuse in its own
way — a rejected landing, a skip, or a quarantine. See `domain/curation/landing.py` for why
those three must not share vocabulary.
"""
