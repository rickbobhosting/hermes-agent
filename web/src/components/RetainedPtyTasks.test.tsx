// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { RetainedPtyTasks } from "./RetainedPtyTasks";

describe("RetainedPtyTasks", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows safe task state and requires confirmation before stopping", async () => {
    const getSessions = vi.spyOn(api, "getRetainedPtySessions").mockResolvedValue({
      sessions: [
        {
          id: "public-management-id",
          alive: true,
          attached: false,
          created_at: Date.now() / 1000,
          last_attached_at: null,
          last_detached_at: Date.now() / 1000,
          buffer_bytes: 128,
          buffer_truncated: false,
          metadata: { profile: "default", resume: null },
        },
        {
          id: "attached-management-id",
          alive: true,
          attached: true,
          created_at: Date.now() / 1000,
          last_attached_at: Date.now() / 1000,
          last_detached_at: null,
          buffer_bytes: 256,
          buffer_truncated: false,
          metadata: { profile: "work", resume: "saved-session" },
        },
        {
          id: "ended-management-id",
          alive: false,
          attached: false,
          created_at: Date.now() / 1000,
          last_attached_at: null,
          last_detached_at: Date.now() / 1000,
          buffer_bytes: 0,
          buffer_truncated: false,
          metadata: { profile: "archive", resume: null },
        },
      ],
    });
    const stop = vi
      .spyOn(api, "stopRetainedPtySession")
      .mockResolvedValue({ ok: true });

    render(<RetainedPtyTasks />);

    expect(await screen.findByText("background")).toBeTruthy();
    expect(screen.getByText("attached")).toBeTruthy();
    expect(screen.getByText("ended")).toBeTruthy();
    expect(screen.getByText("default · new chat")).toBeTruthy();
    expect(screen.queryByText("public-management-id")).toBeNull();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Stop default · new chat background task",
      }),
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(stop).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Stop task" }));
    await waitFor(() => expect(stop).toHaveBeenCalledWith("public-management-id"));
    await waitFor(() => expect(getSessions).toHaveBeenCalledTimes(2));
  });
});
