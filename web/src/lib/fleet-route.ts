import type { ProfileInfo } from "@/lib/api";

/** Only the session reader has an external-home contract in this dashboard. */
export function fleetRouteAllowed(path: string): boolean {
  return path === "/sessions";
}

export function fleetSelectionBlocked(profile: string, target: ProfileInfo | undefined): boolean {
  return profile.startsWith("fleet:") && (!target || !target.external);
}
