{# Optional wrapper for the incoming task. Default profiles leave user_prompt unset, so the raw
   task is used verbatim. Set a profile's `user_prompt: "@prompts/user_task.md"` to wrap it. #}
{{ task }}
