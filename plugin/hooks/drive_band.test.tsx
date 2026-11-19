import { expect, test } from 'claude-code/testing'

const HINT = {
  plugin: 'context-gate',
  surface: 'terminal',
  component: 'PromptHint',
  props: { isDraft: false, isWorking: false, hint: '' },
} as const

const RUN = {
  command: 'drive-band',
  args: '',
  origin: { kind: 'plugin', name: 'context-gate' },
  presentation: { isFullscreen: false, columns: 80 },
} as any

const READING = {
  running: true,
  stopped: false,
  project: 'demo',
  session: 3,
  elapsed_s: 65,
  in_session: true,
  doing: null,
  doing_s: null,
  units: null,
  seven_day: null,
  cap: null,
  last: null,
  age_s: 1,
}

type Poll = { exitCode: number; stdout: string } | 'throw'

// Starts one session per poll in `polls`, in order, each session's polls answering with its own
// entry (the band's state carries over from one session to the next), and returns the band's text and the colour of its `● drive` part.
async function bandOf($: any, on: any, polls: Poll[]): Promise<{ text?: string; color?: string }> {
  let n = 0
  on('process.run', (): any => {
    const poll = polls[Math.min(n, polls.length - 1)] // a poll after the last session repeats its answer
    if (poll === 'throw') throw new Error('no such script')
    return { value: poll }
  })
  on('clock.now', () => ({ value: 1000 }))
  on('clock.every', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'drive-band' } }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  for (n = 0; n < polls.length; n++) {
    await $.session.start({ cwd: '/work/demo', surface: 'terminal', isInteractive: true })
  }
  const ui = await $.ui.mount(HINT)
  const found = await ui.find({ type: 'Text', text: /drive/ })
  await ui.unmount()
  return { text: found?.text, color: found?.children?.[0]?.props?.color }
}

async function band($: any, on: any, poll: Poll): Promise<string | undefined> {
  return (await bandOf($, on, [poll])).text
}

const ok = (d: object): Poll => ({ exitCode: 0, stdout: JSON.stringify(d) })
const GREEN = 'green'
const ORANGE = '#FFA500'

test('a running reading inside a session shows the project', async ($, on) => {
  expect(await band($, on, ok(READING))).toContain('● drive demo')
})

test('a running reading inside a session shows the clock', async ($, on) => {
  expect(await band($, on, ok(READING))).toContain('session 3 · 1m05s')
})

test('a reading in a session shows what it is doing and for how long', async ($, on) => {
  const text = await band($, on, ok({ ...READING, doing: 'Run the gate', doing_s: 125 }))
  expect(text).toContain('Run the gate · 2m')
})

test('a reading with a next step shows no next', async ($, on) => {
  const text = await band($, on, ok({ ...READING, next: 'x' }))
  expect(text).not.toContain('next:')
})

test('a running reading between sessions says so', async ($, on) => {
  const text = await band($, on, ok({ ...READING, in_session: false, elapsed_s: null }))
  expect(text).toContain('between sessions (3 run)')
})

test('a stopped reading says so', async ($, on) => {
  const text = await band($, on, ok({ ...READING, running: false, stopped: true }))
  expect(text).toContain('stopped after 3 sessions')
})

test('a fresh running reading is green', async ($, on) => {
  expect((await bandOf($, on, [ok(READING)])).color).toBe(GREEN)
})

test('a stopped reading is blue', async ($, on) => {
  const got = await bandOf($, on, [ok({ ...READING, running: false, stopped: true })])
  expect(got.color).toBe('blue')
})

test('a run that ended without a stop is orange', async ($, on) => {
  const got = await bandOf($, on, [ok({ ...READING, running: false, stopped: false })])
  expect(got.color).toBe(ORANGE)
})

test('a stale reading is orange', async ($, on) => {
  expect((await bandOf($, on, [ok(READING), { exitCode: 1, stdout: '' }])).color).toBe(ORANGE)
})

test('a good poll after a failed one clears the stale mark', async ($, on) => {
  const got = await bandOf($, on, [ok(READING), { exitCode: 1, stdout: '' }, ok(READING)])
  expect(got.color).toBe(GREEN)
})

test('an empty object hides the band', async ($, on) => {
  expect(await band($, on, ok({}))).toBeUndefined()
})

test('an empty object after a good reading hides the band', async ($, on) => {
  expect((await bandOf($, on, [ok(READING), ok({})])).text).toBeUndefined()
})

test('a failed poll first shows nothing', async ($, on) => {
  expect(await band($, on, { exitCode: 1, stdout: '' })).toBeUndefined()
})

const FAILED: [string, Poll][] = [
  ['a non-zero exit', { exitCode: 1, stdout: '' }],
  ['text', { exitCode: 0, stdout: 'oops' }],
  ['broken JSON', { exitCode: 0, stdout: '{not json' }],
  ['an array', { exitCode: 0, stdout: '[1]' }],
  ['a missing script', 'throw'],
]
for (const [name, poll] of FAILED) {
  test(`a failed poll (${name}) after a good one keeps the reading, stale`, async ($, on) => {
    const got = await bandOf($, on, [ok(READING), poll])
    expect(got.text).toContain('● drive demo')
    expect(got.color).toBe(ORANGE)
  })
}

test('a poll started while one is pending does not run the process twice', async ($, on) => {
  let calls = 0
  let release: (poll: Poll) => void = () => {}
  // the first poll hangs until released
  on('process.run', (): any => {
    calls++
    return new Promise(resolve => { release = resolve }).then(value => ({ value }))
  })
  on('clock.now', () => ({ value: 1000 }))
  on('clock.every', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'drive-band' } }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  const first = $.session.start({ cwd: '/work/demo', surface: 'terminal', isInteractive: true })
  await Promise.resolve() // let the first poll reach the process call
  await Promise.resolve()
  // a second poll starts while the first is pending: it is skipped
  await $.session.start({ cwd: '/work/demo', surface: 'terminal', isInteractive: true })
  expect(calls).toBe(1)
  release(ok(READING))
  await first
})

test('a run that ended without a stop says so', async ($, on) => {
  const text = await band($, on, ok({ ...READING, running: false, stopped: false }))
  expect(text).toContain('ended without a stop after 3 sessions')
  expect(text).not.toContain('stopped after')
})

test('the drive-band command hides and shows the band', async ($, on) => {
  on('process.run', (): any => ({ value: ok(READING) }))
  on('clock.now', () => ({ value: 1000 }))
  on('clock.every', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'drive-band' } }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/work/demo', surface: 'terminal', isInteractive: true })

  const first = await $.command.run(RUN)
  expect(first.text).toBe('Drive band hidden.')
  let ui = await $.ui.mount(HINT)
  expect(await ui.find({ type: 'Text', text: /drive/ })).toBeUndefined()
  await ui.unmount()

  const second = await $.command.run(RUN)
  expect(second.text).toBe('Drive band shown.')
  ui = await $.ui.mount(HINT)
  expect(await ui.find({ type: 'Text', text: /drive/ })).toBeDefined()
  await ui.unmount()
})
