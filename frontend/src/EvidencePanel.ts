export function evidenceLabel(verdict: string | undefined, expiresAt: string | undefined): string {
  if (!verdict) return "NO_ARTIFACT";
  if (verdict !== "PASS") return "NO_GO";
  if (expiresAt && Date.parse(expiresAt) < Date.now()) return "EXPIRED";
  return "PASS";
}
