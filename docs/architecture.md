# Architecture

```
Raw market data
  -> features + data manifest
  -> strategy commit + config digest
  -> OOS / CPCV / shadow
  -> signed evidence artifact
  -> release manifest
  -> startup verifier
  -> risk gate
  -> execution adapter
  -> order/fill/side-effect ledger
```

Services: scanner, signal, portfolio, risk, execution, gateway, notification, onchain, monitoring, healthcheck, sel_v2 paper engine and collectors.

Execution modes: `NOTIFY_ONLY | PAPER | SHADOW | LIMITED_LIVE | AUTO_EXEC`. Compose pins `PAPER`.
