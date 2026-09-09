import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { fireEvent, getByRole, queryByRole } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { JSDOM } from "jsdom"

const template = readFileSync(
  resolve(process.cwd(), "../templates/captions.html"),
  "utf8",
)
const frontendWorkflow = readFileSync(
  resolve(process.cwd(), "../.github/workflows/frontend.yml"),
  "utf8",
)
const pages = []

function createPage() {
  const page = new JSDOM(template, {
    runScripts: "dangerously",
    url: "http://localhost/captions",
  })
  pages.push(page)
  return page
}

afterEach(() => {
  for (const page of pages.splice(0)) page.window.close()
})

it("runs frontend CI when the caption template changes", () => {
  const pathBlock = frontendWorkflow.match(
    /    paths:\n(?<paths>(?:\s+- .+\n)+)/,
  )?.groups?.paths

  expect(pathBlock).toContain('- "templates/captions.html"')
})

describe("caption controls", () => {
  it("uses native upload and reset buttons with an upload description", () => {
    const { window } = createPage()
    const upload = getByRole(window.document.body, "button", {
      name: "Upload a video",
    })

    expect(upload).toHaveAttribute("type", "button")
    expect(upload).toHaveAccessibleDescription(
      "Drop a video file here or click to browse",
    )

    window.showError("Upload failed")
    expect(
      getByRole(window.document.body, "button", {
        name: "Process another video",
      }),
    ).toHaveAttribute("type", "button")
  })

  it("keeps browsing, file selection, and drag-and-drop operable", async () => {
    const { window } = createPage()
    const upload = getByRole(window.document.body, "button", {
      name: "Upload a video",
    })
    const fileInput = window.document.querySelector("#file-input")
    const openFilePicker = vi.spyOn(fileInput, "click").mockImplementation(() => {})
    window.fetch = vi.fn().mockResolvedValue({
      json: async () => ({ job_id: "job-1" }),
    })
    window.setInterval = vi.fn(() => 1)

    upload.click()
    expect(openFilePicker).toHaveBeenCalledOnce()

    const selectedFile = new window.File(["selected"], "selected.mp4")
    fireEvent.change(fileInput, { target: { files: [selectedFile] } })
    await vi.waitFor(() => expect(window.fetch).toHaveBeenCalledTimes(1))

    window.resetForm()
    const droppedFile = new window.File(["dropped"], "dropped.mp4")
    fireEvent.drop(upload, { dataTransfer: { files: [droppedFile] } })
    await vi.waitFor(() => expect(window.fetch).toHaveBeenCalledTimes(2))
  })

  it("defines visible focus treatment for both native controls", () => {
    const { window } = createPage()
    const styleRules = [...window.document.styleSheets].flatMap((sheet) => [
      ...sheet.cssRules,
    ])
    const focusRule = styleRules.find(
      (rule) =>
        rule.selectorText?.includes(".upload-area:focus-visible") &&
        rule.selectorText?.includes(".reset-button:focus-visible"),
    )

    expect(focusRule).toBeDefined()
    expect(focusRule.style.outline).not.toBe("")
    expect(focusRule.style.outlineOffset).not.toBe("")
  })
})

describe("caption status and focus", () => {
  it("updates programmatic progress without stealing focus while polling", async () => {
    const { window } = createPage()
    let poll
    window.setInterval = vi.fn((callback) => {
      poll = callback
      return 1
    })
    window.fetch = vi.fn().mockResolvedValue({
      json: async () => ({
        status: "processing",
        progress: 42,
        message: "Transcribing audio",
      }),
    })
    const liveLink = getByRole(window.document.body, "link", { name: "Live" })
    liveLink.focus()

    window.pollStatus("job-1")
    await poll()

    const progress = getByRole(window.document.body, "progressbar", {
      hidden: true,
      name: "Caption processing progress",
    })
    expect(progress).toHaveAttribute("aria-valuemin", "0")
    expect(progress).toHaveAttribute("aria-valuemax", "100")
    expect(progress).toHaveAttribute("aria-valuenow", "42")
    const status = getByRole(window.document.body, "status", { hidden: true })
    expect(status).toHaveAttribute("aria-live", "polite")
    expect(status).toHaveTextContent("Transcribing audio")
    expect(window.document.activeElement).toBe(liveLink)

    const repeatedAnnouncements = []
    const observer = new window.MutationObserver((records) => {
      repeatedAnnouncements.push(...records)
    })
    observer.observe(status, { childList: true })
    await poll()
    await Promise.resolve()
    observer.disconnect()
    expect(repeatedAnnouncements).toHaveLength(0)
  })

  it("announces and focuses errors", () => {
    const { window } = createPage()

    window.showError("Lost connection to server")

    const error = getByRole(window.document.body, "alert")
    expect(error).toHaveTextContent("Lost connection to server")
    expect(window.document.activeElement).toBe(error)
  })

  it("focuses completed downloads and preserves every download link", () => {
    const { window } = createPage()

    window.showDownloads("job-1", {
      detected_language: "es",
      files: ["captions.original.srt", "captions.translated.srt"],
    })

    const downloads = getByRole(window.document.body, "region", {
      name: "Caption downloads",
    })
    expect(window.document.activeElement).toBe(downloads)
    expect(getByRole(downloads, "link", { name: /captions\.original\.srt/ })).toHaveAttribute(
      "href",
      "/captions/download/job-1/captions.original.srt",
    )
    expect(getByRole(downloads, "link", { name: /captions\.translated\.srt/ })).toHaveAttribute(
      "href",
      "/captions/download/job-1/captions.translated.srt",
    )
  })

  it("clears stale state and returns focus to upload after reset", () => {
    const { window } = createPage()
    window.showDownloads("job-1", {
      detected_language: "es",
      files: ["captions.srt"],
    })
    window.showError("Old failure")
    const reset = getByRole(window.document.body, "button", {
      name: "Process another video",
    })

    reset.click()

    const upload = getByRole(window.document.body, "button", {
      name: "Upload a video",
    })
    expect(window.document.activeElement).toBe(upload)
    expect(window.document.querySelector("#download-list")).toBeEmptyDOMElement()
    expect(window.document.querySelector("#detected-lang")).toBeEmptyDOMElement()
    expect(window.document.querySelector("#error-message")).toBeEmptyDOMElement()
    expect(window.document.querySelector("#progress-message")).toHaveTextContent(
      "Starting...",
    )
    expect(queryByRole(window.document.body, "button", {
      name: "Process another video",
    })).not.toBeInTheDocument()
  })
})
