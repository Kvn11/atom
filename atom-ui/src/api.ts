export interface InputDef { name: string; required: boolean; description?: string; default?: string; }
export interface Workflow { name: string; description?: string; inputs: InputDef[]; }
export interface ArtifactRef { name: string; path: string; rel: string; size: number; }
export interface TaskState {
  id: string; status: string; model?: string; thinking?: string | number;
  error?: string; artifacts: ArtifactRef[]; started_at?: string; ended_at?: string;
}
export interface StepState { index: number; title: string; status: string; tasks: TaskState[]; }
export interface Manifest {
  run_id: string; workflow: string; status: string; inputs: Record<string, unknown>;
  created_at: string; ended_at?: string; workspace_path: string; steps: StepState[];
}
export interface ChatMsg {
  role: string; text: string; name?: string;
  tool_calls?: { name: string; args?: Record<string, unknown> }[];
}
export interface RunSummary {
  run_id: string; workflow: string; status: string; created_at: string; ended_at?: string;
  steps_total: number; steps_done: number; tasks_total: number; tasks_done: number; current_step?: string;
}
export interface RunsPage { items: RunSummary[]; total: number; counts: { active: number; complete: number; halted: number }; }
export interface Artifact extends ArtifactRef { step: number; task: string; }

const j = async (r: Response) => { if (!r.ok) throw new Error(await r.text()); return r.json(); };

export const artifactUrl = (id: string, rel: string) =>
  `/api/runs/${id}/artifacts/${rel.split("/").map(encodeURIComponent).join("/")}`;

export const api = {
  workflows: (): Promise<Workflow[]> => fetch("/api/workflows").then(j),
  submit: (workflow: string, inputs: Record<string, string>): Promise<{ run_id: string }> =>
    fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow, inputs }),
    }).then(j),
  runs: (status: string, limit: number, offset: number, signal?: AbortSignal): Promise<RunsPage> =>
    fetch(`/api/runs?status=${status}&limit=${limit}&offset=${offset}`, { signal }).then(j),
  run: (id: string): Promise<Manifest> => fetch(`/api/runs/${id}`).then(j),
  messages: (id: string, step: number, task: string): Promise<ChatMsg[]> =>
    fetch(`/api/runs/${id}/tasks/${step}/${task}/messages`).then(j),
  artifacts: (id: string): Promise<Artifact[]> => fetch(`/api/runs/${id}/artifacts`).then(j),
  artifactText: (id: string, rel: string): Promise<string> =>
    fetch(artifactUrl(id, rel)).then(async (r) => { if (!r.ok) throw new Error(await r.text()); return r.text(); }),
};
