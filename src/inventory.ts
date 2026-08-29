/** What skills and MCP servers are *installed*, as opposed to used.
 *
 * Usage comes from telemetry. This module supplies the denominator, by reading
 * the same configuration Claude Code reads. Nothing here executes a skill or
 * starts a server; it only reads names, paths and descriptions.
 *
 * Secrets are never stored: MCP `args` and `env` frequently carry tokens, so
 * only the command's program name is kept.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { type Db, insertIgnore } from './db.js'
import { scrub } from './redact.js'
import { strip, utcNow } from './util/text.js'

const SKILL_FILE = 'SKILL.md'

function readTextSafe(path: string): string | null {
  try {
    return readFileSync(path, 'utf8')
  } catch {
    return null
  }
}

function listDirs(base: string): string[] {
  try {
    return readdirSync(base, { withFileTypes: true })
      .filter((e) => e.isDirectory() || e.isSymbolicLink())
      .map((e) => e.name)
      .sort()
      .map((name) => join(base, name))
  } catch {
    return []
  }
}

/** Minimal YAML frontmatter reader - name and description only. */
function frontmatter(path: string): { name?: string; description?: string } {
  const out: { name?: string; description?: string } = {}
  const text = readTextSafe(path)
  if (text === null || !text.startsWith('---')) return out
  const end = text.indexOf('\n---', 3)
  if (end === -1) return out
  for (const line of text.slice(3, end).split('\n')) {
    if (
      !line.includes(':') ||
      line.startsWith(' ') ||
      line.startsWith('\t') ||
      line.startsWith('#')
    ) {
      continue
    }
    const idx = line.indexOf(':')
    const key = line.slice(0, idx).trim().toLowerCase()
    const value = strip(line.slice(idx + 1).trim(), '"\'')
    if (key === 'name' || key === 'description') out[key] = value
  }
  return out
}

export interface SkillRow {
  skill_id: string
  name: string
  scope: string
  project_id: string | null
  source_path: string
  description: string
  discovered_at: string
}

/** Find skills in user, project and plugin scopes. */
export function scanSkills(
  projectDirs: Record<string, string> = {},
): SkillRow[] {
  const found: SkillRow[] = []
  const seen = new Set<string>()

  const add = (
    name: string,
    scope: string,
    path: string,
    desc: string | undefined,
    projectId: string | null = null,
  ) => {
    const key = `${scope}:${name}`
    if (seen.has(key)) return
    seen.add(key)
    found.push({
      skill_id: key,
      name,
      scope,
      project_id: projectId,
      source_path: path,
      description: (desc || '').slice(0, 400),
      discovered_at: utcNow(),
    })
  }

  const home = homedir()
  for (const dir of listDirs(join(home, '.claude', 'skills'))) {
    const skillMd = join(dir, SKILL_FILE)
    if (existsSync(skillMd)) {
      const fm = frontmatter(skillMd)
      add(fm.name || dir.split('/').pop()!, 'user', dir, fm.description)
    }
  }

  const plugins = join(home, '.claude', 'plugins')
  if (existsSync(plugins)) {
    for (const skillDir of findPluginSkillDirs(plugins)) {
      const fm = frontmatter(join(skillDir, SKILL_FILE))
      add(
        fm.name || skillDir.split('/').pop()!,
        'plugin',
        skillDir,
        fm.description,
      )
    }
  }

  for (const [projectId, root] of Object.entries(projectDirs)) {
    const base = join(root, '.claude', 'skills')
    if (!existsSync(base)) continue
    for (const dir of listDirs(base)) {
      const skillMd = join(dir, SKILL_FILE)
      if (existsSync(skillMd)) {
        const fm = frontmatter(skillMd)
        add(
          fm.name || dir.split('/').pop()!,
          'project',
          dir,
          fm.description,
          projectId,
        )
      }
    }
  }
  return found
}

/** Every `.../skills/<name>/SKILL.md` under a root, at any depth. */
function findPluginSkillDirs(root: string, depth = 0): string[] {
  if (depth > 8) return []
  const out: string[] = []
  let entries: string[]
  try {
    entries = readdirSync(root, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .sort()
  } catch {
    return out
  }
  for (const name of entries) {
    const dir = join(root, name)
    if (name === 'skills') {
      for (const skillDir of listDirs(dir)) {
        if (existsSync(join(skillDir, SKILL_FILE))) out.push(skillDir)
      }
    }
    out.push(...findPluginSkillDirs(dir, depth + 1))
  }
  return out
}

function mcpFromFile(path: string): Record<string, unknown> {
  const text = readTextSafe(path)
  if (text === null) return {}
  try {
    const data = JSON.parse(text || '{}')
    if (
      data &&
      typeof data === 'object' &&
      data.mcpServers &&
      typeof data.mcpServers === 'object'
    ) {
      return data.mcpServers as Record<string, unknown>
    }
  } catch {
    /* not JSON, or not the shape we expect */
  }
  return {}
}

export interface McpRow {
  mcp_id: string
  name: string
  scope: string
  project_id: string | null
  config_path: string
  transport: string
  command: string | null
  discovered_at: string
}

export function scanMcp(projectDirs: Record<string, string> = {}): McpRow[] {
  const found: McpRow[] = []
  const seen = new Set<string>()

  const add = (
    name: string,
    scope: string,
    cfg: unknown,
    path: string,
    projectId: string | null = null,
  ) => {
    const key = `${scope}:${name}`
    if (seen.has(key)) return
    seen.add(key)
    const conf = (cfg && typeof cfg === 'object' ? cfg : {}) as Record<
      string,
      unknown
    >
    const command = typeof conf.command === 'string' ? conf.command : null
    let transport = (conf.type || conf.transport) as string | undefined
    if (!transport) transport = conf.url ? 'http' : 'stdio'
    found.push({
      mcp_id: key,
      name,
      scope,
      project_id: projectId,
      config_path: path,
      transport,
      // Only the program name: args and env routinely carry tokens.
      command: command ? scrub(String(command).split('/').pop()!) : null,
      discovered_at: utcNow(),
    })
  }

  const home = homedir()
  for (const path of [
    join(home, '.claude.json'),
    join(home, '.claude', 'settings.json'),
  ]) {
    for (const [name, cfg] of Object.entries(mcpFromFile(path)))
      add(name, 'user', cfg, path)
  }

  for (const [projectId, root] of Object.entries(projectDirs)) {
    for (const fname of ['.mcp.json', '.claude/settings.json']) {
      const path = join(root, fname)
      for (const [name, cfg] of Object.entries(mcpFromFile(path))) {
        add(name, 'project', cfg, path, projectId)
      }
    }
  }
  return found
}

/** Rescan and store. Inventory is current state, so it is replaced. */
export function refresh(db: Db): {
  skills: number
  mcp_servers: number
  projects_scanned: number
} {
  const projectDirs: Record<string, string> = {}
  for (const row of db.all(
    'SELECT project_id, repo_root FROM projects WHERE repo_root IS NOT NULL',
  )) {
    projectDirs[String(row.project_id)] = String(row.repo_root)
  }
  const skills = scanSkills(projectDirs)
  const servers = scanMcp(projectDirs)
  db.run('DELETE FROM skill_inventory')
  db.run('DELETE FROM mcp_inventory')
  for (const row of skills) insertIgnore(db, 'skill_inventory', { ...row })
  for (const row of servers) insertIgnore(db, 'mcp_inventory', { ...row })
  db.commit()
  return {
    skills: skills.length,
    mcp_servers: servers.length,
    projects_scanned: Object.keys(projectDirs).length,
  }
}
