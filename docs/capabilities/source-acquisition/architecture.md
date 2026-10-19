# Source Acquisition — architecture

The [overview](README.md) owns the consumer contract. This document owns enabled-producer topology and finalization flow.

## 4. Internal structure

An external producer may assume only a destination-owned landing role and write one result plus a final registration object. The destination-owned Finalizer validates that registration, reads exact object versions, resolves governed pack identity, copies unchanged bytes to retained source storage, writes the canonical manifest last, and records finalization status. The existing ingestion path consumes only that manifest.

The registration path remains separate from ingestion because their authorities differ: Finalizer handles incoming-object custody and registration status; ingestion handles semantic publication.

## 5. Key runtime flows

1. The producer writes one immutable result and obtains its VersionId.
2. The producer writes `registration.json` last, binding that VersionId and one governed pack.
3. Registration delivery invokes the Finalizer once per registration VersionId.
4. The Finalizer validates role, external ID, bytes, source identity, and pack bindings before copying bytes unchanged.
5. The Finalizer writes the canonical manifest last; existing Semantic V2 ingestion begins only from that event.
6. Byte-identical replay converges on the recorded result. A durable rejection records status; an unresolved dependency remains retryable.
