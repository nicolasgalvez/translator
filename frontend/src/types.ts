import type { ReactNode } from "react"

export interface TranscriptEvent {
  id: string
  text: string
  time: string
  metadata?: Record<string, unknown>
  render?: Record<string, unknown>
}

export interface TranscriptMessage {
  type: "transcript"
  event: TranscriptEvent
}

export interface StatusMessage {
  type: "status"
  status: "recording-error"
  message: string
}

export type ServerMessage = TranscriptMessage | StatusMessage

export type TranscriptFilter = (event: TranscriptEvent) => TranscriptEvent
export type MainPaneRenderer = (events: TranscriptEvent[]) => ReactNode
