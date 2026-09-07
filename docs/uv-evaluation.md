# Should this project adopt uv?

**Recommendation: adopt.** The one thing that could have blocked it — expressing
Apple-Silicon-only `mlx-whisper` alongside three different torch builds from two
private indexes, in a single lockfile — works. It is verified below, not assumed.

Adoption is a separate ticket. This document is the evidence for taking it.

## The crux: platform- and index-conditional dependencies

The project needs one dependency set to resolve three ways:

| Environment | torch | mlx-whisper |
|---|---|---|
| Apple Silicon laptop | default wheel from PyPI | yes |
| CI (x86 Linux) | CPU-only build, no CUDA runtime | no |
| Docker image | CUDA build | no |

This is the whole question. If uv cannot express it, nothing else matters.

**It can.** This `pyproject.toml` produces one `uv.lock` — 146 packages — that
resolves correctly for all three:

```toml
[project.optional-dependencies]
mlx = ["mlx-whisper; sys_platform == 'darwin' and platform_machine == 'arm64'"]
# torch is transitive (argostranslate -> stanza -> torch), but it must be named
# directly for tool.uv.sources to apply to it at all.
cpu = ["torch; sys_platform == 'linux'"]
cuda = ["torch; sys_platform == 'linux'"]

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
url = "https://download.pytorch.org/whl/cu124"
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
torch 2.6.0+cu124  -> https://download.pytorch.org/whl/cu124      (Docker)
mlx-whisper 0.4.3  -> https://pypi.org/simple, darwin/arm64 only
```

### Three things that had to be discovered by trying

Each of these produced a silent wrong answer or an unhelpful error first. They
are the reason this ticket was worth doing before the migration rather than
during it.

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

Measured on this project's `requirements-dev.txt`, cold cache, same machine:

| | real | user |
|---|---|---|
| `pip install --no-cache-dir` | 4.14s | 1.88s |
| `uv pip install --no-cache` | 0.77s | 0.13s |

About 5x, on the small dependency group.

**This number is honest but narrow, and should not be extrapolated.** The dev
group is three pure-Python packages. CI's slow job is `pylint`, which installs
all of `requirements.txt` plus torch and currently takes ~60s; that time is
dominated by downloading hundreds of megabytes, where a faster resolver helps
proportionally far less. A full before-and-after on the real CI jobs was not
measured and should be part of the migration ticket, not assumed from the number
above.

The more valuable property is not speed. It is the lockfile: there is currently
no lockfile at all, so no two installs are guaranteed identical.

## What this replaces

- `requirements.txt`, `requirements-dev.txt`, `requirements-mlx.txt` become one
  `pyproject.toml` plus a committed `uv.lock`. The mlx file, which today exists
  only to bolt one platform-specific package onto a `-r` include, becomes an extra.
- `run.sh`'s venv bootstrap and `pip install` step become `uv run`, which creates
  and syncs the environment on demand.

## `conftest.py`

The root `conftest.py` from TRAN-2 exists **only** because the repo had no project
file to put `pythonpath` in. Adding `pyproject.toml` makes the idiomatic fix
available:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

The migration should make that swap and delete `conftest.py`. It is the same fix,
declared where a reader expects to find it.

## Risks

- **A committed `uv.lock` with three torch variants is large** and will show up in
  dependency-bump diffs. Acceptable, and far better than the current situation of
  no lock at all.
- **The CUDA version is pinned by index URL** (`cu124`). Moving CUDA versions means
  editing the index URL, not just a version constraint. Worth a comment in the file.
- **Contributors need uv installed.** Keeping `requirements.txt` generated via
  `uv export` during a transition period would avoid a hard cutover; decide in the
  migration ticket whether that is worth the duplication.

## Method

`uv 0.8.20`. Each configuration above was actually locked, and the resulting
`uv.lock` inspected for the source URL and markers of each torch and mlx entry.
The failures described are real command output, not anticipated problems. No
package was installed into this project and no dependency version was changed.
