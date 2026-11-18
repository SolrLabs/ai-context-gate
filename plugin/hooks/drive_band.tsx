import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Drive } from '../types'

const drive = atom({ plugin: 'context-gate', key: 'drive' } as const, null)
const isHidden = atom({ plugin: 'context-gate', key: 'isHidden' } as const, false)
const readAt = atom({ plugin: 'context-gate', key: 'readAt' } as const, 0)
const now = atom({ plugin: 'context-gate', key: 'now' } as const, 0)
const stale = atom({ plugin: 'context-gate', key: 'stale' } as const, false)

const POLL_MS = 15_000

function clock(s: number): string {
  const m = Math.floor(s / 60)
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m` : `${m}m${String(s % 60).padStart(2, '0')}s`
}

function short(text: string | null, max: number): string {
  if (!text) return ''
  const one = text.replace(/\s+/g, ' ').trim()
  return one.length > max ? `${one.slice(0, max - 1)}…` : one
}

export const register: Register = on => {
  let polling = false // one poll at a time, across sessions
  on('session.start', async ($, e, next) => {
    const poll = async () => {
      if (polling) return // a slow poll is still running: skip this one
      polling = true
      try {
        // The project root is the session's folder; drive_status.py takes it as its one argument.
        const argv = ['python3', `${$.plugin.root}/hooks/drive_status.py`]
        if (e.cwd) argv.push(e.cwd)
        const { exitCode, stdout } = await $.process.run(argv, { timeoutMs: 10_000 })
        const text = stdout.trim()
        if (exitCode !== 0) throw new Error(`drive_status.py exited ${exitCode}`)
        const parsed = JSON.parse(text)
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object')
        const value = parsed as Partial<Drive>
        const at = await $.clock.now()
        // `{}` (no run, or one stopped long ago) hides the band
        await update($, drive, () => ('project' in value ? (value as Drive) : null))
        await update($, readAt, () => at)
        await update($, now, () => at)
        if (await read($, stale)) await update($, stale, () => false)
      } catch {
        // a failed poll keeps the last reading and marks it stale; the next good poll clears it
        try {
          await update($, stale, () => true)
        } catch {
          // nothing more to do
        }
      } finally {
        polling = false
      }
    }
    await poll()
    $.clock.every(POLL_MS, poll)
    // tick once a second while a session runs, so the counter moves between polls
    $.clock.every(1000, async () => {
      const d = await read($, drive)
      if (d?.running && d.in_session && d.elapsed_s !== null) {
        const t = await $.clock.now()
        await update($, now, () => t)
      }
    })
    await $.command.register({ name: 'drive-band', description: 'Show or hide the build-drive band.' })
    return next(e)
  })

  on('command.run', { command: 'drive-band' }, async $ => {
    const hidden = await read($, isHidden)
    await update($, isHidden, () => !hidden)
    return { text: hidden ? 'Drive band shown.' : 'Drive band hidden.' }
  })

  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    const d = await read($, drive)
    if (d === null || (await read($, isHidden))) {
      return next(e)
    }
    const line = await next(e)
    const { Box, Text } = $.ui.resolve(e)
    const since = Math.max(0, Math.floor(((await read($, now)) - (await read($, readAt))) / 1000))
    // green: running and fresh; orange: stale, or ended without a stop; blue: stopped
    const color = d.stopped
      ? 'blue'
      : (await read($, stale)) || !d.running
        ? '#FFA500'
        : 'green'
    const state = d.running
      ? d.in_session && d.elapsed_s !== null
        ? `session ${d.session} · ${clock(d.elapsed_s + since)}`
        : `between sessions (${d.session} run)`
      : d.stopped
        ? `stopped after ${d.session} sessions`
        : `ended without a stop after ${d.session} sessions`
    const usage =
      d.seven_day === null ? '' : ` · 7d ${d.seven_day}%${d.cap !== null ? ` / cap ${d.cap}` : ''}`
    const last = d.last ? ` · last ${d.last}` : ''
    const units = d.units ? ` · ${d.units}` : ''
    return (
      <Box flexDirection="column">
        {line}
        <Text>
          <Text color={color}>● drive {d.project}</Text>
          <Text dimColor>
            {' '}
            {state}
            {units}
            {usage}
            {last}
            {d.next ? ` · next: ${short(d.next, 60)}` : ''}
          </Text>
        </Text>
      </Box>
    )
  })
}
