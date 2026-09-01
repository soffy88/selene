# Evidence directory

Immutable, digest-bound artifacts for Selene closure. Large data stays outside git;
manifests, digests, generator commands, and object references are committed.

```
evidence/
  schemas/
  oos/
  shadow/
  releases/
  smoke/
  security/
  incidents/
  approvals/
  closure/
```

Rules:

- Artifacts are produced by CI/qualification jobs, not by editing production env vars.
- `I_HAVE_OOS_EVIDENCE=yes` is not evidence.
- `gate_verdict=FAIL`, unknown schema, digest mismatch, or expiry is refuse-to-boot.
- Live/backfill/shadow/paper data must not be mixed in live metrics.
