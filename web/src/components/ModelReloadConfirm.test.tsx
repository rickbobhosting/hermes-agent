// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ModelReloadConfirm } from "./ModelReloadConfirm";

describe("ModelReloadConfirm", () => {
  it("describes future-turn eligibility without promising a fresh chat", () => {
    const onCancel = vi.fn();
    render(
      <ModelReloadConfirm model="new-model" onCancel={onCancel} />,
    );

    expect(screen.getByText(/eligible unpinned chats/i)).toBeTruthy();
    expect(screen.queryByText(/starts a fresh chat/i)).toBeNull();
    expect(
      screen.getByRole("button", { name: "Reload dashboard" }),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
