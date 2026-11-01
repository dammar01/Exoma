// Exoma host plugin for OpenCode. Managed by Exoma (`exoma install opencode`); do not edit
// by hand, an install replaces it.
//
// A thin shim: every decision is made by opencode-host.py, which imports the same
// workflow_policy module as the Claude Code, Codex and Agy hosts, so a gate cannot behave
// differently here. The shim only carries events across (JSON on stdin, JSON back).
//
//   chat.message                         -> UserPromptSubmit (binds the session, arms gates)
//   experimental.chat.system.transform   -> System (session binding + intent hint)
//   tool.execute.before                  -> PreToolUse (throws to refuse the call)
//
// A process started as Exoma's second agent (WORKFLOW_AGENT_ROLE=second_agent) gets no
// hooks at all: the delegated call must not be gated as if it were the user's session.
import { spawnSync } from "node:child_process"

const PYTHON = {{PYTHON_JSON}}
const HOOK = {{HOOK_JSON}}

function call(event, payload) {
  try {
    const run = spawnSync(PYTHON, [HOOK, event], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: 30000,
      windowsHide: true,
    })
    if (run.status !== 0 || !run.stdout) return {}
    const parsed = JSON.parse(run.stdout)
    return parsed && typeof parsed === "object" ? parsed : {}
  } catch {
    // Fail open, as every Exoma hook does: a broken hook must not stop the session.
    return {}
  }
}

export const ExomaHost = async ({ directory }) => {
  if (process.env.WORKFLOW_AGENT_ROLE === "second_agent") return {}
  let lastSession = null
  return {
    "chat.message": async (input, output) => {
      const parts = (output && output.parts) || []
      const prompt = parts
        .filter((part) => part && part.type === "text" && !part.synthetic)
        .map((part) => part.text || "")
        .join("\n")
      lastSession = input.sessionID || lastSession
      call("UserPromptSubmit", { session_id: input.sessionID, cwd: directory, prompt })
    },
    "experimental.chat.system.transform": async (input, output) => {
      const session = (input && input.sessionID) || lastSession
      if (!session || !output || !Array.isArray(output.system)) return
      const result = call("System", { session_id: session, cwd: directory })
      if (result.context) output.system.push(result.context)
    },
    "tool.execute.before": async (input, output) => {
      const result = call("PreToolUse", {
        session_id: input.sessionID,
        cwd: directory,
        tool_name: input.tool,
        tool_input: (output && output.args) || {},
      })
      if (result.deny) throw new Error(result.deny)
    },
  }
}
