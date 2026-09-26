import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { KanbanTask, KanbanTaskComment, KanbanTaskEvent } from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { LoadErrorNotice } from "@/components/LoadErrorNotice";
import { errorMessage } from "@/lib/api-error";
import { usePageHeader } from "@/contexts/usePageHeader";

const STATUS_TONE: Record<string, "success" | "warning" | "destructive" | "secondary"> = {
  done: "success",
  running: "warning",
  ready: "secondary",
  todo: "secondary",
  blocked: "destructive",
  triage: "destructive",
};

function statusTone(status: string): "success" | "warning" | "destructive" | "secondary" {
  return STATUS_TONE[status] ?? "secondary";
}

function formatTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function TaskDetail({ taskId }: { taskId: string }) {
  const [task, setTask] = useState<KanbanTask | null>(null);
  const [comments, setComments] = useState<KanbanTaskComment[]>([]);
  const [events, setEvents] = useState<KanbanTaskEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchDetail = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getKanbanTask(taskId)
      .then((resp) => {
        setTask(resp.task);
        setComments(resp.comments);
        setEvents(resp.events);
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setLoading(false));
  }, [taskId]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  if (loading && !task) return <Spinner />;
  if (error) return <LoadErrorNotice what="this task" detail={error} onRetry={fetchDetail} />;
  if (!task) return null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <Badge tone={statusTone(task.status)}>{task.status}</Badge>
        <span className="font-medium">{task.title}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm text-muted-foreground">
        <div>Assignee: {task.assignee ?? "—"}</div>
        <div>Workspace: {task.workspace_kind ?? "—"}</div>
        <div>Started: {formatTime(task.started_at)}</div>
        <div>Completed: {formatTime(task.completed_at)}</div>
        <div>Consecutive failures: {task.consecutive_failures ?? 0}</div>
        <div>Worker PID: {task.worker_pid ?? "—"}</div>
      </div>
      {task.last_failure_error && (
        <div className="rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive">
          {task.last_failure_error}
        </div>
      )}
      <div>
        <div className="mb-1 text-sm font-medium">
          Comments ({comments.length})
        </div>
        <div className="flex flex-col gap-2">
          {comments.length === 0 && (
            <div className="text-sm text-muted-foreground">No comments yet.</div>
          )}
          {comments.map((c, i) => (
            <Card key={i}>
              <CardContent className="p-3 text-sm">
                <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">{c.author}</span>
                  <span>{formatTime(c.created_at)}</span>
                </div>
                <pre className="whitespace-pre-wrap break-words font-sans">{c.body}</pre>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
      <div>
        <div className="mb-1 text-sm font-medium">
          Recent events ({events.length})
        </div>
        <div className="flex flex-col gap-1 text-xs">
          {events.map((e, i) => (
            <div key={i} className="flex items-center gap-2 text-muted-foreground">
              <span className="w-40 shrink-0">{formatTime(e.created_at)}</span>
              <Badge tone="secondary" className="shrink-0">{e.kind}</Badge>
              {e.run_id != null && <span>run #{e.run_id}</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function KanbanPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [profile, setProfile] = useState(searchParams.get("profile") ?? "");
  const [status, setStatus] = useState(searchParams.get("status") ?? "");
  const [tasks, setTasks] = useState<KanbanTask[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const { setAfterTitle, setEnd } = usePageHeader();
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchTasks = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getKanbanTasks({ profile: profile || undefined, status: status || undefined, limit: 100 })
      .then((resp) => setTasks(resp.tasks))
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setLoading(false));
  }, [profile, status]);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  useEffect(() => {
    if (!autoRefresh) return;
    intervalRef.current = setInterval(fetchTasks, 5000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [autoRefresh, fetchTasks]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (profile) params.set("profile", profile);
    if (status) params.set("status", status);
    setSearchParams(params, { replace: true });
  }, [profile, status, setSearchParams]);

  useLayoutEffect(() => {
    setAfterTitle(
      <span className="flex items-center gap-1.5">
        <Badge tone="secondary" className="text-xs">
          {tasks.length} task{tasks.length === 1 ? "" : "s"}
        </Badge>
        <Button
          type="button"
          ghost
          size="icon"
          className="text-muted-foreground hover:text-foreground"
          onClick={fetchTasks}
          disabled={loading}
          aria-label="Refresh"
        >
          {loading ? <Spinner /> : <RefreshCw />}
        </Button>
      </span>,
    );
    setEnd(
      <div className="flex items-center gap-2">
        <Label htmlFor="kanban-auto-refresh" className="text-xs cursor-pointer">
          Auto-refresh
        </Label>
        <Switch id="kanban-auto-refresh" checked={autoRefresh} onCheckedChange={setAutoRefresh} />
      </div>,
    );
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [tasks.length, loading, autoRefresh, fetchTasks, setAfterTitle, setEnd]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 p-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="kanban-profile-filter" className="text-xs">
            Profile
          </Label>
          <Input
            id="kanban-profile-filter"
            placeholder="all profiles"
            value={profile}
            onChange={(e) => setProfile(e.target.value)}
            className="w-48"
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="kanban-status-filter" className="text-xs">
            Status
          </Label>
          <Select
            value={status}
            onValueChange={setStatus}
            id="kanban-status-filter"
            className="w-40"
          >
            <SelectOption value="">any status</SelectOption>
            <SelectOption value="ready">ready</SelectOption>
            <SelectOption value="running">running</SelectOption>
            <SelectOption value="todo">todo</SelectOption>
            <SelectOption value="blocked">blocked</SelectOption>
            <SelectOption value="triage">triage</SelectOption>
            <SelectOption value="done">done</SelectOption>
          </Select>
        </div>
      </div>

      {error && <LoadErrorNotice what="kanban tasks" detail={error} onRetry={fetchTasks} />}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <div className="flex min-h-0 flex-col gap-2 overflow-y-auto pr-1">
          {tasks.length === 0 && !loading && (
            <div className="text-sm text-muted-foreground">No tasks match this filter.</div>
          )}
          {tasks.map((t) => (
            <Card
              key={t.id}
              className={
                "cursor-pointer transition-colors " +
                (selectedId === t.id ? "border-primary" : "hover:border-muted-foreground/40")
              }
              onClick={() => setSelectedId(t.id)}
            >
              <CardContent className="flex flex-col gap-1 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium">{t.title}</span>
                  <Badge tone={statusTone(t.status)} className="shrink-0">
                    {t.status}
                  </Badge>
                </div>
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span>{t.assignee ?? "unassigned"}</span>
                  <span>·</span>
                  <span>{t.id}</span>
                  <span>·</span>
                  <span>{formatTime(t.created_at)}</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
        <div className="min-h-0 overflow-y-auto rounded border p-4">
          {selectedId ? (
            <TaskDetail taskId={selectedId} />
          ) : (
            <div className="text-sm text-muted-foreground">
              Select a task on the left to see its comments and event history.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
