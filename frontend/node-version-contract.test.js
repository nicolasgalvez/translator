import { execFileSync, spawnSync } from "node:child_process"
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"

import { expect, it } from "vitest"

const repositoryRoot = resolve(process.cwd(), "..")
const supportedRange = "^22.12.0 || ^24.0.0 || >=26.0.0"
const policyScript = resolve(repositoryRoot, "frontend/scripts/node-version-policy.js")

function readJson(path) {
  return JSON.parse(readFileSync(resolve(repositoryRoot, path), "utf8"))
}

function read(path) {
  return readFileSync(resolve(repositoryRoot, path), "utf8")
}

function runPolicy(version) {
  return spawnSync(process.execPath, [policyScript, version], {
    encoding: "utf8",
  })
}

function copyLauncherFiles(fixtureRoot) {
  copyFileSync(resolve(repositoryRoot, "run.sh"), resolve(fixtureRoot, "run.sh"))
  for (const name of ["runtime_config.py", "language.py", "websocket_security.py"]) {
    copyFileSync(resolve(repositoryRoot, name), resolve(fixtureRoot, name))
  }
  const scripts = resolve(fixtureRoot, "scripts")
  mkdirSync(scripts)
  copyFileSync(
    resolve(repositoryRoot, "scripts/validate_runtime_config.py"),
    resolve(scripts, "validate_runtime_config.py"),
  )
}

function compareVersions(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return left[index] - right[index]
  }
  return 0
}

function rangeIntervals(range) {
  return range.split("||").map((rawClause) => {
    const clause = rawClause.trim()
    const match = /^(\^|>=)(\d+)\.(\d+)\.(\d+)$/.exec(clause)
    if (!match) throw new Error(`Unsupported test range clause: ${clause}`)

    const lower = match.slice(2).map(Number)
    const upper = match[1] === "^" ? [lower[0] + 1, 0, 0] : null
    return { lower, upper }
  })
}

function rangeIsSubsetOf(candidateRange, allowedRange) {
  const allowedIntervals = rangeIntervals(allowedRange)
  return rangeIntervals(candidateRange).every((candidate) =>
    allowedIntervals.some(
      (allowed) =>
        compareVersions(candidate.lower, allowed.lower) >= 0 &&
        (allowed.upper === null ||
          (candidate.upper !== null &&
            compareVersions(candidate.upper, allowed.upper) <= 0)),
    ),
  )
}

it("keeps package and lock metadata on the supported Node range", () => {
  const packageMetadata = readJson("frontend/package.json")
  const lockMetadata = readJson("frontend/package-lock.json")

  expect(packageMetadata.engines?.node).toBe(supportedRange)
  expect(lockMetadata.packages[""].engines?.node).toBe(supportedRange)
})

it("keeps the project range inside locked Vite and Vitest compatibility", () => {
  const lockMetadata = readJson("frontend/package-lock.json")
  const projectRange = lockMetadata.packages[""].engines.node
  const lockedTools = [
    {
      name: "vite",
      version: "8.2.2",
      engine: "^20.19.0 || >=22.12.0",
    },
    {
      name: "vitest",
      version: "5.0.0",
      engine: "^22.12.0 || ^24.0.0 || >=26.0.0",
    },
  ]

  for (const tool of lockedTools) {
    const lockedPackage = lockMetadata.packages[`node_modules/${tool.name}`]
    expect({
      version: lockedPackage.version,
      engine: lockedPackage.engines.node,
    }).toEqual({ version: tool.version, engine: tool.engine })
    expect(rangeIsSubsetOf(projectRange, lockedPackage.engines.node)).toBe(true)
  }
})

it("configures npm to reject unsupported package engines", () => {
  const engineStrict = execFileSync(
    "npm",
    ["config", "get", "engine-strict"],
    { cwd: resolve(repositoryRoot, "frontend"), encoding: "utf8" },
  ).trim()

  expect(engineStrict).toBe("true")
})

