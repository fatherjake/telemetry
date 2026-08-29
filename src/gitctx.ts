/** Project identification and read-only git reconciliation.
 *
 * All git invocations here are read-only (`rev-parse`, `remote get-url`,
 * `branch --show-current`, `log`, `show`). Nothing in this module writes to a
 * repository or reads file contents.
 */
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { statSync } from 'node:fs'
import { basename, dirname } from 'node:path'
import { scrub } from './redact.js'
import { purePath, rstrip, strip, toUtcIso } from './util/text.js'

export { toUtcIso }

const GIT_TIMEOUT_MS = 15_000

function git(args: string[], cwd: string): string | null {
  const result = spawnSync('git', ['-C', cwd, ...args], {
    encoding: 'utf8',
    timeout: GIT_TIMEOUT_MS,
    maxBuffer: 64 * 1024 * 1024,
    env: { ...process.env, GIT_OPTIONAL_LOCKS: '0', GIT_TERMINAL_PROMPT: '0' },
  })
  if (result.error || result.status !== 0) return null
  return (result.stdout ?? '').trim()
}

const nearestDirCache = new Map<string, string | null>()
const repoOfDirCache = new Map<string, RepoInfo>()

function isDir(p: string): boolean {
  try {
    return statSync(p).isDirectory()
  } catch {
    return false
  }
}

/** Walk up to the nearest existing *directory*.
 *
 * Handles file paths, paths that have since been deleted, and paths whose
 * parent is itself a file - `git -C` needs a real directory or it fails
 * outright.
 */
export function nearestDir(path: string): string | null {
  const cached = nearestDirCache.get(path)
  if (cached !== undefined) return cached

  // `Path(path)` normalises before anything else looks at it, and the answer
  // becomes a cache key, so `/a/b` and `/a/b/` must not become two entries.
  let p = purePath(path)
  let answer: string | null = null
  while (true) {
    if (isDir(p)) {
      answer = p
      break
    }
    const parent = dirname(p)
    if (parent === p) break
    p = parent
  }
  if (nearestDirCache.size > 4096) nearestDirCache.clear()
  nearestDirCache.set(path, answer)
  return answer
}

export interface RepoInfo {
  cwd: string
  is_git: boolean
  repo_root?: string
  remote_url?: string | null
}

function repoOfDir(directory: string): RepoInfo {
  const cached = repoOfDirCache.get(directory)
  if (cached) return cached

  const root = git(['rev-parse', '--show-toplevel'], directory)
  const info: RepoInfo = root
    ? {
        cwd: directory,
        is_git: true,
        repo_root: root,
        // Credentials out, host/org/repo kept.
        remote_url: scrub(
          git(['remote', 'get-url', 'origin'], directory)?.trim() ?? null,
        ),
      }
    : { cwd: directory, is_git: false }
  if (repoOfDirCache.size > 1024) repoOfDirCache.clear()
  repoOfDirCache.set(directory, info)
  return info
}

/** Which repository a path belongs to, if any.
 *
 * Cached per *directory*, not per path: attribution runs over thousands of
 * file paths that share a handful of directories, and the answer is the same
 * for all of them.
 *
 * Deliberately excludes branch, HEAD and dirtiness. Those need `git status`,
 * which walks the entire working tree - on a repo holding the telemetry's own
 * data that is seconds per call, and nothing about attributing a file to a
 * project depends on them. Use `describeFull` when you actually want the live
 * state of a checkout.
 */
export function repoInfo(path: string): RepoInfo {
  const directory = nearestDir(path)
  if (directory === null) return { cwd: path, is_git: false }
  return { ...repoOfDir(directory) }
}

/** Reduce a remote URL to `host/org/repo` so ssh and https forms match. */
export function normalizeRemote(url: string | null | undefined): string | null {
  if (!url) return null
  let u = url.trim()
  u = u.replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '')
  u = u.replace(/^[^/@]+@/, '') // strip user@ / credentials
  if (!u.includes('@') && !u.startsWith('/')) u = u.replace(':', '/')
  u = u.replace(/\.git\/?$/, '')
  u = strip(u, '/')
  return u.toLowerCase() || null
}

/** Stable id for a project.
 *
 * Preference order: normalized remote (stable across clones) > repo root path
 * > working directory. Prefixed so the basis is visible in the id.
 */
export function projectId(
  repoRoot: string | null | undefined,
  remoteUrl: string | null | undefined,
  cwd?: string | null,
): string {
  const norm = normalizeRemote(remoteUrl)
  let basis: string
  let seed: string
  if (norm) {
    basis = 'remote'
    seed = norm
  } else if (repoRoot) {
    basis = 'root'
    seed = String(repoRoot)
  } else if (cwd) {
    basis = 'dir'
    seed = String(cwd)
  } else {
    basis = 'unknown'
    seed = 'unknown'
  }
  const digest = createHash('sha256')
    .update(seed, 'utf8')
    .digest('hex')
    .slice(0, 12)
  return `${basis}:${digest}`
}

export function projectName(
  repoRoot: string | null | undefined,
  remoteUrl: string | null | undefined,
  cwd?: string | null,
): string {
  const norm = normalizeRemote(remoteUrl)
  if (norm) return norm.split('/').slice(-2).join('/')
  for (const candidate of [repoRoot, cwd]) {
    if (candidate)
      return basename(rstrip(String(candidate), '/')) || String(candidate)
  }
  return 'unknown'
}

