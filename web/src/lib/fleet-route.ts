import type { ProfileInfo } from "@/lib/api";

/** Only explicit target-scoped routes are available for external homes. */
export function fleetRouteAllowed(path: string, access: "inspect" | "manage" | "execute" = "inspect"): boolean {
  return path === "/sessions" || path === "/config" || (path === "/chat" && access === "execute");
}

export function fleetSelectionBlocked(profile: string, target: ProfileInfo | undefined): boolean {
  return profile.startsWith("fleet:") && (!target || !target.external);
}
