# Saturation — architecture

## 4. Internal structure

A scheduled offline fit reads exact selected history, produces a candidate response curve, evaluates convergence and fit diagnostics, and writes an immutable accepted artifact only after validation. Runtime readers consume that artifact; they never run fitting or infer causal allocation at request time.

## 5. Key runtime flow

1. Bind declared channel history to exact successful publications.
2. Run an offline fit with pinned configuration.
3. Reject non-converged, insufficient, or incompatible candidates.
4. Publish one immutable validated fit and conditionally advance its pointer.
5. Serve exact fit state or a typed unavailable/stale outcome.
