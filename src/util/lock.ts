/** An advisory lock that a dead process cannot wedge.
 *
 * The Python this replaces used `flock`, whose whole appeal was that the
 * kernel drops it when the holder dies, so a crashed analyse could not block
 * every future one. Node exposes no `flock`, so the same property is rebuilt
 * from an exclusively-created file holding the owner's pid: a lock whose
 * owner is gone is taken over rather than waited on.
 */
import {
  openSync,
  closeSync,
  writeSync,
  readFileSync,
  unlinkSync,
} from 'node:fs'

export interface Lock {
  release(): void
}

function ownerAlive(path: string): boolean {
  let pid: number
  try {
    pid = Number.parseInt(readFileSync(path, 'utf8').trim(), 10)
  } catch {
    return false // vanished between the failed create and the read
  }
  if (!Number.isFinite(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    // EPERM means it exists and belongs to somebody else: still alive.
    return (err as NodeJS.ErrnoException).code === 'EPERM'
  }
}

/** Take the lock, or return null if somebody live is holding it. */
export function tryAcquire(path: string): Lock | null {
  for (let attempt = 0; attempt < 2; attempt++) {
    let fd: number
    try {
      fd = openSync(path, 'wx', 0o644)
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'EEXIST') return null
      if (ownerAlive(path)) return null
      try {
        unlinkSync(path)
      } catch {
        /* another process got there first; the retry will find out */
      }
      continue
    }
    writeSync(fd, String(process.pid))
    let released = false
    const release = () => {
      if (released) return
      released = true
      try {
        closeSync(fd)
      } catch {
        /* already closed */
      }
      try {
        unlinkSync(path)
      } catch {
        /* already gone */
      }
    }
    process.once('exit', release)
    return { release }
  }
  return null
}
