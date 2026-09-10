import { useEffect, useMemo, useState } from "react"
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels"
import { History, Radio } from "lucide-react"

import { MainPane } from "@/components/MainPane"
import { TranscriptPane } from "@/components/TranscriptPane"
import { applyTranscriptFilters } from "@/plugins/registry"
import "@/plugins/highlightKeyword"
import type { TranscriptEvent, TranscriptMessage } from "@/types"

type ConnectionState = "connected" | "disconnected"

function websocketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws"
  return `${protocol}://${window.location.host}/ws`
}

function App() {
  const [events, setEvents] = useState<TranscriptEvent[]>([])
  const [connectionState, setConnectionState] =
    useState<ConnectionState>("disconnected")

  useEffect(() => {
    let reconnectTimer: number | undefined
    let closed = false
    let ws: WebSocket

    const connect = () => {
      ws = new WebSocket(websocketUrl())

      ws.onopen = () => setConnectionState("connected")
      ws.onclose = () => {
        setConnectionState("disconnected")
        if (!closed) {
          reconnectTimer = window.setTimeout(connect, 2000)
        }
      }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as TranscriptMessage
        if (payload.type !== "transcript") return
        setEvents((current) => [
          ...current,
          applyTranscriptFilters(payload.event),
        ])
      }
    }

    connect()

    return () => {
      closed = true
      window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [])

  const statusClass = useMemo(
    () =>
      connectionState === "connected"
        ? "bg-accent/20 text-accent"
        : "bg-muted text-muted-foreground",
    [connectionState],
  )

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header className="flex h-14 shrink-0 items-center gap-4 border-b px-4">
        <div className="flex items-center gap-2">
          <Radio className="h-4 w-4 text-primary" aria-hidden="true" />
          <span className="text-sm font-semibold">Live Transcriber</span>
        </div>
        <span className={`rounded-md px-2 py-1 text-xs ${statusClass}`}>
          {connectionState}
        </span>
        <div className="flex-1" />
        <a
          className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted"
          href="/history"
        >
          <History className="h-4 w-4" aria-hidden="true" />
          History
        </a>
      </header>

      <PanelGroup className="min-h-0 flex-1" direction="horizontal">
        <Panel defaultSize={33} minSize={22}>
          <TranscriptPane events={events} />
        </Panel>
        <PanelResizeHandle className="w-1 bg-border transition-colors hover:bg-primary" />
        <Panel defaultSize={67} minSize={35}>
          <MainPane events={events} />
        </Panel>
      </PanelGroup>
    </div>
  )
}

export default App
