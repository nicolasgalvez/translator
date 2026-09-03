import { addMainPaneRenderer, addTranscriptFilter } from "@/plugins/registry"
import type { TranscriptEvent } from "@/types"

const KEYWORD = "important"

function escapeHtml(text: string) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;")
}

function highlightText(text: string) {
  const pattern = new RegExp(`(${KEYWORD})`, "ig")
  return escapeHtml(text).replace(pattern, "<mark>$1</mark>")
}

addTranscriptFilter((event: TranscriptEvent) => ({
  ...event,
  render: {
    ...event.render,
    html: highlightText(event.text),
  },
}))

addMainPaneRenderer((events: TranscriptEvent[]) => {
  const matches = events.filter((event) =>
    event.text.toLowerCase().includes(KEYWORD),
  )

  return (
    <section className="rounded-md border bg-muted/40 p-4">
      <h2 className="text-sm font-semibold text-primary">Keyword matches</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Tracking mentions of "{KEYWORD}" from the live transcript.
      </p>
      <div className="mt-4 space-y-3">
        {matches.length === 0 ? (
          <p className="text-sm text-muted-foreground">No matches yet.</p>
        ) : (
          matches.map((event) => (
            <div key={event.id} className="rounded-md border bg-background p-3">
              <div className="text-xs text-muted-foreground">{event.time}</div>
              <div className="mt-1 text-sm">{event.text}</div>
            </div>
          ))
        )}
      </div>
    </section>
  )
})
