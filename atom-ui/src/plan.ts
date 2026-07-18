import type { ChatMsg, StreamBlock, Todo } from "./api";

// write_todos args are untyped at the wire; narrow args.todos to well-formed items or null.
export function todosFromArgs(args: unknown): Todo[] | null {
  const t = (args as { todos?: unknown } | undefined)?.todos;
  if (!Array.isArray(t)) return null;
  const items = t.filter(
    (x): x is Todo =>
      !!x &&
      typeof (x as Todo).content === "string" &&
      typeof (x as Todo).status === "string",
  );
  return items.length ? items : null;
}

// The current plan = the latest write_todos call. Reads from live blocks while the transcript is
// rendering the live stream, else from the persisted chat's tool_calls. Matches the Transcript's
// own branch selection so panel and transcript read the same source.
export function currentPlan(blocks: StreamBlock[], chat: ChatMsg[], streaming: boolean): Todo[] {
  if (streaming || (blocks.length && !chat.length)) {
    for (let i = blocks.length - 1; i >= 0; i--) {
      const b = blocks[i];
      if (b.kind === "tool_call" && b.name === "write_todos") {
        const todos = todosFromArgs(b.args);
        if (todos) return todos;
      }
    }
    return [];
  }
  for (let i = chat.length - 1; i >= 0; i--) {
    const call = chat[i].tool_calls?.find((c) => c.name === "write_todos");
    if (call) {
      const todos = todosFromArgs(call.args);
      if (todos) return todos;
    }
  }
  return [];
}
