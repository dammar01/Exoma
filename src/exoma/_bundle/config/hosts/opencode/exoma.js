// Exoma host plugin for OpenCode. Managed by Exoma (`exoma install opencode`); do not edit
// by hand, an install replaces it.
//
// Python handles session binding and pre-tool policy. The native after-hook validates
// conditional browser skills before their contents reach the conversation.
//
// A process started as Exoma's second agent (WORKFLOW_AGENT_ROLE=second_agent) gets no
// hooks at all: the delegated call must not be gated as if it were the user's session.
import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { lstatSync, readFileSync, realpathSync } from "node:fs"
import path from "node:path"

const CONTINUATIONS = {{CONTINUATIONS_JSON}}
const PYTHON = {{PYTHON_JSON}}
const HOOK = {{HOOK_JSON}}

function managedContinuation(name, metadata) {
  const expected = CONTINUATIONS[name]
  if (metadata?.name !== name || typeof metadata.dir !== "string" ||
      !path.isAbsolute(metadata.dir) || path.relative(expected.dir, metadata.dir) !== "") return
  const file = path.join(expected.dir, "SKILL.md")
  try {
    if (path.relative(expected.dir, realpathSync(metadata.dir)) !== "") return
    const info = lstatSync(file)
    if (!info.isFile() || info.size > 256 * 1024 || path.relative(file, realpathSync(file)) !== "") return
    const source = readFileSync(file, "utf8").replace(/\r\n/g, "\n")
    if (createHash("sha256").update(source).digest("hex") === expected.sha256) return source
  } catch {
    // Missing or unreadable managed file leaves this branch incomplete.
  }
}

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
    "tool.execute.after": async (input, output) => {
      if (input.tool !== "skill") return
      const name = input.args?.name
      if (!Object.hasOwn(CONTINUATIONS, name)) return
      const source = managedContinuation(name, output.metadata)
      // Retain only the managed response, including on mismatch or native output truncation.
      for (const key of Object.keys(output)) delete output[key]
      output.title = name
      output.metadata = source ? { name, dir: CONTINUATIONS[name].dir } : {}
      output.output = source ?? "Exoma browser continuation unavailable. Report this branch as incomplete and stop."
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
