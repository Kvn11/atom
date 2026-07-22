import { useState } from "react";
import { ChatMsg, StreamBlock } from "./api";

export type SubStatus = "running" | "done" | "failed" | "incomplete";
export interface SubResult { text: string; isError: boolean; }
export interface Pairing { delegateIds: Set<string>; resultByCallId: Map<string, SubResult>; }

const DELEGATE = "delegate_task";

// Live stream: delegate call-ids from tool_call blocks; results keyed by the id they answer.
export function pairBlocks(blocks: StreamBlock[]): Pairing {
  const delegateIds = new Set<string>();
  const resultByCallId = new Map<string, SubResult>();
  for (const b of blocks) {
    if (b.kind === "tool_call" && b.name === DELEGATE && b.id) delegateIds.add(b.id);
    else if (b.kind === "tool_result" && b.toolCallId)
      resultByCallId.set(b.toolCallId, { text: b.text, isError: b.isError });
  }
  return { delegateIds, resultByCallId };
}

// Persisted transcript: same, from serialized messages (tool_calls[].id + ToolMessage tool_call_id).
export function pairChat(chat: ChatMsg[]): Pairing {
  const delegateIds = new Set<string>();
  const resultByCallId = new Map<string, SubResult>();
  for (const m of chat) {
    for (const c of m.tool_calls ?? []) if (c.name === DELEGATE && c.id) delegateIds.add(c.id);
    if (m.tool_call_id) resultByCallId.set(m.tool_call_id, { text: m.text, isError: !!m.is_error });
  }
  return { delegateIds, resultByCallId };
}

// No result yet -> running while the task streams, else a dangling call in a terminal transcript.
export function subStatus(result: SubResult | undefined, streaming: boolean): SubStatus {
  if (!result) return streaming ? "running" : "incomplete";
  return result.isError ? "failed" : "done";
}

// One-line summary: the failure reason (sentinel stripped) or the report's first line.
export function subSummary(status: SubStatus, report: string | undefined): string {
  if (!report) return "";
  if (status === "failed") {
    const m = report.match(/^\[sub-agent '.*?' (.*)\]\s*$/s);
    return m ? m[1] : firstLine(report, 80);
  }
  if (status === "done") return firstLine(report, 80) || "reported";
  return "";
}

function firstLine(s: string, n: number): string {
  const line = s.split("\n").find((l) => l.trim()) ?? "";
  return line.length > n ? line.slice(0, n - 1) + "…" : line;
}

const STATUS_PILL: Record<SubStatus, string> = { running: "warn", done: "ok", failed: "err", incomplete: "idle" };

// One delegate_task rendered as a status card: description + type badge + running/done/failed pill,
// a one-line summary, and a collapsible (default-closed) full report. Status is derived, not fetched.
export function SubAgentCard(
  { description, subagentType, result, streaming }:
  { description: string; subagentType: string; result: SubResult | undefined; streaming: boolean },
) {
  const [open, setOpen] = useState(false);
  const status = subStatus(result, streaming);
  const report = result?.text;
  const summary = subSummary(status, report);
  return (
    <div className={`subagent-card ${status}`}>
      <div className="sa-head">
        <span className="sa-icon" aria-hidden="true">{"\u{1F916}"}</span>
        <span className="sa-title" title={description}>{description}</span>
        <span className={`pill ${STATUS_PILL[status]} sa-status`}>
          {status === "running" && <span className="sa-live" aria-hidden="true" />}
          {status}
        </span>
      </div>
      <div className="sa-sub">
        <span className="tag">{subagentType}</span>
        {summary && <span className="sa-summary" title={summary}>{summary}</span>}
        {report && (
          <button className="sa-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? "▾ hide report" : "▸ view report"}
          </button>
        )}
      </div>
      {open && report && <div className="sa-report">{report}</div>}
    </div>
  );
}
