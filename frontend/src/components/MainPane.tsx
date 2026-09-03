import { renderMainPane } from "@/plugins/registry"
import type { TranscriptEvent } from "@/types"

interface MainPaneProps {
  events: TranscriptEvent[]
}

export function MainPane({ events }: MainPaneProps) {
  const pluginContent = renderMainPane(events)

  return (
    <main className="flex h-full min-w-0 flex-col">
      <div className="border-b px-5 py-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-accent">
          Plugin Output
        </h2>
      </div>
      <div className="flex-1 overflow-y-auto p-5">
        {pluginContent.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            No plugin output registered.
          </div>
        ) : (
          <div className="space-y-4">{pluginContent}</div>
        )}
      </div>
    </main>
  )
}
