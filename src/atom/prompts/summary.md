You are compacting an atom agent's working conversation so it can keep going past its context window. Everything between the <messages> tags below will be REPLACED by the summary you write, so capture exactly what the agent needs to continue — and nothing it can rederive.

The user's original instruction is pinned separately and shown to the agent verbatim on every turn. Do NOT restate or re-summarize it — spend your words on progress since then.

Write a tight, self-contained summary under these headings. Put "None" under any heading with nothing to report.

## PLAN STATE
The current todo list — which items are done, which one is in progress, which remain. If there is no explicit plan, state what has been accomplished and what is left.

## WORKSPACE & FILES
Every virtual path in play — the workspace, uploads, and outputs mounts — and each specific file created, modified, or read, with a few words on what each contains. These are how the agent finds its work; never drop a path.

## DELIVERABLES
Files already presented to the user via present_files, and anything still owed.

## FINDINGS & DECISIONS
Key results, values, and commands that worked, plus choices made and the reason for them, that the agent must not rediscover or reverse. Note rejected approaches and why.

## OPEN QUESTIONS
Anything unresolved or still awaiting a result.

Drop verbose tool output, step-by-step reasoning, and anything already superseded. Be specific and concise.

<messages>
{messages}
</messages>
