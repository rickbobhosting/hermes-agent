import { useCallback, useEffect, useRef, useState } from "react";
import { api, type FleetTimelineResponse, type SessionInfo, type SessionMessage } from "@/lib/api";
import { useProfileScope } from "@/contexts/useProfileScope";
import { timeAgo } from "@/lib/utils";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

const PAGE_SIZE = 20;

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
  const { profile, selectedTarget } = useProfileScope();
  const target = targetId(profile);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionInfo | null>(null);
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [timeline, setTimeline] = useState<FleetTimelineResponse | null>(null);
  const [activePrompt, setActivePrompt] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detailRequest = useRef(0);
  const listRequest = useRef(0);

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
    setTimeline(null);
    setActivePrompt(null);
    setDetailLoading(true);
    setError(null);
    try {
      const [nextDetail, nextMessages, nextTimeline] = await Promise.all([
        api.getFleetSessionDetail(target, session.id),
        readFleetTranscript(target, session.id),
        api.getFleetSessionTimeline(target, session.id),
      ]);
      if (request !== detailRequest.current) return;
      setDetail(nextDetail);
      setMessages(nextMessages);
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
      setActivePrompt(rowId);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to read that prompt.");
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const showFullTranscript = async (sessionId: string) => {
    const request = ++detailRequest.current;
    setDetailLoading(true);
    setError(null);
    try {
      const full = await readFleetTranscript(target, sessionId);
      if (request !== detailRequest.current) return;
      setMessages(full);
      setActivePrompt(null);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to read the transcript.");
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const loadMorePrompts = async (sessionId: string) => {
    const cursor = timeline?.pagination.next_cursor;
    if (cursor == null) return;
    const request = detailRequest.current;
    setError(null);
    try {
      const next = await api.getFleetSessionTimeline(target, sessionId, cursor);
      if (request !== detailRequest.current) return;
      setTimeline((previous) => previous ? {
        ...next, entries: [...previous.entries, ...next.entries],
      } : next);
    } catch (cause) {
      if (request !== detailRequest.current) return;
      setError(cause instanceof Error ? cause.message : "Unable to load more prompts.");
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

  return (
    <main className="mx-auto w-full max-w-5xl space-y-4 py-5">
      <div>
        <h1 className="text-xl font-semibold">Fleet sessions · {selectedTarget?.display_name || selectedTarget?.name}</h1>
        <p className="text-sm text-muted-foreground">Read-only history from the selected external target.</p>
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
                <Button outlined size="sm" onClick={() => void exportSession(session)} aria-label={`Export ${session.title || session.id}`}>Export</Button>
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
                      {activePrompt !== null && <Button outlined size="sm" onClick={() => void showFullTranscript(session.id)}>Full transcript</Button>}
                    </div>}
                    {messages.map((message, index) => (
                      <div key={index} className="rounded bg-secondary/30 p-3">
                        <p className="mb-1 text-xs font-semibold">{message.role}{message.tool_name ? ` · ${message.tool_name}` : ""}</p>
                        <pre className="whitespace-pre-wrap break-words font-sans text-sm">{message.content || ""}</pre>
                      </div>
                    ))}
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
