import { canShowLiveControls, type RuntimeIdentity } from "./RuntimeIdentityBanner.ts";

export function liveButtonDisabled(identity: RuntimeIdentity): boolean {
  return !canShowLiveControls(identity);
}
