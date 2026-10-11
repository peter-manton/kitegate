# Contributing

## Commit messages and PR titles

Every commit message, and every PR title, is a
[Conventional Commit](https://www.conventionalcommits.org/en/v1.0.0/) **with a
scope**:

```
<type>(<scope>): <summary in the imperative, lower case, no full stop>
```

```
feat(packs): add a values pack for primary schools
fix(gateway): hold streamed replies until they are screened
docs(openwebui): explain the global filter setting
```

PRs are squash-merged and the PR title becomes the commit on `main`, so the
title is what counts. It is checked in CI (`.github/workflows/pr-title.yml`),
and commit messages are checked locally by the `commit-msg` hook (see
`docs/hooks.md`).

### Types, and what they release

Releases are cut automatically from `main` by python-semantic-release
(`.github/workflows/release.yml`), from these types:

| Type | Use for | Release |
|---|---|---|
| `feat` | New behaviour a user or operator can see | minor |
| `fix` | A bug fix, including a policy that let content through | patch |
| `perf` | Faster, same behaviour | patch |
| `refactor`, `style` | Code change with no behaviour change | none |
| `test` | Tests only | none |
| `docs` | Documentation only | none |
| `build` | Packaging, dependencies, Dockerfiles | none |
| `ci` | Workflows and hooks | none |
| `chore` | Anything else that ships nothing | none |
| `revert` | Reverting an earlier commit | none |

A breaking change, such as a renamed env var, a changed gateway API,
or a removed pack, takes a `!` after the scope (`feat(gateway)!: ...`) or a
`BREAKING CHANGE:` footer. While the version is 0.x it bumps the minor.

### Scopes

| Scope | Covers |
|---|---|
| `core` | `src/kitegate/` engine, core rules, messages, block log |
| `packs` | Values packs, signing, `kitegate-pack` |
| `gateway` | `src/kitegate/gateway/`, `adapters/gateway/` |
| `images`, `photos` | Image screening, the photo providers |
| `openwebui`, `librechat`, `nemo` | That front end's or upstream's adapter config |
| `deploy` | `deploy/` |
| `tests` | Test infrastructure, where `test` alone is not enough |
| `docs` | Repo-wide docs: README, this file |
| `ci` | Workflows, hooks, review config |
| `repo` | Repo housekeeping: `.gitignore`, licence, templates |
| `deps`, `deps-dev` | Dependency bumps (Dependabot uses these) |
| `release` | Release tooling; the release workflow's own commits are `chore(release)` |

Pick the scope of the change's main effect. If no scope fits, add one here,
in `.pre-commit-config.yaml` and in `.github/workflows/pr-title.yml` in the
same PR.

## Before opening a PR

The checklist in the PR template applies. The short version: `pytest`
passes, and changed packs are left for a maintainer to re-sign. Do not edit version numbers or `CHANGELOG.md` by hand. The release
workflow owns both.