export interface ProjectDesc {
  project_id: string
  project_name: string
  repo_root: string | null
  remote_url: string | null
  remote_normalized: string | null
  is_git: number
  cwd?: string | null
  detection_method?: string
  branch?: string | null
  head_sha?: string | null
  is_dirty?: number
}

/** Project descriptor for a filesystem path. Cheap and cached. */
export function describe(path: string): ProjectDesc {
  const info = repoInfo(path)
  const root = info.repo_root ?? null
  const remote = info.remote_url ?? null
  const cwd = info.cwd ?? null
  return {
    project_id: projectId(root, remote, cwd),
    project_name: projectName(root, remote, cwd),
    repo_root: root,
    remote_url: remote,
    remote_normalized: normalizeRemote(remote),
    is_git: info.is_git ? 1 : 0,
    cwd,
  }
}

/** `describe` plus the live state of the checkout - branch, HEAD, dirty.
 *
 * Three extra git invocations, one of which walks the working tree, so this is
 * for the handful of places that record what a session was sitting on, never
 * for bulk attribution.
 */
export function describeFull(path: string): ProjectDesc {
  const desc = describe(path)
  const directory = desc.is_git ? nearestDir(path) : null
  if (directory) {
    desc.branch = git(['branch', '--show-current'], directory) || null
    desc.head_sha = git(['rev-parse', 'HEAD'], directory) || null
    desc.is_dirty = git(['status', '--porcelain'], directory) ? 1 : 0
  }
  return desc
}

export const UNKNOWN_PROJECT: ProjectDesc = {
  project_id: 'unknown:000000000000',
  project_name: '(unattributed)',
  repo_root: null,
  remote_url: null,
  remote_normalized: null,
  is_git: 0,
  detection_method: 'none',
}

// --- commit reconciliation ---------------------------------------------------

export interface CommitFile {
  path: string
  insertions: number
  deletions: number
}

export interface Commit {
  commit_sha: string
  committed_at: string | null
  author_name: string
  author_email: string
  subject: string
  files: CommitFile[]
  insertions: number
  deletions: number
  files_changed: number
}

/** Read-only listing of commits in a time window, with per-file stats. */
export function commitsSince(
  repoRoot: string,
  sinceIso: string,
  untilIso?: string | null,
): Commit[] {
  // --all would sweep in refs/stash, whose entries are commits but not work
  // anyone authored. --branches --remotes --tags covers the real history.
  const args = [
    'log',
    '--branches',
    '--remotes',
    '--tags',
    `--since=${sinceIso}`,
    '--date=iso-strict',
    '--pretty=format:%x1e%H%x1f%aI%x1f%an%x1f%ae%x1f%s',
    '--numstat',
  ]
  if (untilIso) args.splice(2, 0, `--until=${untilIso}`)
  const out = git(args, repoRoot)
  if (!out) return []

  const commits: Commit[] = []
  for (const rawChunk of out.split('\x1e')) {
    const chunk = strip(rawChunk, '\n')
    if (!chunk) continue
    const nl = chunk.indexOf('\n')
    const header = nl === -1 ? chunk : chunk.slice(0, nl)
    const rest = nl === -1 ? '' : chunk.slice(nl + 1)
    const parts = header.split('\x1f')
    if (parts.length < 5) continue
    const [sha, when, an, ae, subject] = parts as [
      string,
      string,
      string,
      string,
      string,
    ]
    const files: CommitFile[] = []
    let ins = 0
    let dels = 0
    for (const rawLine of rest.split('\n')) {
      const line = rawLine.trim()
      if (!line) continue
      const cells = line.split('\t')
      if (cells.length < 3) continue
      const [a, d, path] = cells as [string, string, string]
      const ai = /^\d+$/.test(a) ? Number.parseInt(a, 10) : 0
      const di = /^\d+$/.test(d) ? Number.parseInt(d, 10) : 0
      ins += ai
      dels += di
      files.push({ path, insertions: ai, deletions: di })
    }
    commits.push({
      commit_sha: sha,
      committed_at: toUtcIso(when),
      author_name: an,
      author_email: ae,
      subject,
      files,
      insertions: ins,
      deletions: dels,
      files_changed: files.length,
    })
  }
  return commits
}

/** Map path -> change type (A/M/D/R/C) for one commit. */
export function commitChangeTypes(
  repoRoot: string,
  sha: string,
): Record<string, string> {
  const out = git(
    ['show', '--name-status', '--pretty=format:', '-m', '--first-parent', sha],
    repoRoot,
  )
  if (!out) return {}
  const result: Record<string, string> = {}
  for (const line of out.split('\n')) {
    const cells = line.trim().split('\t')
    if (cells.length >= 2 && cells[0])
      result[cells[cells.length - 1]!] = cells[0]![0]!
  }
  return result
}

/** ISO timestamp of the commit that first added `path`, if any. */
export function fileFirstAdded(repoRoot: string, path: string): string | null {
  const out = git(
    [
      'log',
      '--diff-filter=A',
      '--follow',
      '--date=iso-strict',
      '--pretty=format:%aI',
      '-1',
      '--',
      path,
    ],
    repoRoot,
  )
  return out ? out.split('\n')[0]! : null
}
