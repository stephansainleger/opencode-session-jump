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
// message/session churn (which would map to ``working``) must not overwrite it:
// OpenCode emits a ``message.part.updated`` a few milliseconds after
// ``permission.asked``, and that would otherwise flip the pane back to working.
const WAITING_STATES = {
  "permission.asked": STATE_WAITING_PERMISSION,
  "question.asked": STATE_WAITING_QUESTION,
}

// Events that mean "the user answered / dismissed the prompt": the turn resumes.
const CLEARING_EVENTS = new Set([
  "permission.replied",
  "question.replied",
  "question.rejected",
])

// Terminal events win over any pending wait and clear it.
const TERMINAL_EVENTS = new Set(["session.idle", "session.error"])

const TMUX_PANE = process.env.TMUX_PANE

/**
 * Map a single OpenCode event to the live state it implies.
 *
 * @param {{type?: string}} event - Raw OpenCode event object.
 * @returns {string|null} One of the STATE_* values, or null when the event
 *   carries no state transition. Kept pure so it can be unit-tested with node.
 */
export const stateForEvent = (event) => {
  switch (event?.type) {
    case "session.created":
    case "message.updated":
    case "message.part.updated":
    case "permission.replied":
    case "question.replied":
    case "question.rejected":
      return STATE_WORKING
    case "permission.asked":
      return STATE_WAITING_PERMISSION
    case "question.asked":
      return STATE_WAITING_QUESTION
    case "session.idle":
      return STATE_DONE
    case "session.error":
      return STATE_ERROR
    default:
      return null
  }
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
  if (type in WAITING_STATES) {
    return { waiting: WAITING_STATES[type], state: WAITING_STATES[type] }
  }
  if (CLEARING_EVENTS.has(type)) {
    return { waiting: null, state: STATE_WORKING }
  }
  if (TERMINAL_EVENTS.has(type)) {
    return { waiting: null, state: stateForEvent(event) }
  }
  if (waiting) {
    // Blocked on the user: ignore message/session churn until it is answered.
    return { waiting, state: null }
  }
  return { waiting: null, state: stateForEvent(event) }
}

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

const sessionIdOf = (event) =>
  event?.sessionID || event?.properties?.sessionID || event?.properties?.info?.id || null

/**
 * OpenCode plugin that publishes the live session state to tmux pane options.
 *
 * Writes `@oc_state`, `@oc_session_id` and `@oc_state_at` on the pane running
 * the TUI, which is what `ocjump` reads. It subscribes to OpenCode's event API
 * directly, so an event-API change must be reflected in `stateForEvent`.
 *
 * @returns {Promise<{event: (input: {event: object}) => Promise<void>}>} Plugin hooks.
 */
export const OcStatePlugin = async () => {
  let currentSessionID = null
  let waiting = null
  return {
    event: async ({ event }) => {
      const sid = sessionIdOf(event)
      if (sid) currentSessionID = sid
      const decision = nextState(waiting, event)
      waiting = decision.waiting
      if (!decision.state) return
      setOption(STATE_OPTION, decision.state)
      setOption(SESSION_OPTION, sid || currentSessionID)
      setOption(STATE_AT_OPTION, Math.floor(Date.now() / 1000))
    },
  }
}

// v1 server plugin module: OpenCode reads only this default record and ignores
// the named exports above. Exporting the raw factory as `default` instead would
// route the module through OpenCode's legacy loader, which calls every exported
// function (including `stateForEvent`) as a plugin factory and pushes its
// non-Hooks return value into the plugin list, crashing `Provider.list`.
const plugin = {
  id: "oc-state",
  server: OcStatePlugin,
}

export default plugin
