You are compacting an atom agent's conversation so it can keep working past its context window. Extract only the highest-value context; everything below will be REPLACED by what you write.

Preserve, verbatim where they appear, all of the following:
- The user's overall goal and any explicit constraints or decisions.
- The current plan / todo list state (which items are done, in progress, or pending).
- Every virtual path in play — the workspace, uploads, and outputs mounts, and any specific file paths already created or modified (these are how the agent finds its work).
- Deliverables already produced (files presented to the user) and open questions still unanswered.
- Key results, values, or findings the agent will need to avoid redoing work.

Drop verbose tool output, redundant reasoning, and anything already superseded. Write a concise, self-contained summary the agent can resume from without losing track of its files or its plan.

<messages>
{messages}
</messages>
