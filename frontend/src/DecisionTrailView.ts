export function formatTrail(action: string, reason: string): string {
  return `${action} · ${reason}`;
}
