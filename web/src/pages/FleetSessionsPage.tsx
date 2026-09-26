import { useCallback, useEffect, useRef, useState } from "react";
import { api, type FleetTimelineResponse, type SessionInfo, type SessionMessage } from "@/lib/api";
import { useProfileScope } from "@/contexts/useProfileScope";
import { timeAgo } from "@/lib/utils";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { useNavigate } from "react-router";

const PAGE_SIZE = 20;
const MESSAGE_PAGE_SIZE = 120;

function targetId(selector: string): string {
  return selector.slice("fleet:".length);
}

/** Read every page so the exported transcript matches the selected session. */
export async function readFleetTranscript(target: string, session: string): Promise<SessionMessage[]> {
  const messages: SessionMessage[] = [];
  for (let offset = 0; ; offset += 500) {
    const page = await api.getFleetSessionMessages(target, session, offset);
    messages.push(...page.messages);
    if (page.messages.length < 500) return messages;
  }
}

export default function FleetSessionsPage() {
  const { profile, selectedTarget, canExecute } = useProfileScope();
  const navigate = useNavigate();
  const target = targetId(profile);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionInfo | null>(null);
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [hasMoreMessages, setHasMoreMessages] = useState(false);
  const [timeline, setTimeline] = useState<FleetTimelineResponse | null>(null);
  const [activePrompt, setActivePrompt] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detailRequest = useRef(0);
  const listRequest = useRef(0);
  const timelinePageInFlight = useRef<object | null>(null);
  const messagePageInFlight = useRef<object | null>(null);

  const load = useCallback(async () => {
    const request = ++listRequest.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getFleetSessions(target, PAGE_SIZE, page * PAGE_SIZE);
      if (request !== listRequest.current) return;
      setSessions(result.sessions);
      setTotal(result.total);
    } catch (cause) {
      if (request !== listRequest.current) return;
      setSessions([]);
      setError(cause instanceof Error ? cause.message : "Unable to load fleet sessions.");
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }, [page, target]);

  useEffect(() => { void load(); }, [load]);

  const expand = async (session: SessionInfo) => {
    const request = ++detailRequest.current;
    if (expanded === session.id) {
      setExpanded(null);
      return;
    }
    setExpanded(session.id);
    setDetail(null);
    setMessages([]);
    setHasMoreMessages(false);
    setTimeline(null);
    setActivePrompt(null);
    timelinePageInFlight.current = null;
    messagePageInFlight.current = null;
    setDetailLoading(true);
    setError(null);
    try {
      const [nextDetail, firstPage, nextTimeline] = await Promise.all([
        api.getFleetSessionDetail(target, session.id),
        api.getFleetSessionMessages(target, session.id, 0, MESSAGE_PAGE_SIZE),
        api.getFleetSessionTimeline(target, session.id),
      ]);
      if (request !== detailRequest.current) return;
      setDetail(nextDetail);
      setMessages(firstPage.messages);
      setHasMoreMessages(firstPage.messages.length === MESSAGE_PAGE_SIZE);
      setTimeline(nextTimeline);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to load session details.");
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const jumpToPrompt = async (sessionId: string, rowId: number) => {
    const request = ++detailRequest.current;
    setDetailLoading(true);
    setError(null);
    try {
      const page = await api.getFleetSessionMessagesAround(target, sessionId, rowId);
      if (request !== detailRequest.current) return;
      setMessages(page.messages);
      setHasMoreMessages(false);
      setActivePrompt(rowId);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to read that prompt.");
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const showTranscriptStart = async (sessionId: string) => {
    const request = ++detailRequest.current;
    setDetailLoading(true);
    setError(null);
    try {
      const firstPage = await api.getFleetSessionMessages(target, sessionId, 0, MESSAGE_PAGE_SIZE);
      if (request !== detailRequest.current) return;
      setMessages(firstPage.messages);
      setHasMoreMessages(firstPage.messages.length === MESSAGE_PAGE_SIZE);
      setActivePrompt(null);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to read the transcript.");
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const loadMoreMessages = async (sessionId: string) => {
    const request = detailRequest.current;
    const offset = messages.length;
    const requestLock = {};
    if (messagePageInFlight.current || !hasMoreMessages || activePrompt !== null) return;
    messagePageInFlight.current = requestLock;
    setError(null);
    try {
      const page = await api.getFleetSessionMessages(target, sessionId, offset, MESSAGE_PAGE_SIZE);
      if (request !== detailRequest.current) return;
      setMessages((previous) => [...previous, ...page.messages]);
      setHasMoreMessages(page.messages.length === MESSAGE_PAGE_SIZE);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to load more messages.");
    } finally {
      if (messagePageInFlight.current === requestLock) messagePageInFlight.current = null;
    }
  };

  const loadMorePrompts = async (sessionId: string) => {
    const cursor = timeline?.pagination.next_cursor;
    if (cursor == null) return;
    const requestLock = {};
    if (timelinePageInFlight.current) return;
    timelinePageInFlight.current = requestLock;
    const request = detailRequest.current;
    setError(null);
    try {
      const next = await api.getFleetSessionTimeline(target, sessionId, cursor);
      if (request !== detailRequest.current) return;
      setTimeline((previous) => {
        if (!previous) return next;
        const seen = new Set(previous.entries.map((entry) => entry.row_id));
        return { ...next, entries: [
          ...previous.entries,
          ...next.entries.filter((entry) => !seen.has(entry.row_id)),
        ] };
      });
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to load more prompts.");
    } finally {
      if (timelinePageInFlight.current === requestLock) timelinePageInFlight.current = null;
    }
  };

  const exportSession = async (session: SessionInfo) => {
    setError(null);
    try {
      const [fullSession, transcript] = await Promise.all([
        api.getFleetSessionDetail(target, session.id),
        readFleetTranscript(target, session.id),
      ]);
      const blob = new Blob([JSON.stringify({ ...fullSession, messages: transcript }, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${session.id}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to export session.");
    }
  };

  const openChat = (sessionId?: string) => {
    if (!canExecute) return;
    const params = new URLSearchParams({ profile });
    if (sessionId) params.set("resume", sessionId);
    navigate(`/chat?${params.toString()}`);
  };

  return (
    <main className="mx-auto w-full max-w-5xl space-y-4 py-5">
      <div>
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-xl font-semibold">Fleet sessions · {selectedTarget?.display_name || selectedTarget?.name}</h1>
          <div className="flex shrink-0 gap-2">
            <Button outlined size="sm" onClick={() => navigate(`/analytics?profile=${encodeURIComponent(profile)}`)}>Analytics</Button>
            {canExecute && <Button outlined size="sm" onClick={() => openChat()}>New chat</Button>}
          </div>
        </div>
        <p className="text-sm text-muted-foreground">Session history from the selected external target.</p>
      </div>
      {error && <p role="alert" className="text-sm text-warning">{error}</p>}
      {loading ? <Spinner /> : sessions.length === 0 ? <p className="text-sm text-muted-foreground">No sessions found.</p> : (
        <div className="space-y-2">
          {sessions.map((session) => (
            <div key={session.id} className="rounded-lg border border-border p-3">
              <div className="flex items-center justify-between gap-3">
                <button type="button" className="min-w-0 flex-1 text-left" onClick={() => void expand(session)} aria-expanded={expanded === session.id}>
                  <span className="block truncate text-sm font-medium">{session.title || session.id}</span>
                  <span className="text-xs text-muted-foreground">{session.source || "local"} · {timeAgo(session.started_at)} · {session.message_count} messages</span>
                </button>
                <div className="flex shrink-0 gap-2">
                  {canExecute && <Button outlined size="sm" onClick={() => openChat(session.id)} aria-label={`Resume ${session.title || session.id}`}>Resume</Button>}
                  <Button outlined size="sm" onClick={() => void exportSession(session)} aria-label={`Export ${session.title || session.id}`}>Export</Button>
                </div>
              </div>
              {expanded === session.id && (
                <div className="mt-3 space-y-3 border-t border-border pt-3">
                  {detailLoading ? <Spinner /> : detail && <>
                    <p className="text-xs text-muted-foreground">{detail.id} · {detail.model || "Unknown model"}</p>
                    {timeline && timeline.entries.length > 0 && <div className="space-y-2 rounded border border-border p-2">
                      <p className="text-xs font-semibold">Prompt timeline</p>
                      <div className="max-h-32 space-y-1 overflow-y-auto">
                        {timeline.entries.map((entry) => <button
                          key={entry.row_id} type="button"
                          className="block w-full truncate text-left text-xs underline-offset-2 hover:underline"
                          aria-label={`Jump to prompt: ${entry.preview}`}
                          onClick={() => void jumpToPrompt(session.id, entry.row_id)}
                        >{entry.preview}</button>)}
                      </div>
                      {timeline.pagination.has_more && <Button outlined size="sm" onClick={() => void loadMorePrompts(session.id)}>More prompts</Button>}
                      {activePrompt !== null && <Button outlined size="sm" onClick={() => void showTranscriptStart(session.id)}>Start of transcript</Button>}
                    </div>}
                    {messages.map((message, index) => (
                      <div key={index} className="rounded bg-secondary/30 p-3">
                        <p className="mb-1 text-xs font-semibold">{message.role}{message.tool_name ? ` · ${message.tool_name}` : ""}</p>
                        <pre className="whitespace-pre-wrap break-words font-sans text-sm">{message.content || ""}</pre>
                      </div>
                    ))}
                    {activePrompt === null && hasMoreMessages && <Button outlined size="sm" onClick={() => void loadMoreMessages(session.id)}>More messages</Button>}
                  </>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {total > PAGE_SIZE && <div className="flex items-center gap-3 text-sm">
        <Button outlined size="sm" disabled={page === 0} onClick={() => { detailRequest.current++; setExpanded(null); setPage(page - 1); }}>Previous</Button>
        <span>Page {page + 1} of {Math.ceil(total / PAGE_SIZE)}</span>
        <Button outlined size="sm" disabled={(page + 1) * PAGE_SIZE >= total} onClick={() => { detailRequest.current++; setExpanded(null); setPage(page + 1); }}>Next</Button>
      </div>}
    </main>
  );
}
