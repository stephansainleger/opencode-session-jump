import { spawn } from "node:child_process"

const STATE_OPTION = "@oc_state"
const SESSION_OPTION = "@oc_session_id"
const STATE_AT_OPTION = "@oc_state_at"

const STATE_WORKING = "working"
const STATE_WAITING_PERMISSION = "waiting-permission"
const STATE_WAITING_QUESTION = "waiting-question"
const STATE_DONE = "done"
const STATE_ERROR = "error"

// Events that mean "the session is blocked on the user".  While one is active,
// activity churn (which would map to ``working``) must not overwrite it:
// OpenCode emits step/text events right after ``permission.asked`` or
// ``form.created``, and those would otherwise flip the pane back to working.
const WAITING_STATES = {
  "permission.asked": STATE_WAITING_PERMISSION,
  "form.created": STATE_WAITING_QUESTION,
}

// Events that mean "the user answered / dismissed the prompt": the turn resumes.
// OpenCode 2 reports prompts through the form stream; permission replies keep
// their own event.
const CLEARING_EVENTS = new Set([
  "permission.replied",
  "form.replied",
  "form.cancelled",
])

// Terminal events win over any pending wait and clear it.  OpenCode 2 replaces
// the V1 ``session.error`` with ``session.execution.failed``.
const TERMINAL_STATES = {
  "session.idle": STATE_DONE,
  "session.execution.failed": STATE_ERROR,
}

// OpenCode 2 no longer emits ``message.*`` events.  Session activity is
// reported through execution, step, text, reasoning, tool, shell, compaction,
// inbox and retry events; any of them means the session is working.
const WORKING_EVENTS = new Set([
  "session.created",
  "session.execution.started",
  "session.execution.succeeded",
  "session.step.started",
  "session.step.streamed",
  "session.step.ended",
  "session.step.failed",
  "session.text.started",
  "session.text.delta",
  "session.text.ended",
  "session.reasoning.started",
  "session.reasoning.delta",
  "session.reasoning.ended",
  "session.tool.called",
  "session.tool.input.started",
  "session.tool.input.delta",
  "session.tool.input.ended",
  "session.tool.progress",
  "session.tool.success",
  "session.tool.failed",
  "session.shell.started",
  "session.shell.ended",
  "session.compaction.started",
  "session.compaction.delta",
  "session.compaction.ended",
  "session.compaction.failed",
  "session.inbox.delivered",
  "session.retry.scheduled",
  "permission.replied",
  "form.replied",
  "form.cancelled",
])

const TMUX_PANE = process.env.TMUX_PANE

/**
 * Map a single OpenCode 2 event to the live state it implies.
 *
 * @param {{type?: string}} event - Raw OpenCode event object.
 * @returns {string|null} One of the STATE_* values, or null when the event
 *   carries no state transition. Kept pure so it can be unit-tested with node.
 */
export const stateForEvent = (event) => {
  const type = event?.type
  if (Object.hasOwn(WAITING_STATES, type)) return WAITING_STATES[type]
  if (Object.hasOwn(TERMINAL_STATES, type)) return TERMINAL_STATES[type]
  if (WORKING_EVENTS.has(type)) return STATE_WORKING
  return null
}

/**
 * Compute the next state from the current wait lock and an event.
 *
 * @param {string|null} waiting - The active wait (a STATE_* value) or null.
 * @param {{type?: string}} event - Raw OpenCode event object.
 * @returns {{waiting: string|null, state: string|null}} The new wait lock and
 *   the state to publish (``null`` means "leave the pane unchanged"). Pure, so
 *   the blocking behaviour is testable without tmux.
 */
export const nextState = (waiting, event) => {
  const type = event?.type
  if (Object.hasOwn(WAITING_STATES, type)) {
    return { waiting: WAITING_STATES[type], state: WAITING_STATES[type] }
  }
  if (CLEARING_EVENTS.has(type)) {
    return { waiting: null, state: STATE_WORKING }
  }
  if (Object.hasOwn(TERMINAL_STATES, type)) {
    return { waiting: null, state: TERMINAL_STATES[type] }
  }
  if (waiting) {
    // Blocked on the user: ignore activity churn until it is answered.
    return { waiting, state: null }
  }
  return { waiting: null, state: stateForEvent(event) }
}

/**
 * Write one tmux pane option, ignoring failures outside tmux.
 *
 * @param {string} name - Pane option name, such as ``@oc_state``.
 * @param {string|number|null|undefined} value - Value to publish; empty values
 *   are skipped so a missing session id never clears an existing option.
 */
const setOption = (name, value) => {
  if (!TMUX_PANE || value === undefined || value === null || value === "") return
  try {
    const child = spawn(
      "tmux",
      ["set-option", "-p", "-t", TMUX_PANE, name, String(value)],
      { stdio: "ignore" },
    )
    child.on("error", () => {})
  } catch {
    // tmux unavailable: state publication is best-effort only.
  }
}

/**
 * Extract the session id from an OpenCode 2 event payload.
 *
 * @param {object} event - Raw event whose payload lives under ``data``; session
 *   prompts carry it on the form object instead.
 * @returns {string|null} The session id, or null when the event carries none.
 */
const sessionIdOf = (event) =>
  event?.data?.sessionID || event?.data?.form?.sessionID || null

/**
 * OpenCode plugin that publishes the live session state to tmux pane options.
 *
 * Writes `@oc_state`, `@oc_session_id` and `@oc_state_at` on the pane running
 * the TUI, which is what `ocjump` reads. It subscribes to OpenCode 2's event API
 * directly, so an event-API change must be reflected in the tables above.
 *
 * @param {object} ctx - Plugin context; only ``ctx.event`` is used.
 * @returns {Promise<() => void>} Cleanup that aborts the event subscription.
 */
export const OcStatePlugin = async (ctx) => {
  const controller = new AbortController()
  let currentSessionID = null
  let waiting = null

  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        const sid = sessionIdOf(event)
        if (sid) currentSessionID = sid
        const decision = nextState(waiting, event)
        waiting = decision.waiting
        if (!decision.state) continue
        setOption(STATE_OPTION, decision.state)
        setOption(SESSION_OPTION, sid || currentSessionID)
        setOption(STATE_AT_OPTION, Math.floor(Date.now() / 1000))
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        console.error(`[oc-state] event stream: ${error}`)
      }
    }
  })()

  return () => controller.abort()
}

// OpenCode 2 plugin definition: the loader reads only this default record and
// requires an `id` plus an `effect` or `setup` function.
const plugin = {
  id: "oc-state",
  setup: OcStatePlugin,
}

export default plugin
