# Why this project adopted uv

**Decision: adopted.** The one thing that could have blocked it — expressing
Apple-Silicon-only `mlx-whisper` alongside three different torch builds from two
private indexes, in a single lockfile — works. It is verified below, not assumed.

The repository now uses this design. This document records the evidence behind
that choice and its current dependency-source contract.

## The crux: platform- and index-conditional dependencies

The project needs one dependency set to resolve three ways:

| Environment | torch | mlx-whisper |
|---|---|---|
| Apple Silicon laptop | default wheel from PyPI | yes |
| CI (x86 Linux) | CPU-only build, no CUDA runtime | no |
| Docker image | CUDA build | no |

This is the whole question. If uv cannot express it, nothing else matters.

**It can.** This `pyproject.toml` produces one `uv.lock` — 148 packages — that
resolves correctly for all three:

```toml
[project.optional-dependencies]
mlx = ["mlx-whisper; sys_platform == 'darwin' and platform_machine == 'arm64'"]
# torch is transitive (argostranslate -> stanza -> torch), but it must be named
# directly for tool.uv.sources to apply to it at all.
cpu = ["torch; sys_platform == 'linux'"]
cuda = ["torch>=2.14.0; sys_platform == 'linux'"]

[tool.uv]
environments = [
    "sys_platform == 'darwin' and platform_machine == 'arm64'",
    "sys_platform == 'linux' and platform_machine == 'x86_64'",
]
# cpu and cuda can never be installed together; declaring the conflict lets uv
# lock a valid resolution for each instead of failing to unify them.
conflicts = [[{ extra = "cpu" }, { extra = "cuda" }]]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[[tool.uv.index]]
name = "pytorch-cuda"
url = "https://download.pytorch.org/whl/cu126"
explicit = true

[tool.uv.sources]
torch = [
    { index = "pytorch-cpu", extra = "cpu" },
    { index = "pytorch-cuda", extra = "cuda" },
]
```

Resulting lock entries:

```
torch 2.14.0       -> https://pypi.org/simple                     (macOS)
torch 2.14.0+cpu   -> https://download.pytorch.org/whl/cpu        (CI)
torch 2.14.0+cu126 -> https://download.pytorch.org/whl/cu126      (Docker)
mlx-whisper 0.4.3  -> https://pypi.org/simple, darwin/arm64 only
```

### Three things that had to be discovered by trying

Each of these produced a silent wrong answer or an unhelpful error during the
original evaluation. Recording them keeps future dependency changes from
reintroducing the same source-selection mistakes.

**1. A source on a transitive dependency is silently ignored.** The obvious
first attempt declares `[tool.uv.sources] torch = {index = "pytorch-cpu"}`
without listing torch in `dependencies`. `uv lock` **succeeds**, exits 0, and
produces a lockfile in which torch comes from pypi.org. No warning. The build
would be quietly wrong. torch must be a direct dependency for the source to
apply, even though nothing imports it directly.

**2. `sys_platform` cannot separate CI-Linux from Docker-Linux.** Both are
`linux`. Marker-only routing sends both to whichever index you name, so the GPU
image silently gets CPU torch. Extras plus `conflicts` are what distinguish
them, and `conflicts` is required — without it uv tries to unify cpu and cuda
into one resolution and fails.

**3. `required-environments` is the wrong knob and fails confusingly.** It
demands wheels exist for every listed environment, so it drags the `cuda` extra
onto macOS arm64 and fails with `torch>=2.0.0 has no arm64-compatible wheels` —
an error that reads like a torch problem rather than a configuration one. The
correct key is `environments`, which restricts resolution instead of demanding it.

## Speed

Measured during the original evaluation on the former `requirements-dev.txt`,
cold cache, same machine:

| | real | user |
|---|---|---|
| `pip install --no-cache-dir` | 4.14s | 1.88s |
| `uv pip install --no-cache` | 0.77s | 0.13s |

About 5x, on the small dependency group.

**This number is honest but narrow, and should not be extrapolated.** The dev
group was three pure-Python packages. The number does not measure current CI or
the complete locked application graph, so it should not be used as a current
end-to-end benchmark.

The more valuable property is not speed. It is the committed lockfile, which now
makes the supported installs reproducible.

## What this replaced

- `requirements.txt`, `requirements-dev.txt`, and `requirements-mlx.txt` became
  one `pyproject.toml` plus a committed `uv.lock`. The platform-specific MLX
  dependency is now an extra.
- `run.sh`'s venv bootstrap and `pip install` step became `uv run`, which creates
  and syncs the environment on demand.

## `conftest.py`

The former root `conftest.py` existed only because the repository had no project
file in which to configure `pythonpath`. `pyproject.toml` now contains the
idiomatic configuration:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

The migration made that swap and removed `conftest.py`.

## Risks

- **A committed `uv.lock` with three torch variants is large** and will show up in
  dependency-bump diffs. Acceptable, and far better than the current situation of
  no lock at all.
- **The CUDA version is pinned by index URL** (`cu126`). Moving CUDA versions means
  editing the index URL, not just a version constraint. Worth a comment in the file.
- **Contributors need uv installed.** The current dependency exports and lock
  validation were verified with uv 0.12.12; normal locked syncs remain
  compatible with the supported local toolchain.

## Method

The original evaluation used uv 0.8.20. The current cu126/Torch 2.14 dependency
graph was re-locked and re-verified with uv 0.12.12. The resulting `uv.lock` and
target exports were inspected for the source URL, version, and selection markers
of each Torch, CUDA, and MLX entry. The failures described above are preserved
historical command results, not anticipated problems.
