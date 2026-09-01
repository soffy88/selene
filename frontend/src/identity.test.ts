import assert from "node:assert/strict";
import test from "node:test";
import { bannerClass, canShowLiveControls } from "./RuntimeIdentityBanner.ts";
import { liveButtonDisabled } from "./ExecutionModeGuard.ts";
import { freshnessTone } from "./DataFreshnessPanel.ts";
import { evidenceLabel } from "./EvidencePanel.ts";
import { isTerminal } from "./OrderLifecycleView.ts";
import { divergenceKind } from "./ReconciliationView.ts";
import { assertHighRiskReason } from "./AdminActions.ts";

test("paper identity is not live", () => {
  const id = { exec_mode: "PAPER", funds_scope: "paper" };
  assert.equal(bannerClass(id), "identity-paper");
  assert.equal(canShowLiveControls(id), false);
  assert.equal(liveButtonDisabled(id), true);
});

test("stale data is flagged", () => {
  assert.equal(freshnessTone(5), "ok");
  assert.equal(freshnessTone(120), "stale");
});

test("expired evidence blocks live label", () => {
  assert.equal(evidenceLabel("PASS", "2000-01-01T00:00:00Z"), "EXPIRED");
});

test("quarantine is terminal", () => {
  assert.equal(isTerminal("QUARANTINED"), true);
});

test("ghost venue", () => {
  assert.equal(divergenceKind(0, 1), "ghost_venue");
});

test("admin reason required", () => {
  assert.throws(() => assertHighRiskReason({ requestId: "", actor: "a", reason: "x" }));
});
