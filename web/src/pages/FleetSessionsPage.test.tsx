// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProfileContext } from "@/contexts/profile-context";
import type { ProfileInfo, SessionInfo } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  getFleetSessions: vi.fn(),
  getFleetSessionDetail: vi.fn(),
  getFleetSessionMessages: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: apiMocks }));

import FleetSessionsPage, { readFleetTranscript } from "./FleetSessionsPage";

const target = { name: "fleet:infra", external: true, display_name: "Infrastructure" } as ProfileInfo;
const session = {
  id: "session-1", title: "Deployment review", source: "cli", model: "test-model",
  started_at: Date.now() / 1000, ended_at: null, last_active: Date.now() / 1000,
  is_active: false, message_count: 2, tool_call_count: 0, input_tokens: 0,
  output_tokens: 0, preview: null,
} as SessionInfo;

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("fleet session reader", () => {
  it("expands detail and messages without exposing mutation or resume controls", async () => {
    apiMocks.getFleetSessions.mockResolvedValue({ sessions: [session], total: 1 });
    apiMocks.getFleetSessionDetail.mockResolvedValue(session);
    apiMocks.getFleetSessionMessages.mockResolvedValue({ messages: [
      { role: "user", content: "Review this" },
      { role: "assistant", content: "Reviewed" },
    ] });

    render(<ProfileContext.Provider value={{
      profile: "fleet:infra", currentProfile: "default", ready: true,
      profiles: ["default", "fleet:infra"], targets: [target], selectedTarget: target,
      access: "inspect", canManage: false, canExecute: false, setProfile: vi.fn(),
    }}><FleetSessionsPage /></ProfileContext.Provider>);

    expect(await screen.findByText("Deployment review")).toBeTruthy();
    fireEvent.click(screen.getByText("Deployment review"));
    expect(await screen.findByText("Review this")).toBeTruthy();
    expect(screen.getByText("Reviewed")).toBeTruthy();
    expect(apiMocks.getFleetSessionDetail).toHaveBeenCalledWith("infra", "session-1");
    expect(apiMocks.getFleetSessionMessages).toHaveBeenCalledWith("infra", "session-1", 0);
    for (const action of ["Delete", "Rename", "Resume", "Import", "Prune"]) {
      expect(screen.queryByRole("button", { name: new RegExp(action, "i") })).toBeNull();
    }
  });

  it("reads every message page for a complete export", async () => {
    const firstPage = Array.from({ length: 500 }, (_, index) => ({ role: "user", content: `message ${index}` }));
    apiMocks.getFleetSessionMessages
      .mockResolvedValueOnce({ messages: firstPage })
      .mockResolvedValueOnce({ messages: [{ role: "assistant", content: "last" }] });

    const messages = await readFleetTranscript("infra", "session-1");
    expect(messages).toHaveLength(501);
    expect(messages.at(-1)?.content).toBe("last");
    await waitFor(() => expect(apiMocks.getFleetSessionMessages.mock.calls.map((call) => call[2])).toEqual([0, 500]));
  });
});
