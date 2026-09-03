import type { MainPaneRenderer, TranscriptEvent, TranscriptFilter } from "@/types"

const transcriptFilters: TranscriptFilter[] = []
const mainPaneRenderers: MainPaneRenderer[] = []

export function addTranscriptFilter(filter: TranscriptFilter) {
  transcriptFilters.push(filter)
}

export function applyTranscriptFilters(event: TranscriptEvent) {
  return transcriptFilters.reduce((current, filter) => filter(current), event)
}

export function addMainPaneRenderer(renderer: MainPaneRenderer) {
  mainPaneRenderers.push(renderer)
}

export function renderMainPane(events: TranscriptEvent[]) {
  return mainPaneRenderers.map((renderer, index) => (
    <div key={index}>{renderer(events)}</div>
  ))
}

export function clearFrontendPlugins() {
  transcriptFilters.length = 0
  mainPaneRenderers.length = 0
}
