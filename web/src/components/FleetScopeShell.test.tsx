// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const scope = vi.hoisted(() => ({
  profile: "fleet:infra",
  currentProfile: "default",
  selectedTarget: undefined as undefined | { display_name: string },
  canManage: false,
  canExecute: false,
}));
const pluginSlot = vi.hoisted(() => vi.fn(({ name }: { name: string }) => <button>{name} action</button>));

vi.mock("@/contexts/useProfileScope", () => ({ useProfileScope: () => scope }));
vi.mock("@/i18n", () => ({ useI18n: () => ({ t: { app: {} } }) }));
vi.mock("@/plugins", () => ({ PluginSlot: pluginSlot }));

import { ProfileScopeBanner } from "./ProfileScopeBanner";
import { FleetScopedPluginSlot } from "./FleetScopedPluginSlot";

describe("fleet shell scope", () => {
  afterEach(() => {
    cleanup();
    scope.profile = "fleet:infra";
    scope.currentProfile = "default";
    scope.selectedTarget = undefined;
    scope.canManage = false;
    scope.canExecute = false;
    pluginSlot.mockClear();
  });

  it("renders an unresolved deep link before target registration completes", () => {
    render(<ProfileScopeBanner />);
    expect(screen.getByText(/Inspecting “fleet:infra”/)).toBeTruthy();
    scope.selectedTarget = { display_name: "Infrastructure" };
    cleanup();
    render(<ProfileScopeBanner />);
    expect(screen.getByText(/Inspecting “Infrastructure”/)).toBeTruthy();
    scope.canExecute = true;
    cleanup();
    render(<ProfileScopeBanner />);
    expect(screen.getByText(/chat and config changes target this fleet profile/)).toBeTruthy();
  });

  it("does not mount plugin controls for fleet targets, but preserves local slots", () => {
    const view = render(<FleetScopedPluginSlot name="header-right" />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(pluginSlot).not.toHaveBeenCalled();

    scope.profile = "default";
    view.rerender(<FleetScopedPluginSlot name="header-right" />);
    expect(screen.getByRole("button", { name: "header-right action" })).toBeTruthy();
    expect(pluginSlot).toHaveBeenCalledOnce();
  });
});
