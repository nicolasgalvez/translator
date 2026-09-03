import { render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import App from "@/App"
import { applyTranscriptFilters, clearFrontendPlugins } from "@/plugins/registry"
import "@/plugins/highlightKeyword"

class MockWebSocket {
  static instances: MockWebSocket[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  constructor(public url: string) {
    MockWebSocket.instances.push(this)
  }

  close() {}
}

describe("App", () => {
  beforeEach(() => {
    MockWebSocket.instances = []
    vi.stubGlobal("WebSocket", MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("renders the resizable transcript and plugin panes", () => {
    render(<App />)

    expect(screen.getByText("Live Transcript")).toBeInTheDocument()
    expect(screen.getByText("Plugin Output")).toBeInTheDocument()
    expect(screen.getByText("Waiting for audio...")).toBeInTheDocument()
  })
})

describe("highlight plugin", () => {
  afterEach(() => {
    clearFrontendPlugins()
  })

  it("adds highlighted html before transcript rendering", async () => {
    const event = applyTranscriptFilters({
      id: "1",
      text: "This is important.",
      time: "12:00:00",
    })

    expect(event.render?.html).toContain("<mark>important</mark>")
  })
})
