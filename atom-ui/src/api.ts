export interface InputDef { name: string; type?: "text" | "file"; required: boolean; description?: string; default?: string; }
export interface Workflow { name: string; description?: string; notes_enabled?: boolean; inputs: InputDef[]; }
export interface ArtifactRef { name: string; path: string; rel: string; size: number; }
export interface TaskState {
  id: string; status: string; model?: string; thinking?: string | number;
  error?: string; artifacts: ArtifactRef[]; started_at?: string; ended_at?: string;
}
export interface StepState { index: number; title: string; status: string; tasks: TaskState[]; }
export interface Manifest {
  run_id: string; workflow: string; status: string; inputs: Record<string, unknown>;
  created_at: string; ended_at?: string; workspace_path: string; steps: StepState[];
  cancel_requested?: boolean;
}
export interface ChatMsg {
  role: string; text: string; name?: string;
  tool_call_id?: string; is_error?: boolean;
  tool_calls?: { name: string; args?: Record<string, unknown>; id?: string }[];
}
export type StreamBlock =
  | { kind: "thinking"; text: string }
  | { kind: "text"; text: string }
  | { kind: "tool_call"; id?: string; name?: string; args?: Record<string, unknown> }
  | { kind: "tool_result"; toolCallId?: string; name?: string; text: string; isError: boolean };
export type TodoStatus = "pending" | "in_progress" | "completed";
export interface Todo { content: string; status: TodoStatus; }
export interface RunSummary {
  run_id: string; workflow: string; status: string; created_at: string; ended_at?: string;
  steps_total: number; steps_done: number; tasks_total: number; tasks_done: number; current_step?: string;
}
export interface RunsPage { items: RunSummary[]; total: number; counts: { active: number; complete: number; halted: number; cancelled: number }; }
export interface Artifact extends ArtifactRef { step: number; task: string; }
export interface ExportResponse {
  run_id: string; scope: "run" | "task"; task_id: string | null;
  path: string; complete: boolean; expected_roots: number; fetched_roots: number;
}

const j = async (r: Response) => { if (!r.ok) throw new Error(await r.text()); return r.json(); };

export const artifactUrl = (id: string, rel: string) =>
  `/api/runs/${id}/artifacts/${rel.split("/").map(encodeURIComponent).join("/")}`;

export const exportDownloadUrl = (id: string, body?: { step: number; task: string }) =>
  body
    ? `/api/runs/${id}/export/download?step=${body.step}&task=${encodeURIComponent(body.task)}`
    : `/api/runs/${id}/export/download`;

export const api = {
  workflows: (): Promise<Workflow[]> => fetch("/api/workflows").then(j),
  submit: (
    workflow: string,
    inputs: Record<string, string>,
    files?: Record<string, File>,
  ): Promise<{ run_id: string }> => {
    const fileEntries = files ? Object.entries(files) : [];
    if (fileEntries.length === 0) {
      return fetch("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow, inputs }),
      }).then(j);
    }
    const fd = new FormData();
    fd.append("workflow", workflow);
    fd.append("inputs", JSON.stringify(inputs));
    for (const [name, file] of fileEntries) fd.append(name, file);
    return fetch("/api/runs", { method: "POST", body: fd }).then(j);  // browser sets multipart boundary
  },
  runs: (status: string, limit: number, offset: number, signal?: AbortSignal): Promise<RunsPage> =>
    fetch(`/api/runs?status=${status}&limit=${limit}&offset=${offset}`, { signal }).then(j),
  run: (id: string): Promise<Manifest> => fetch(`/api/runs/${id}`).then(j),
  messages: (id: string, step: number, task: string): Promise<ChatMsg[]> =>
    fetch(`/api/runs/${id}/tasks/${step}/${task}/messages`).then(j),
  streamUrl: (id: string, step: number, task: string): string =>
    `/api/runs/${id}/tasks/${step}/${encodeURIComponent(task)}/stream`,
  artifacts: (id: string): Promise<Artifact[]> => fetch(`/api/runs/${id}/artifacts`).then(j),
  artifactText: (id: string, rel: string): Promise<string> =>
    fetch(artifactUrl(id, rel)).then(async (r) => { if (!r.ok) throw new Error(await r.text()); return r.text(); }),
  // Whole run when body is omitted; one task when { step, task } is given. Surfaces the API's
  // {detail} message on error (e.g. "task not completed", "run not found").
  exportRun: (id: string, body?: { step: number; task: string }): Promise<ExportResponse> =>
    fetch(`/api/runs/${id}/export`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `export failed (${r.status})`);
      return data as ExportResponse;
    }),
  selfImprove: (id: string): Promise<{ run_id: string; status: string }> =>
    fetch(`/api/runs/${id}/self-improve`, { method: "POST" }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `self-improve failed (${r.status})`);
      return data as { run_id: string; status: string };
    }),
  cancel: (id: string): Promise<{ run_id: string; status: string; cancel_requested?: boolean }> =>
    fetch(`/api/runs/${id}/cancel`, { method: "POST" }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `cancel failed (${r.status})`);
      return data as { run_id: string; status: string; cancel_requested?: boolean };
    }),
  clearNotes: (name: string): Promise<{ workflow: string; cleared: boolean }> =>
    fetch(`/api/workflows/${encodeURIComponent(name)}/notes`, { method: "DELETE" }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `clear notes failed (${r.status})`);
      return data as { workflow: string; cleared: boolean };
    }),
};
