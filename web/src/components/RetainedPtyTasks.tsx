import { Button } from "@nous-research/ui/ui/components/button";
import { AlertCircle, RefreshCw, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { api, type RetainedPtySession } from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";

type TaskState = "attached" | "background" | "ended";

function retainedPtyTaskState(task: RetainedPtySession): TaskState {
  if (!task.alive) return "ended";
  return task.attached ? "attached" : "background";
}

function taskLabel(task: RetainedPtySession): string {
  const profile = task.metadata.profile?.trim() || "default";
  return task.metadata.resume ? `${profile} · resumed chat` : `${profile} · new chat`;
}

const STATE_CLASS: Record<TaskState, string> = {
  attached: "border-success/30 bg-success/15 text-success",
  background: "border-warning/30 bg-warning/15 text-warning",
  ended: "border-midground/15 bg-midground/8 text-midground",
};

/** Compact control for live dashboard chats retained by the server. */
export function RetainedPtyTasks({ className }: { className?: string }) {
  const [tasks, setTasks] = useState<RetainedPtySession[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingStop, setPendingStop] = useState<RetainedPtySession | null>(null);
  const [stopping, setStopping] = useState(false);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    setLoading(true);
    try {
      const response = await api.getRetainedPtySessions();
      if (request !== requestRef.current) return;
      setTasks(response.sessions);
      setError(null);
    } catch {
      if (request !== requestRef.current) return;
      setError("Background tasks are unavailable.");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Defer the initial state transition out of the effect body. Polling is an
    // external subscription, and using the same timer path for its first tick
    // avoids a synchronous effect -> setState cascade.
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 15_000);
    return () => {
      requestRef.current += 1;
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);

  const stop = useCallback(async () => {
    if (!pendingStop) return;
    setStopping(true);
    try {
      await api.stopRetainedPtySession(pendingStop.id);
      setPendingStop(null);
      await load();
    } catch {
      setError("Could not stop that task. Try again.");
    } finally {
      setStopping(false);
    }
  }, [load, pendingStop]);

  return (
    <div
      className={cn(
        "min-w-0 rounded border border-border/60 px-2 py-2",
        className,
      )}
    >
      <div className="flex items-center justify-between gap-2 px-1">
        <div>
          <div className="text-display text-xs tracking-wider text-text-tertiary">
            background tasks
          </div>
          <div className="text-xs text-text-secondary">
            {tasks === null ? "Checking…" : `${tasks.length} retained`}
          </div>
        </div>
        <Button
          ghost
          size="icon"
          aria-label="Refresh background tasks"
          title="Refresh background tasks"
          disabled={loading}
          onClick={() => void load()}
        >
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
        </Button>
      </div>

      {error && (
        <div className="mt-2 flex items-start gap-1.5 px-1 text-xs text-destructive">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {tasks && tasks.length > 0 && (
        <div className="mt-2 max-h-36 space-y-1 overflow-y-auto">
          {tasks.map((task) => {
            const state = retainedPtyTaskState(task);
            return (
              <div
                key={task.id}
                className="flex min-w-0 items-center gap-2 border-t border-border/60 px-1 pt-1.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-text-primary">
                    {taskLabel(task)}
                  </div>
                  <div className="text-xs text-text-tertiary">
                    started {timeAgo(task.created_at)}
                    {task.buffer_truncated ? " · earlier output trimmed" : ""}
                  </div>
                </div>
                <span
                  className={cn(
                    "inline-flex shrink-0 items-center border px-2 py-1 text-xs leading-none",
                    STATE_CLASS[state],
                  )}
                >
                  {state}
                </span>
                <Button
                  ghost
                  size="icon"
                  aria-label={`Stop ${taskLabel(task)} background task`}
                  title="Stop task"
                  onClick={() => setPendingStop(task)}
                  disabled={!task.alive}
                >
                  <Square className="h-3 w-3" />
                </Button>
              </div>
            );
          })}
        </div>
      )}

      {tasks?.length === 0 && !error && (
        <div className="mt-2 px-1 text-xs text-text-tertiary">
          No background tasks.
        </div>
      )}

      <ConfirmDialog
        open={pendingStop !== null}
        destructive
        loading={stopping}
        title="Stop this task?"
        description="Any work still running in this chat will end. The saved conversation remains in Sessions."
        confirmLabel="Stop task"
        onCancel={() => setPendingStop(null)}
        onConfirm={() => void stop()}
      />
    </div>
  );
}
