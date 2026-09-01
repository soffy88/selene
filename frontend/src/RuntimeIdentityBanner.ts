export type RuntimeIdentity = {
  exec_mode?: string;
  funds_scope?: string;
  git_sha?: string;
  image_digest?: string;
  oos_artifact_id?: string | null;
};

export function bannerClass(identity: RuntimeIdentity): string {
  const mode = (identity.exec_mode || "PAPER").toUpperCase();
  if (mode === "AUTO_EXEC" || identity.funds_scope === "mainnet") return "identity-live";
  if (mode === "LIMITED_LIVE" || mode === "SHADOW") return "identity-shadow";
  return "identity-paper";
}

export function canShowLiveControls(identity: RuntimeIdentity): boolean {
  if (!identity.oos_artifact_id) return false;
  if ((identity.exec_mode || "").toUpperCase() === "PAPER") return false;
  return (identity.funds_scope || "") === "mainnet";
}
