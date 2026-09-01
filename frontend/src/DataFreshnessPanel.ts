export function freshnessTone(ageSeconds: number | null): "ok" | "stale" | "unknown" {
  if (ageSeconds == null || Number.isNaN(ageSeconds)) return "unknown";
  if (ageSeconds > 90) return "stale";
  return "ok";
}
