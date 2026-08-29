/** The OTLP receiver: the only thing standing between Claude Code and disk.
 *
 * Accepts OTLP over HTTP/JSON on loopback and appends each request verbatim to
 * newline-delimited JSON files under ~/.telemetry/raw. That is the whole job.
 * Everything downstream reads those files, so the receiver never needs to know
 * what any of it means, and a crash here can lose at most one in-flight batch.
 *
 * Node's standard library only - no collector, no container, no daemon
 * manager. `telemetry start` runs this in the background.
 */
import { appendFileSync, renameSync, statSync } from 'node:fs'
import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from 'node:http'
import { join } from 'node:path'
import { gunzipSync } from 'node:zlib'
import * as config from './config.js'

/** Roll a raw file over at this size and start a fresh one. Nothing deletes
 * the rolled files: `analyse` reads them all, and pruning is the operator's
 * call. */
const MAX_BYTES = 64 * 1024 * 1024

const PATHS: Record<string, [string, string]> = {
  '/v1/logs': ['logs', 'resourceLogs'],
  '/v1/metrics': ['metrics', 'resourceMetrics'],
  '/v1/traces': ['traces', 'resourceSpans'],
}

function timestampSuffix(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`
  )
}

/** Move a full file aside under a timestamped name. */
function rotate(out: string): void {
  try {
    const dot = out.lastIndexOf('.')
    const stem = out.slice(0, dot)
    const ext = out.slice(dot)
    renameSync(out, `${stem}-${timestampSuffix()}-size${ext}`)
  } catch {
    /* somebody else rotated it, or the directory is gone */
  }
}

function append(signalName: string, payload: unknown): void {
  const out = join(config.RAW_DIR, `${signalName}.jsonl`)
  // Node runs this on one thread, so the append is already serialised; the
  // only ordering that matters is rotate-then-write.
  try {
    if (statSync(out).size >= MAX_BYTES) rotate(out)
  } catch {
    /* the file does not exist yet, which is the common case on first write */
  }
  appendFileSync(out, JSON.stringify(payload) + '\n', 'utf8')
}

function reply(res: ServerResponse, code: number, body = '{}'): void {
  const buf = Buffer.from(body, 'utf8')
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': String(buf.length),
  })
  res.end(buf)
}

function readBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (chunk: Buffer) => chunks.push(chunk))
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

async function handleRequest(
  req: IncomingMessage,
  res: ServerResponse,
): Promise<void> {
  const path = (req.url || '').split('?')[0]!

  if (req.method === 'GET') {
    const normalized = path.replace(/\/+$/, '')
    if (normalized === '/health' || normalized === '') {
      reply(res, 200, '{"status":"Server available"}')
    } else {
      reply(res, 404, '{"error":"not found"}')
    }
    return
  }

  if (req.method !== 'POST') {
    reply(res, 404, '{"error":"not found"}')
    return
  }

  const route = PATHS[path]
  if (!route) {
    reply(res, 404, '{"error":"unsupported signal path"}')
    return
  }
  const [signalName, rootKey] = route

  let payload: Record<string, unknown>
  try {
    let raw = await readBody(req)
    if ((req.headers['content-encoding'] || '').toLowerCase() === 'gzip') {
      raw = gunzipSync(raw)
    }
    const ctype = (req.headers['content-type'] || '').toLowerCase()
    if (!ctype.includes('json')) {
      reply(
        res,
        415,
        JSON.stringify({
          error:
            'this receiver accepts OTLP/JSON only; set ' +
            'OTEL_EXPORTER_OTLP_PROTOCOL=http/json (run ' +
            '`telemetry install` to fix your settings)',
        }),
      )
      return
    }
    payload = JSON.parse(raw.toString('utf8'))
  } catch (exc) {
    reply(res, 400, JSON.stringify({ error: (exc as Error).message }))
    return
  }

  if (!(rootKey in payload)) {
    payload = { [rootKey]: (payload as Record<string, unknown>)[rootKey] ?? [] }
  }
  append(signalName, payload)
  reply(res, 200)
}

export function serve(port?: number): void {
  config.ensureDirs()
  const listenPort = port || config.OTLP_PORT
  const server = createServer((req, res) => {
    handleRequest(req, res).catch((exc) => {
      try {
        reply(res, 500, JSON.stringify({ error: (exc as Error).message }))
      } catch {
        /* the client hung up first */
      }
    })
  })

  const stop = () => {
    server.close(() => {
      process.stdout.write('receiver stopped\n')
      process.exit(0)
    })
  }
  process.on('SIGTERM', stop)
  process.on('SIGINT', stop)

  server.on('error', (err: NodeJS.ErrnoException) => {
    if (err.code === 'EADDRINUSE') {
      process.stderr.write(
        `port ${listenPort} is already in use - another receiver is running, ` +
          `or the previous one has not finished shutting down\n`,
      )
      process.exit(1)
    }
    throw err
  })

  server.listen(listenPort, '127.0.0.1', () => {
    const started = new Date().toISOString().replace(/\.\d{3}Z$/, '+00:00')
    process.stdout.write(
      `[${started}] OTLP/JSON receiver on http://127.0.0.1:${listenPort} -> ${config.RAW_DIR}\n`,
    )
  })
}
