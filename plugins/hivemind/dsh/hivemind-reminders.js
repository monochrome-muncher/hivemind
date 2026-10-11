// DeepSeek Harness plugin: the write-and-rate reminder the Claude Code
// plugin's Stop hook gives. Once per session, when the main agent is about to
// end a turn after 20+ tool calls (HIVEMIND_STOP_MIN_TOOLS) without calling
// hive_write or hive_feedback, it is steered once to write what another agent
// could reuse; if nothing is new it just finishes.
//
// It stays silent whenever the agent cannot see the Hivemind tools: the
// bundle's server row is off in incognito sessions and while the URL is
// unset, and then no mcp__hivemind__ tool exists. Subagents are never
// reminded and their calls do not count, as in Claude Code.
//
// Deliberately imports nothing but Node built-ins (see hivemind-skills.js), so
// the steering message is written out here instead of using dsh-llm's
// createUserMessage, which builds the same frozen object.
import { randomUUID } from 'node:crypto'

const STOP_REMINDER =
  'HIVEMIND: before finishing, write to Hivemind (hive_write) what another agent could reuse from this task, and rate the entries your work tested (hive_feedback). If nothing is new, write nothing and just finish.'
const WRITE_TOOL = 'mcp__hivemind__hive_write'
const CONTRIBUTION = /(^|[^a-z])hive_(write|feedback)$/

function stopThreshold() {
  const value = process.env.HIVEMIND_STOP_MIN_TOOLS ?? ''
  return /^[0-9]+$/.test(value) ? Number(value) : 20
}

function message(text) {
  return Object.freeze({
    role: 'user',
    content: Object.freeze([Object.freeze({ type: 'text', text })]),
    source: Object.freeze({ kind: 'hivemind' }),
    id: randomUUID(),
  })
}

export const name = 'hivemind-reminders'
export const inject = ['tools']

export function apply(ctx) {
  const subagents = new Set()
  const sessions = new Map() // agent id -> { calls, contributed, reminded }
  const state = (agent) => {
    let s = sessions.get(agent.id)
    if (!s) sessions.set(agent.id, (s = { calls: 0, contributed: false, reminded: false }))
    return s
  }
  const isMain = (agent) => agent?.id !== undefined && !subagents.has(agent.id)

  ctx.on('subagent/start', (info) => {
    subagents.add(info.id)
  })

  ctx.on('tools/post-execute', async (exec, result, next) => {
    if (isMain(exec.agent)) {
      const s = state(exec.agent)
      s.calls += 1
      if (CONTRIBUTION.test(exec.name)) s.contributed = true
    }
    return next()
  })

  ctx.on('agent/turn-stopping', async ({ agent }) => {
    if (!isMain(agent)) return
    const s = state(agent)
    if (s.reminded || s.contributed || s.calls < stopThreshold()) return
    if (!ctx.tools.get(WRITE_TOOL, agent)) return
    s.reminded = true
    agent.steer(message(STOP_REMINDER))
  })
}
