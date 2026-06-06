# Shell Tool

## Objective

Add a shell tool that executes commands inside the Docker sandbox, plus a `!` prefix
for user-initiated shell commands directly from the input box.

## Context

- Sandboxing (Phase 3) complete — container lifecycle, exec, teardown all working
- Engine tool dispatch and result truncation in place
- Shell is the "god tool" — makes the agent capable of most coding tasks
- No approval prompts needed — sandbox containment is the safety mechanism
- Container per session, destroyed on quit

## Requirements

### Shell Tool

- MUST execute commands inside the Docker sandbox via `sandbox.exec()`
  - AC: Model calls `shell(command="uv run pytest")` and gets output back
- MUST include exit code in the response
  - AC: Model can distinguish success (0) from failure
- MUST run without a timeout (commands run until completion)
  - AC: Long builds, deploys, terraform applies all work without being killed
- MUST support interrupt via Esc key (sends SIGINT to the running command)
  - AC: User presses Esc → command receives SIGINT → partial output returned
  - AC: Same Esc binding that cancels LLM generation
- MUST start sandbox on first use (lazy via `ensure_running()`)
  - AC: No container overhead if shell is never called
- MUST display in conversation via existing ToolCallMessage widget

### User Shell (! prefix)

- MUST intercept messages starting with `!` and run in sandbox instead of sending to LLM
  - AC: `!ls -la` runs in container, output displayed, not sent to model
- MUST work even while model generation is in progress
  - AC: User can run ! commands at any time
- MUST display output in a ShellOutput widget (distinct from tool calls)
- MUST cap displayed output at 2000 chars
- MUST handle errors gracefully (container start failure → ErrorMessage)
- MUST ignore empty ! commands (bare `!` or `! `)
- MUST NOT record ! commands in the session or send to the LLM

## Design

### Key Decisions (from review)

- **No timeout**: Commands run until completion. User presses Esc to interrupt.
  This matches real terminal behaviour — you don't pre-set timeouts on builds.
- **Interrupt mechanism**: Worker thread runs `sandbox.exec()` which uses `subprocess.Popen`
  (not `subprocess.run`) so we can kill the process when the Worker is cancelled.
  On cancellation: kill the docker exec process → returns partial captured output.
- **Stale sandbox on new session**: Engine recreated on new session with new sandbox.
  Engine creates its own registry with the sandbox reference. Clean lifecycle.
- **! during generation**: Allowed. Independent of model work.
- **Truncation**: Shell tool output truncated by Engine's `truncate_result()` (4000 chars).
  ShellOutput widget caps display at 2000 chars.
- **Destructive ! commands**: Fine — that's what sandboxing is for. Project dir is rw
  by design (same as running commands in a terminal).
- **Empty !**: Ignored silently.

### Shell Tool (`src/archie/tools/shell.py`)

Handler:
1. `sandbox.ensure_running()`
2. `sandbox.exec(command)` — no timeout, blocks until done or interrupted
3. Format: `"$ {command}\n[exit: {code}]\n{output}"`

Schema: `command` (required string). No timeout parameter.

Description: "Execute a shell command in the sandboxed container. Use for: running
tests, installing packages, git operations, build commands, or any system command."

### Sandbox.exec() Change

Currently uses `subprocess.run`. Needs to switch to `subprocess.Popen` so the process
can be killed on interrupt. The Sandbox gets a `cancel()` method that kills the active
process. The Engine Worker's cancellation check triggers `sandbox.cancel()`.

### Registry Change

`create_default_registry(cwd, allowed_directories, sandbox)` — add sandbox param.
Shell tool captures sandbox via closure (same pattern as file tools capture cwd).

### ! Prefix Flow

```
on_message_input_submitted:
  if content.startswith("!"):
    command = content[1:].strip()
    if not command: return  # ignore empty
    run in Worker: sandbox.ensure_running() + sandbox.exec()
    display ShellOutput widget with result
    return  # don't send to engine
```

### New Widget: ShellOutput

Styled for user-initiated commands (not model tool calls):
- Muted header with `$` prefix
- Monospace output
- Exit code shown
- Capped at 2000 chars display

## Milestones

1. Shell tool
   Approach:
   - Create `src/archie/tools/shell.py` with spec and handler
   - Handler calls `sandbox.ensure_running()` then `sandbox.exec(command)`
   - Format output: command echo + exit code + output
   - Schema: `command` required (string). No timeout param.
   - Refactor `sandbox.exec()` to use `subprocess.Popen` instead of `subprocess.run`
     so the process can be killed on interrupt. Add `sandbox.cancel()` method.
   - Update `create_default_registry()` to accept sandbox, register shell tool
   - Update Engine to call `sandbox.cancel()` when Worker is cancelled (Esc pressed)
   - ⚠️ Ensure new_session recreates Engine with new sandbox+registry
   Tasks:
   - Refactor `sandbox.exec()` to Popen-based (killable)
   - Add `sandbox.cancel()` — kills active exec process if any
   - Create `src/archie/tools/shell.py`
   - Update `tools/__init__.py` — add sandbox param
   - Update `app.py` — pass sandbox when creating registry, fix new_session
   - Update Engine — on Worker cancellation, call sandbox.cancel()
   - Tests: mock subprocess, verify output formatting, verify cancel kills process
   Deliverable: Model can execute shell commands in the sandbox; Esc interrupts them.
   Verify: Tests pass. Manual: ask model to run `sleep 10` — press Esc — command stops.

2. User shell (! prefix)
   Approach:
   - In `app.py` `on_message_input_submitted`: detect `!` prefix before the engine guard
   - Strip prefix, ignore if empty
   - Run sandbox.ensure_running() + sandbox.exec() in a Worker thread
   - On success: display ShellOutput widget with command, exit code, output (capped 2000 chars)
   - On error (container failure): display ErrorMessage
   - Create ShellOutput widget in conversation.py — distinct styling from ToolCallMessage
   - ⚠️ Must work even when _stream_worker is active (user shell is independent)
   Tasks:
   - Add ! detection at top of on_message_input_submitted (before engine guard)
   - Create ShellOutput widget in conversation.py
   - Add add_shell_output() method to Conversation
   - Create Textual Message class for shell result (posted from Worker)
   - Handle errors from sandbox
   - Tests: verify ! intercepted, not sent to engine
   Deliverable: User can run shell commands with ! prefix at any time.
   Verify: Type `!echo hello` — output appears in conversation. Type during active generation — still works.
