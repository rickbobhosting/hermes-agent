// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useProfileScope } from "./useProfileScope";

const apiMocks = vi.hoisted(() => ({
  getProfiles: vi.fn(),
  getActiveProfile: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  setManagementProfile: vi.fn(),
}));

import { ProfileProvider } from "./ProfileProvider";

function ScopeState() {
  const { profile, currentProfile, ready } = useProfileScope();
  return (
    <div>
      <span data-testid="ready">{String(ready)}</span>
      <span data-testid="profile">{profile || "unset"}</span>
      <span data-testid="current">{currentProfile}</span>
    </div>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

describe("ProfileProvider readiness", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("stays unready until the real current and active profiles settle", async () => {
    const profiles = deferred<{ profiles: { name: string }[] }>();
    const active = deferred<{ current: string; active: string }>();
    apiMocks.getProfiles.mockReturnValue(profiles.promise);
    apiMocks.getActiveProfile.mockReturnValue(active.promise);

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ProfileProvider>
          <ScopeState />
        </ProfileProvider>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("ready").textContent).toBe("false");
    expect(screen.getByTestId("current").textContent).toBe("default");

    profiles.resolve({ profiles: [{ name: "work" }] });
    active.resolve({ current: "custom-host", active: "work" });

    await waitFor(() =>
      expect(screen.getByTestId("ready").textContent).toBe("true"),
    );
    expect(screen.getByTestId("current").textContent).toBe("custom-host");
    expect(screen.getByTestId("profile").textContent).toBe("work");
  });

  it("becomes ready after bootstrap failure so the fallback can proceed", async () => {
    apiMocks.getProfiles.mockRejectedValue(new Error("offline"));
    apiMocks.getActiveProfile.mockResolvedValue({
      current: "default",
      active: "default",
    });

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ProfileProvider>
          <ScopeState />
        </ProfileProvider>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("ready").textContent).toBe("true"),
    );
    expect(screen.getByTestId("current").textContent).toBe("default");
  });
});
