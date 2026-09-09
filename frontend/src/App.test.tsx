import { act, cleanup, render, screen } from "@testing-library/react"
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
    cleanup()
    vi.unstubAllGlobals()
  })

  it("renders the resizable transcript and plugin panes", () => {
    render(<App />)

    expect(screen.getByText("Live Transcript")).toBeInTheDocument()
    expect(screen.getByText("Plugin Output")).toBeInTheDocument()
    expect(screen.getByText("Waiting for audio...")).toBeInTheDocument()
  })

  it("keeps rendering the newest 500 transcript events after reaching the limit", () => {
    render(<App />)
    const socket = MockWebSocket.instances[0]

    act(() => socket.onopen?.())
    expect(screen.getByText("connected")).toBeInTheDocument()

    act(() => {
      for (let index = 0; index <= 500; index += 1) {
        socket.onmessage?.({
          data: JSON.stringify({
            type: "transcript",
            event: {
              id: String(index),
              text: `message ${index}`,
              time: `12:00:${String(index).padStart(2, "0")}`,
            },
          }),
        } as MessageEvent)
      }
    })

    let articles = screen.getAllByRole("article")
    expect(articles).toHaveLength(500)
    expect(articles[0]).toHaveTextContent("message 1")
    expect(articles.at(-1)).toHaveTextContent("message 500")
    expect(screen.queryByText("message 0")).not.toBeInTheDocument()

    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({
          type: "transcript",
          event: { id: "501", text: "message 501", time: "12:08:21" },
        }),
      } as MessageEvent)
    })

    articles = screen.getAllByRole("article")
    expect(articles).toHaveLength(500)
    expect(articles[0]).toHaveTextContent("message 2")
    expect(articles.at(-1)).toHaveTextContent("message 501")
    expect(screen.queryByText("message 1")).not.toBeInTheDocument()
    expect(screen.getByText("connected")).toBeInTheDocument()
  })

  it("shows recording failure while live transcription stays connected", () => {
    render(<App />)
    const socket = MockWebSocket.instances[0]

    act(() => socket.onopen?.())
    act(() => {
      socket.onmessage?.({
        data: JSON.stringify({
          type: "status",
          status: "recording-error",
          message: "Recording stopped; live transcription continues.",
        }),
      } as MessageEvent)
    })

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Recording stopped; live transcription continues.",
    )
    expect(screen.getByText("connected")).toBeInTheDocument()
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