it.each([
  "v22.12.0",
  "v22.16.0",
  "v22.99.99",
  "v24.0.0",
  "v24.99.99",
  "v26.0.0",
  "v99.0.0",
])(
  "accepts supported Node boundary %s",
  (version) => {
    const result = runPolicy(version)

    expect(result.status).toBe(0)
    expect(result.stderr).toBe("")
  },
)

it.each(["v22.11.99", "v23.0.0", "v23.99.99", "v25.0.0", "v25.99.99"])(
  "rejects unsupported Node boundary %s with one actionable error",
  (version) => {
    const result = runPolicy(version)

    expect(result.status, result.stdout + result.stderr).toBe(1)
    expect(result.stderr.trim().split("\n")).toEqual([
      `Unsupported Node.js version ${version}. Supported versions: ${supportedRange}. Install Node.js 22.12 through 22.x, 24.x, or 26+ and try again.`,
    ])
  },
)

it.each(["", "24.0", "not-a-version", "v024.0.0", "v24.0.0 extra"])(
  "rejects malformed Node version output %j",
  (version) => {
    const result = runPolicy(version)
    const activeVersion = version || "<empty>"

    expect(result.status, result.stdout + result.stderr).toBe(1)
    expect(result.stderr.trim().split("\n")).toEqual([
      `Unsupported Node.js version ${activeVersion}. Supported versions: ${supportedRange}. Install Node.js 22.12 through 22.x, 24.x, or 26+ and try again.`,
    ])
  },
)

it("owns the supported range and decision in the Node policy class", () => {
  const script = [
    `import { NodeVersionPolicy } from ${JSON.stringify(policyScript)}`,
    "console.log(JSON.stringify({",
    "  supportedRange: NodeVersionPolicy.supportedRange,",
    "  supported: NodeVersionPolicy.supports('v22.16.0'),",
    "  unsupported: NodeVersionPolicy.supports('v23.0.0'),",
    "}))",
  ].join("\n")
  const result = spawnSync(
    process.execPath,
    ["--input-type=module", "--eval", script],
    { encoding: "utf8" },
  )

  expect(result.status).toBe(0)
  expect(JSON.parse(result.stdout)).toEqual({
    supportedRange,
    supported: true,
    unsupported: false,
  })
})

it("stops unsupported Node before dependency sync, frontend install, or build", () => {
  const fixtureRoot = mkdtempSync(join(tmpdir(), "translator-node-policy-"))
  const frontendDirectory = resolve(fixtureRoot, "frontend")
  const scriptDirectory = resolve(frontendDirectory, "scripts")
  const binaryDirectory = resolve(fixtureRoot, "bin")
  const callLog = resolve(fixtureRoot, "calls.log")

  try {
    mkdirSync(scriptDirectory, { recursive: true })
    mkdirSync(binaryDirectory)
    copyLauncherFiles(fixtureRoot)
    copyFileSync(policyScript, resolve(scriptDirectory, "node-version-policy.js"))
    writeFileSync(resolve(frontendDirectory, "package.json"), '{"type":"module"}\n')
    writeFileSync(resolve(frontendDirectory, "package-lock.json"), "{}\n")

    for (const command of ["npm", "uv"]) {
      const commandPath = resolve(binaryDirectory, command)
      writeFileSync(
        commandPath,
        `#!/bin/sh\nprintf '${command} %s\\n' "$*" >> "$CALL_LOG"\n`,
      )
      chmodSync(commandPath, 0o755)
    }

    const nodePath = resolve(binaryDirectory, "node")
    writeFileSync(
      nodePath,
      `#!/bin/sh\nexec ${JSON.stringify(process.execPath)} "$@" v23.0.0\n`,
    )
    chmodSync(nodePath, 0o755)

    const result = spawnSync("bash", [resolve(fixtureRoot, "run.sh")], {
      encoding: "utf8",
      env: {
        ...process.env,
        CALL_LOG: callLog,
        PATH: `${binaryDirectory}:${process.env.PATH}`,
      },
    })

    expect(result.status, result.stdout + result.stderr).toBe(1)
    expect(result.stderr.trim()).toBe(
      `Unsupported Node.js version v23.0.0. Supported versions: ${supportedRange}. Install Node.js 22.12 through 22.x, 24.x, or 26+ and try again.`,
    )
    const calls = existsSync(callLog) ? readFileSync(callLog, "utf8") : ""
    expect(calls).toBe("")
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true })
  }
})

