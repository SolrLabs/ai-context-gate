export type Drive = {
  running: boolean
  stopped: boolean
  project: string
  session: number
  elapsed_s: number | null
  in_session: boolean
  next: string | null
  units: string | null
  seven_day: number | null
  cap: number | null
  last: string | null
  age_s: number
}

declare module 'claude-code' {
  interface PluginState {
    'context-gate': { drive: Drive | null; isHidden: boolean; readAt: number; now: number; stale: boolean }
  }
}
