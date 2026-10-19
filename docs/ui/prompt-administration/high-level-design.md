# Prompt Administration — high-level design

## 1. Topology

Three parts, one new:

1. **Prompt Registry API** (new) — operator-authenticated routes on the existing API boundary:
   list slots, list versions, read one version, create version, activate version, retire slot.
   Reads are safe projections; writes are the four lifecycle transitions and nothing else.
2. **Registry store** — one table: immutable version records keyed by `(slot_id, version)` plus
   one activation pointer record per slot. A version row is write-once; the pointer is the only
   mutable record and every pointer change appends an activation event.
3. **Calibration runner** — executes a version's declared calibration cases (fictional, per the
   evaluation-validity rules) and writes a pass/fail result onto the version record. Activation
   reads that result; the API refuses to activate an unpassed version.

The insight generation path reads only the activation pointer at generation time and stamps the
version it used into the generated insight's cache record and provenance. Nothing at generation
time consults this page or its API.

## 2. Lifecycle

```
draft version created ──► calibration runs ──► passed ──► activatable
                                   │
                                   └─► failed ──► visible with failing cases; never activatable
activate(vN) ──► pointer moves, activation event appended, slot cache for prior version is dead
retire(slot) ──► pointer cleared; slot renders its Limit state on pages; history retained
```

Rollback is `activate` pointed at an earlier passed version — there is no separate mechanism.

Every declared slot's initial prompt (from its insight contract) is seeded as version 1 by an
idempotent deploy-time step and activated once its calibration passes, so the generation path
never finds an empty registry and a fresh environment needs no manual bootstrap.

## 3. Failure containment

- A registry outage never breaks insight pages: generation reads the pointer through the same
  cached configuration path as other governed configuration, and pages render cached insights
  or the slot's designed absence state.
- A half-completed activation cannot exist: pointer move and activation event are one
  conditional write.
- Deleting is not a capability. Retire hides a slot from generation; history remains readable
  because cached insights and audits reference version identities.

## 4. Authorization

Registry routes sit behind the operator authorization boundary (ADR-060 precedent). The
workspace shell shows the Admin section only when the session carries operator authorization;
the API enforces it independently on every route.