it("reports how to install a supported Node when the executable is missing", () => {
  const fixtureRoot = mkdtempSync(join(tmpdir(), "translator-missing-node-"))
  const frontendDirectory = resolve(fixtureRoot, "frontend")
  const binaryDirectory = resolve(fixtureRoot, "bin")
  const callLog = resolve(fixtureRoot, "calls.log")

  try {
    mkdirSync(frontendDirectory)
    mkdirSync(binaryDirectory)
    copyLauncherFiles(fixtureRoot)
    writeFileSync(resolve(frontendDirectory, "package.json"), "{}\n")

    for (const command of ["npm", "uv"]) {
      const commandPath = resolve(binaryDirectory, command)
      writeFileSync(
        commandPath,
        `#!/bin/sh\nprintf '${command} %s\\n' "$*" >> "$CALL_LOG"\n`,
      )
      chmodSync(commandPath, 0o755)
    }

    const result = spawnSync("bash", [resolve(fixtureRoot, "run.sh")], {
      encoding: "utf8",
      env: {
        ...process.env,
        CALL_LOG: callLog,
        PATH: `${binaryDirectory}:/usr/bin:/bin`,
      },
    })

    expect(result.status, result.stdout + result.stderr).toBe(1)
    expect(result.stderr.trim()).toBe(
      `Node.js is required but was not found. Install Node.js 22.12 through 22.x, 24.x, or 26+ (supported range: ${supportedRange}), then try again.`,
    )
    expect(existsSync(callLog)).toBe(false)
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true })
  }
})

it("keeps setup docs, package metadata, CI, and Docker on the supported policy", () => {
  const readmeVersion = read("README.md").match(
    /^### Node\.js (?<version>.+)$/m,
  )?.groups?.version
  const packageRange = readJson("frontend/package.json").engines.node
  const workflowVersion = read(".github/workflows/frontend.yml").match(
    /node-version:\s*["'](?<version>\d+)["']/,
  )?.groups?.version
  const dockerVersion = read("Dockerfile").match(
    /^FROM node:(?<version>\d+)-/m,
  )?.groups?.version

  expect(readmeVersion).toBe("22.12 through 22.x, 24.x, or 26+")
  expect(packageRange).toBe(supportedRange)
  for (const version of [workflowVersion, dockerVersion]) {
    expect(version).toBe("26")
    expect(runPolicy(`v${version}.0.0`).status).toBe(0)
  }
})

it("runs the Node contract when any maintained declaration changes", () => {
  const frontendWorkflow = read(".github/workflows/frontend.yml")
  const pathBlock = frontendWorkflow.match(
    /paths: &frontend_paths\n(?<paths>(?:\s+- .+\n)+)/,
  )?.groups?.paths

  for (const path of ["Dockerfile", "README.md", "run.sh"]) {
    expect(pathBlock).toContain(`- "${path}"`)
  }
})

it("enforces package engines during the Docker dependency installation", () => {
  const dockerfile = read("Dockerfile")
  const configCopy = "COPY frontend/package*.json frontend/.npmrc ./"

  expect(dockerfile).toContain(configCopy)
  expect(dockerfile.indexOf(configCopy)).toBeLessThan(
    dockerfile.indexOf("RUN npm ci"),
  )
})
