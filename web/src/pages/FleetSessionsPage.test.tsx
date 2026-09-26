// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";
import { ProfileContext } from "@/contexts/profile-context";
import type { ProfileInfo, SessionInfo } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  getFleetSessions: vi.fn(),
  getFleetSessionDetail: vi.fn(),
  getFleetSessionMessages: vi.fn(),
  getFleetSessionTimeline: vi.fn(),
  getFleetSessionMessagesAround: vi.fn(),
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
    apiMocks.getFleetSessionTimeline.mockResolvedValue({
      entries: [{ row_id: 17, preview: "Review this", timestamp: 10 }],
      pagination: { has_more: false, next_cursor: null },
    });
    apiMocks.getFleetSessionMessagesAround.mockResolvedValue({ messages: [
      { role: "user", content: "Review this" },
      { role: "assistant", content: "Focused answer" },
    ] });

    render(<MemoryRouter><ProfileContext.Provider value={{
      profile: "fleet:infra", currentProfile: "default", ready: true,
      profiles: ["default", "fleet:infra"], targets: [target], selectedTarget: target,
      access: "inspect", canManage: false, canExecute: false, setProfile: vi.fn(),
    }}><FleetSessionsPage /></ProfileContext.Provider></MemoryRouter>);

    expect(await screen.findByText("Deployment review")).toBeTruthy();
    fireEvent.click(screen.getByText("Deployment review"));
    expect(await screen.findAllByText("Review this")).toHaveLength(2);
    expect(screen.getByText("Reviewed")).toBeTruthy();
    expect(apiMocks.getFleetSessionDetail).toHaveBeenCalledWith("infra", "session-1");
    expect(apiMocks.getFleetSessionMessages).toHaveBeenCalledWith("infra", "session-1", 0, 120);
    expect(apiMocks.getFleetSessionTimeline).toHaveBeenCalledWith("infra", "session-1");
    fireEvent.click(screen.getByRole("button", { name: "Jump to prompt: Review this" }));
    expect(await screen.findByText("Focused answer")).toBeTruthy();
    expect(apiMocks.getFleetSessionMessagesAround).toHaveBeenCalledWith("infra", "session-1", 17);
    fireEvent.click(screen.getByRole("button", { name: "Start of transcript" }));
    expect(await screen.findByText("Reviewed")).toBeTruthy();
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

  it("loads one transcript page on expansion and only one page per prompt cursor", async () => {
    let resolveMore!: (value: unknown) => void;
    const more = new Promise((resolve) => { resolveMore = resolve; });
    apiMocks.getFleetSessions.mockResolvedValue({ sessions: [session], total: 1 });
    apiMocks.getFleetSessionDetail.mockResolvedValue(session);
    apiMocks.getFleetSessionMessages.mockResolvedValue({ messages: [{ role: "user", content: "First page" }] });
    apiMocks.getFleetSessionTimeline
      .mockResolvedValueOnce({ entries: [{ row_id: 17, preview: "First prompt", timestamp: 10 }],
        pagination: { has_more: true, next_cursor: 17 } })
      .mockReturnValueOnce(more);

    render(<MemoryRouter><ProfileContext.Provider value={{
      profile: "fleet:infra", currentProfile: "default", ready: true,
      profiles: ["default", "fleet:infra"], targets: [target], selectedTarget: target,
      access: "inspect", canManage: false, canExecute: false, setProfile: vi.fn(),
    }}><FleetSessionsPage /></ProfileContext.Provider></MemoryRouter>);

    fireEvent.click(await screen.findByText("Deployment review"));
    expect(await screen.findByText("First page")).toBeTruthy();
    expect(apiMocks.getFleetSessionMessages).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "More prompts" }));
    fireEvent.click(screen.getByRole("button", { name: "More prompts" }));
    expect(apiMocks.getFleetSessionTimeline).toHaveBeenCalledTimes(2);
    expect(apiMocks.getFleetSessionTimeline).toHaveBeenLastCalledWith("infra", "session-1", 17);
    resolveMore({ entries: [{ row_id: 18, preview: "Second prompt", timestamp: 11 }],
      pagination: { has_more: false, next_cursor: null } });
    expect(await screen.findByRole("button", { name: "Jump to prompt: Second prompt" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Jump to prompt: Second prompt" })).toHaveLength(1);
  });

  it("keeps a reopened session's message-page lock when an older request settles", async () => {
    let resolveOld!: (value: unknown) => void;
    let resolveCurrent!: (value: unknown) => void;
    const oldPage = new Promise((resolve) => { resolveOld = resolve; });
    const currentPage = new Promise((resolve) => { resolveCurrent = resolve; });
    const firstPage = { messages: Array.from({ length: 120 }, (_, index) => ({
      role: "user", content: `first ${index}`,
    })) };
    apiMocks.getFleetSessions.mockResolvedValue({ sessions: [session], total: 1 });
    apiMocks.getFleetSessionDetail.mockResolvedValue(session);
    apiMocks.getFleetSessionTimeline.mockResolvedValue({ entries: [], pagination: { has_more: false, next_cursor: null } });
    apiMocks.getFleetSessionMessages
      .mockResolvedValueOnce(firstPage)
      .mockReturnValueOnce(oldPage)
      .mockResolvedValueOnce(firstPage)
      .mockReturnValueOnce(currentPage);

    render(<MemoryRouter><ProfileContext.Provider value={{
      profile: "fleet:infra", currentProfile: "default", ready: true,
      profiles: ["default", "fleet:infra"], targets: [target], selectedTarget: target,
      access: "inspect", canManage: false, canExecute: false, setProfile: vi.fn(),
    }}><FleetSessionsPage /></ProfileContext.Provider></MemoryRouter>);

    fireEvent.click(await screen.findByText("Deployment review"));
    expect(await screen.findByRole("button", { name: "More messages" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "More messages" }));
    fireEvent.click(screen.getByText("Deployment review"));
    fireEvent.click(screen.getByText("Deployment review"));
    expect(await screen.findByRole("button", { name: "More messages" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "More messages" }));
    expect(apiMocks.getFleetSessionMessages).toHaveBeenCalledTimes(4);

    resolveOld({ messages: [{ role: "assistant", content: "stale page" }] });
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.click(screen.getByRole("button", { name: "More messages" }));
    expect(apiMocks.getFleetSessionMessages).toHaveBeenCalledTimes(4);
    resolveCurrent({ messages: [{ role: "assistant", content: "current page" }] });
    expect(await screen.findByText("current page")).toBeTruthy();
    expect(screen.queryByText("stale page")).toBeNull();
  });

  it("offers exact-target chat only when execute is granted", async () => {
    apiMocks.getFleetSessions.mockResolvedValue({ sessions: [session], total: 1 });
    function Location() {
      const location = useLocation();
      return <span data-testid="location">{location.pathname}{location.search}</span>;
    }
    render(<MemoryRouter initialEntries={["/sessions?profile=fleet%3Ainfra"]}>
      <ProfileContext.Provider value={{
        profile: "fleet:infra", currentProfile: "default", ready: true,
        profiles: ["default", "fleet:infra"], targets: [target], selectedTarget: target,
        access: "execute", canManage: false, canExecute: true, setProfile: vi.fn(),
      }}><FleetSessionsPage /><Location /></ProfileContext.Provider>
    </MemoryRouter>);
    expect(await screen.findByRole("button", { name: "Resume Deployment review" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume Deployment review" }));
    expect(screen.getByTestId("location").textContent).toBe("/chat?profile=fleet%3Ainfra&resume=session-1");
    fireEvent.click(screen.getByRole("button", { name: "Analytics" }));
    expect(screen.getByTestId("location").textContent).toBe("/analytics?profile=fleet%3Ainfra");
  });
});
