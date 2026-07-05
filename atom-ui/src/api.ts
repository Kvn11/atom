export interface InputDef { name: string; required: boolean; description?: string; default?: string; }
export interface Workflow { name: string; description?: string; inputs: InputDef[]; }
export interface TaskState { id: string; status: string; model?: string; error?: string; }
export interface StepState { index: number; title: string; status: string; tasks: TaskState[]; }
export interface Manifest {
  run_id: string; workflow: string; status: string;
  workspace_path: string; steps: StepState[];
}
export interface ChatMsg { role: string; text: string; tool_calls?: { name: string }[]; name?: string; }
export interface Artifact { path: string; size: number; modified: number; }

const j = async (r: Response) => { if (!r.ok) throw new Error(await r.text()); return r.json(); };

export const api = {
  workflows: (): Promise<Workflow[]> => fetch("/api/workflows").then(j),
  submit: (workflow: string, inputs: Record<string, string>): Promise<{ run_id: string }> =>
    fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow, inputs }),
    }).then(j),
  run: (id: string): Promise<Manifest> => fetch(`/api/runs/${id}`).then(j),
  messages: (id: string, step: number, task: string): Promise<ChatMsg[]> =>
    fetch(`/api/runs/${id}/tasks/${step}/${task}/messages`).then(j),
  artifacts: (id: string): Promise<Artifact[]> => fetch(`/api/runs/${id}/artifacts`).then(j),
  artifact: (id: string, path: string): Promise<string> =>
    fetch(`/api/runs/${id}/artifacts/${path.split("/").map(encodeURIComponent).join("/")}`)
      .then(async (r) => { if (!r.ok) throw new Error(await r.text()); return r.text(); }),
};
