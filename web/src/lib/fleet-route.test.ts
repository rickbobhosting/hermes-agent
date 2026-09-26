import { describe, expect, it } from "vitest";
import { fleetRouteAllowed, fleetSelectionBlocked } from "./fleet-route";
import type { ProfileInfo } from "./api";

const registered = { name: "fleet:infra", external: true } as ProfileInfo;

describe("external fleet page boundary", () => {
  it("allows only the scoped session reader, including against plugin routes", () => {
    expect(fleetRouteAllowed("/sessions")).toBe(true);
    expect(fleetRouteAllowed("/config")).toBe(true);
    expect(fleetRouteAllowed("/analytics")).toBe(true);
    expect(fleetRouteAllowed("/chat", "execute")).toBe(true);
    expect(fleetRouteAllowed("/chat", "inspect")).toBe(false);
    for (const path of ["/", "/profiles", "/cron", "/files", "/plugin-tab"]) {
      expect(fleetRouteAllowed(path)).toBe(false);
    }
  });

  it("holds deep links until the selector resolves to a registered external target", () => {
    expect(fleetSelectionBlocked("fleet:infra", undefined)).toBe(true);
    expect(fleetSelectionBlocked("fleet:infra", { name: "fleet:infra", external: false } as ProfileInfo)).toBe(true);
    expect(fleetSelectionBlocked("fleet:infra", registered)).toBe(false);
    expect(fleetSelectionBlocked("work", undefined)).toBe(false);
  });
});
