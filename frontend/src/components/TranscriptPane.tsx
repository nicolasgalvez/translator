import type { TranscriptEvent } from "@/types"

interface TranscriptPaneProps {
  events: TranscriptEvent[]
}

export function TranscriptPane({ events }: TranscriptPaneProps) {
  return (
    <aside className="flex h-full min-w-0 flex-col border-r bg-muted/30">
      <div className="border-b px-4 py-3">
        <h1 className="text-sm font-semibold uppercase tracking-wide text-primary">
          Live Transcript
        </h1>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {events.length === 0 ? (
          <div className="pt-12 text-center text-sm text-muted-foreground">
            Waiting for audio...
          </div>
        ) : (
          events.map((event) => (
            <article key={event.id} className="rounded-md border bg-background p-3">
              <div className="text-xs text-muted-foreground">{event.time}</div>
              <div
                className="mt-1 text-sm leading-6 [&_mark]:rounded-sm [&_mark]:bg-primary [&_mark]:px-1 [&_mark]:text-primary-foreground"
                dangerouslySetInnerHTML={{
                  __html:
                    typeof event.render?.html === "string"
                      ? event.render.html
                      : event.text,
                }}
              />
            </article>
          ))
        )}
      </div>
    </aside>
  )
}
