#!/usr/bin/env -S node --disable-warning=ExperimentalWarning
/** `telemetry` executable.
 *
 * node:sqlite is still flagged experimental, and its warning would land on
 * stderr in front of every command and inside every MCP session. The shebang
 * silences it.
 */

// `telemetry status | head` closes stdout early. That is a normal way to use a
// CLI, not a crash, so a broken pipe ends the process quietly instead of
// throwing an unhandled error out of a write.
for (const stream of [process.stdout, process.stderr]) {
  stream.on('error', (err) => {
    if (err && err.code === 'EPIPE') process.exit(0)
    throw err
  })
}

const { main } = await import('../dist/cli.js')
process.exitCode = await main(process.argv.slice(2))
