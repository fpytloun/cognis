# Orchestration routing by conversation surface

This document defines how Cognis should expose and enforce orchestration
choices for each conversation surface. The goal is to keep prompts
context-specific: invalid options should disappear from the model's
instructions and tool schemas where possible, with runtime validation as the
safety net.

## Routing table

| Conversation surface | Prompt should show | `delegate(wait=true)` | `delegate(wait=false)` | Managed conversation `wait=true` | Managed conversation `wait=false` | `create_task` |
|---|---|---:|---:|---:|---:|---:|
| Main / channel chat | Async-first routing options | Yes, when current answer needs result | Yes, only lightweight non-interactive background work | Yes, if caller must inspect result before continuing | Yes, for visible async interactive work; prefer `agent_conversation_send` when a relevant managed conversation already exists | Yes, preferred for heavier workflow-shaped work |
| Direct / topic chat | Inline + joined specialist work | Yes, common | No / not prompt-visible | Yes, for planning/review/implementation needing interaction | No / not prompt-visible | Yes, if durable/heavy |
| Managed agent conversation | Only valid synchronous/inline options | Yes, common | Forbidden, not prompt-visible | Usually not needed from inside managed conversation | Forbidden / not prompt-visible | Yes, only if durable workflow is explicitly appropriate |
| Workflow task step | Workflow-step-safe options | Yes / forced sync | Forbidden / coerced or rejected | No, unless explicitly available and needed | No | Usually no; already inside task |
| Sub-session / delegated child | Finish assigned work | Avoid further fan-out unless necessary | No / not prompt-visible | No | No | No, unless explicitly requested and tool policy allows |

## Runtime invariants

1. Managed agent conversation + `delegate(wait=false)` is rejected.
2. Workflow task step + `delegate(wait=false)` is forced to synchronous joined
   execution by the step orchestration mode.
3. Prompts and tool schemas should be context-specific. Forbidden options
   should disappear instead of being described as available-but-prohibited.
4. `delegate(wait=false)` is a narrow live/main or channel-chat primitive for
   lightweight non-interactive background work. It is not a general
   orchestration primitive.
5. Managed conversation `wait=false` is available only from main/channel chat
   surfaces. Direct/topic chats default omitted managed-conversation `wait` to
   joined execution and reject explicit `wait=false`.
6. Prompt/context guidance must distinguish web main chat from normal web
   topic chats. Topic chats must not receive guidance to start background
   managed conversations just to keep the main/live chat responsive.
7. Main/channel prompt and tool guidance must prefer reusing an existing
   relevant managed conversation with `agent_conversation_send` before
   creating a new one. `agent_conversation_create` is for new visible managed
   work loops or intentional separation, not same-problem continuation.
8. Moving the same issue from plan/debug to implementation should continue the
   existing managed conversation and set `chat_mode="build"` for that turn.

## Fine-tuning point

The centralized policy lives in `cognis/core/orchestration_policy.py`. Adjust
surface classification and exposed affordances there before adding new ad-hoc
context checks elsewhere.
