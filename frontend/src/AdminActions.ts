export type WriteHeaders = {
  requestId: string;
  actor: string;
  reason: string;
};

export function assertHighRiskReason(headers: WriteHeaders): void {
  if (!headers.reason.trim() || !headers.actor.trim() || !headers.requestId.trim()) {
    throw new Error("high-risk actions require actor, request id, and reason");
  }
}
