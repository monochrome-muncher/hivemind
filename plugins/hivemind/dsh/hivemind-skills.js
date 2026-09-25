// DeepSeek Harness plugin: serves this bundle's skills/ folder (hivemind,
// hivemind-setup) as a packaged skill provider, the pattern DSH's own
// dsh-skill-badge uses.
//
// Deliberately imports nothing but Node built-ins. A bundle installed with
// `dsh plugin add <checkout>` is linked, and a linked checkout cannot
// resolve DSH's own packages, so the provider object is written out here
// instead of importing @deepseek-ai/dsh-skill's types or constants.
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const PROVIDER_NAME = 'hivemind'
const SKILLS_DIR = new URL('../skills/', import.meta.url)
const BUNDLED_SKILL_RANK = 600 // dsh-skill's rank for packaged skills
const INVOCATION = { modelInvocable: true, userInvocable: true }

/** Split a SKILL.md into its frontmatter fields and its body. */
function parse(text) {
  const match = /^---\n([\s\S]*?)\n---\n?([\s\S]*)$/.exec(text)
  if (!match) return null
  const fields = {}
  for (const line of match[1].split('\n')) {
    const at = line.indexOf(':')
    if (at > 0) fields[line.slice(0, at).trim()] = line.slice(at + 1).trim()
  }
  return fields.name && fields.description ? { ...fields, body: match[2] } : null
}

async function candidates() {
  const out = []
  for (const entry of await readdir(SKILLS_DIR, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const dir = new URL(`${entry.name}/`, SKILLS_DIR)
    const locator = new URL('SKILL.md', dir)
    const skill = parse(await readFile(locator, 'utf8'))
    if (!skill) continue
    out.push({
      name: skill.name,
      description: skill.description,
      invocation: INVOCATION,
      provider: PROVIDER_NAME,
      source: 'bundled',
      resourceBase: { kind: 'directory', path: fileURLToPath(dir) },
      rank: BUNDLED_SKILL_RANK,
      locator,
    })
  }
  return out
}

const provider = {
  name: PROVIDER_NAME,
  list: () => candidates(),
  async get(candidate) {
    const skill = parse(await readFile(candidate.locator, 'utf8'))
    return {
      name: candidate.name,
      description: candidate.description,
      invocation: candidate.invocation,
      provider: candidate.provider,
      source: candidate.source,
      resourceBase: candidate.resourceBase,
      content: skill ? skill.body : '',
    }
  },
}

export const name = 'hivemind-skills'
export const inject = ['skills']

export function apply(ctx) {
  ctx.skills.registerProvider(() => provider)
}
