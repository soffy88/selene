export function isTerminal(state: string): boolean {
  return ["CLOSED", "CANCELLED", "FAILED", "QUARANTINED"].includes(state);
}
