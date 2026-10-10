# Commit hooks

`.pre-commit-config.yaml` holds the hooks. The point of them is that a leaked
credential or an unsafe call is cheapest to deal with before it is published,
and that this repository's two generated artefacts cannot be allowed to drift
from the source they are built from.

## Setup

```bash
scoop install trufflehog      # or: brew install trufflehog
pip install pre-commit
pre-commit install --install-hooks
```

`--install-hooks` builds the hook environments now rather than during your
first commit. There is no `pre-commit` entry in the `dev` extra on purpose:
it manages git hooks for a checkout, which is not something installing the
package should do to someone.

## What runs, and when

Before each commit:

| Hook | Why it is here |
|---|---|
| TruffleHog | Secrets, over the staged changes |
| bandit | Python SAST, over the files you changed |
| detect-private-key | Catches the pack signing key even with TruffleHog absent |
| check-added-large-files, check-case-conflict, check-merge-conflict | Mistakes that are annoying to undo once pushed |
| check-yaml, check-toml, check-json | A malformed pack or `pyproject.toml` fails the suite anyway, slower |
| check-executables-have-shebangs | A script committed executable but unrunnable |
| end-of-file-fixer, trailing-whitespace | Rewrite the file; re-stage and commit again |
| owui-filter-built, owui-photo-built | `dist/` matches the package and adapter it is built from |

Before each push: `pytest`. Too slow for every commit, fast enough before a
push, and the same gate CI applies.

The whole tree rather than one commit's worth of changes:

```bash
pre-commit run --all-files
```

### Files the hooks will not rewrite

`end-of-file-fixer` and `trailing-whitespace` skip `src/kitegate/packs/` and
`dist/`. A pack is signed, so a stripped trailing space invalidates the
signature for no gain, and `dist/` is generated, so the fix belongs in the
source followed by a rebuild. There is no `mixed-line-ending` hook either:
`.gitattributes` already pins the repository to LF, so on a Windows checkout
it rewrites files for a change git cannot even see.

## Why TruffleHog is a `system` hook

TruffleHog's own pre-commit hook is a Go hook, which means pre-commit clones
the repository and builds the binary. That is slow enough that the project's
issue tracker records it as a reason people put off upgrading
([trufflehog#4281](https://github.com/trufflesecurity/trufflehog/issues/4281)).
On one Windows machine the build also produced no binary at all and the hook
quietly used whichever `trufflehog` was already on `PATH` — the wrong failure
mode for a security gate, because it looks like it passed. So the hook calls
the TruffleHog you installed, and keeping it current is on you, which is the
right way round for a scanner whose value is its newest detectors.

The hook passes no flags, which is deliberate. TruffleHog notices that it is
running under pre-commit and sets its own ("Running as a pre-commit hook,
overriding default flags for hook context"), and that is the only arrangement
that reads **staged but uncommitted** content. The flag list its guide offers
as the manual equivalent includes `--since-commit HEAD`, which looks at
commits only: configured that way the hook reports a clean scan while a
staged AWS key sails past it. If you ever see hook output with no pre-commit
line in it, your TruffleHog is too old to rely on here; verified against
3.97.

## False positives

**TruffleHog.** Put `trufflehog:ignore` on the line. Prefer not to commit a
realistic-looking fake credential in the first place.

**bandit.** Only if the finding is genuinely not a problem, and say why:

```python
except Exception:  # nosec B110 - older Open WebUI has no Config: fall through
```

Two such notes exist today, both load-bearing rather than decorative: the
version fallback above, and the `urlopen` in `deploy/openwebui/provision.py`,
whose scheme is checked once where `WEBUI_URL` is read because bandit cannot
see that from the call site. `tests/` is not scanned at all, since its
asserts are the point of the file rather than a finding, and nor is `dist/`,
which is generated from `src/`, which is.

## Skipping, and why CI does not care if you do

```bash
git commit --no-verify        # bypasses all of it
SKIP=pytest git push          # bypasses one hook
```

Both exist, and both are fine for a work-in-progress branch. The hooks are a
fast local first line, not the enforcement point: `.github/workflows/
security.yml` runs the same two scanners where nobody can skip them, over the
whole tree and the whole history rather than one commit's diff, and on a
weekly schedule as well, because a secret scanner finds things next month
with detectors it does not have today.

## If `pre-commit install` refuses

```
[ERROR] Cowardly refusing to install hooks with `core.hooksPath` set.
```

Something else already owns your hooks globally — GitGuardian's `ggshield`
installs itself exactly this way. You do not have to choose between them:
hooks installed by that sort of tool typically run the repository's own
`.git/hooks/pre-commit` first and then do their own work, so both end up
running. Install into the repository while hiding the global config for the
one command, rather than unsetting it:

```bash
# bash
GIT_CONFIG_GLOBAL=/dev/null pre-commit install --install-hooks
```

```powershell
# PowerShell
$empty = New-TemporaryFile
$env:GIT_CONFIG_GLOBAL = $empty
pre-commit install --install-hooks
Remove-Item Env:GIT_CONFIG_GLOBAL
```

Then check it worked by committing something harmless: you should see this
repository's hooks run, followed by the global tool's own output. If you
would rather have only pre-commit, `git config --global --unset-all
core.hooksPath` is the documented fix, but that switches the other tool off
in every repository you have.
