import { realpathSync } from "node:fs"
import { fileURLToPath } from "node:url"

export class NodeVersionPolicy {
  static supportedRange = "^22.12.0 || ^24.0.0 || >=26.0.0"

  static supports(versionOutput) {
    const match = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(
      versionOutput.trim(),
    )
    if (!match) return false

    const major = Number(match[1])
    const minor = Number(match[2])
    return (major === 22 && minor >= 12) || major === 24 || major >= 26
  }

  static rejectionMessage(versionOutput) {
    const activeVersion = versionOutput.trim() || "<empty>"
    return (
      `Unsupported Node.js version ${activeVersion}. ` +
      `Supported versions: ${this.supportedRange}. ` +
      "Install Node.js 22.12 through 22.x, 24.x, or 26+ and try again."
    )
  }
}

const isDirectRun =
  process.argv[1] &&
  realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))

if (isDirectRun) {
  const activeVersion = process.argv[2] ?? process.version
  if (!NodeVersionPolicy.supports(activeVersion)) {
    console.error(NodeVersionPolicy.rejectionMessage(activeVersion))
    process.exitCode = 1
  }
}
