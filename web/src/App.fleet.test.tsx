// @vitest-environment jsdom

import { Suspense } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/contexts/useProfileScope", () => ({
  useProfileScope: () => ({
    profile: "fleet:infra",
    selectedTarget: { name: "fleet:infra", external: true, access: "manage" },
    access: "manage",
  }),
}));
vi.mock("@/pages/ConfigPage", () => ({
  default: () => <div>Built-in scoped config</div>,
}));

import { FleetRouteGuard } from "./App";

afterEach(cleanup);

describe("fleet route overrides", () => {
  it("renders the built-in config page even when a plugin supplies the route element", async () => {
    render(<Suspense fallback={<div>Loading config</div>}>
      <FleetRouteGuard path="/config">
        <button>Host plugin config action</button>
      </FleetRouteGuard>
    </Suspense>);
    expect(await screen.findByText("Built-in scoped config")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Host plugin config action" })).toBeNull();
  });
});
