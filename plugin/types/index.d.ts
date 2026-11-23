export type Drive = {
  running: boolean
  stopped: boolean
  project: string
  session: number
  elapsed_s: number | null
  in_session: boolean
  doing: string | null
  doing_s: number | null
  units: string | null
  seven_day: number | null
  cap: number | null
  last: string | null
  age_s: number
}

declare module 'claude-code' {
  interface PluginState {
    'context-gate': {
      drive: Record<string, Drive | null>
      isHidden: boolean
      readAt: Record<string, number>
      now: number
      stale: Record<string, boolean>
    }
  }
}
