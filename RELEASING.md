# Releasing mom-bot

Authoritative release process for the repo's `v*` tags. This document exists because siege-web's v1.3.0 release cut the tag before updating `CHANGELOG.md` or version sources — the exact footgun this checklist prevents.

## What a release does

**A release publishes artifacts; it does not deploy production.**

CI on `main` publishes `ghcr.io/glitchwerks/mom-bot:<commit-sha>`. Pushing a `v*` tag triggers `.github/workflows/release.yml`, which verifies that the tagged commit is on `main` and that its CI image exists, then attaches the version tag to that exact image and publishes a GitHub Release. The retained Azure deployment workflow is disabled after the VPS cutover. Production remains pinned until the coordinated, separately approved VPS promotion in [rsl-mom-apps#25](https://github.com/glitchwerks/rsl-mom-apps/issues/25) is implemented and accepted.

`release.yml` also posts the Discord announcement directly as a final `notify` job (after the GitHub Release is created). It does **not** rely on a separate `release: published`-triggered workflow — GitHub does not fire cross-workflow events from `GITHUB_TOKEN`-raised release events, so a separate trigger would never fire (#274). The notification is configured via the `DISCORD_RELEASE_WEBHOOK_URL` repo secret; if the secret is absent the notify job logs a warning and exits cleanly so the overall release is not blocked.

`.github/workflows/notify-discord-release.yml` is a **manual remediation tool** (`workflow_dispatch` only, input: `tag`). Use it to re-post the Discord announcement for a given tag if the automatic post in `release.yml` failed or needs to be resent. It has no idempotency guard — a manual dispatch is an intentional re-post.

### Discord Highlights convention

The Discord announcement includes a description pulled from a `### 📣 Highlights` sub-section inside the release's CHANGELOG entry. This is the author's opportunity to write a plain-language, member-facing summary — distinct from the engineering detail in the keep-a-changelog sub-sections (`### Added`, `### Fixed`, etc.).

**What to put in Highlights:**
- One short paragraph or 3–5 bullets describing user-facing impact.
- What changed for members/operators — not implementation minutiae.
- Enough context for someone not following the PR trail to understand why this release matters.

**How to add it:**
Add a `### 📣 Highlights` sub-section under the version heading in `CHANGELOG.md` before opening the release PR. The `release.yml` workflow copies the full version section into the GitHub Release body, so the Highlights block flows through automatically.

```markdown
## [X.Y.Z] - YYYY-MM-DD

### 📣 Highlights

Brief member-facing summary of what changed and why it matters.

### Added

- Engineering detail here.
```

The emoji prefix (`📣`) is optional — `### Highlights` (without emoji) also works. If no Highlights section is present when the release publishes, the Discord post falls back to: `"<tag> published. View notes: <url>"`. The notification is never silently dropped.

**Soft cap:** Descriptions longer than 1 500 characters are truncated and a "View full release notes" link is appended. Keep Highlights concise.

**Required repo secret:**
`DISCORD_RELEASE_WEBHOOK_URL` must be set as a repository secret. To create a Discord webhook: open the target channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL. Add that URL at `https://github.com/glitchwerks/rsl-mom-bot/settings/secrets/actions`.

## Pre-tag checklist

Run through every item **before** running `git tag`. The metadata updates (CHANGELOG, version) must land in a commit on `main` that the tag will point at. Tagging before these steps produces an inconsistency between the image label, the `/api/version` response, and the in-app changelog — the failure mode this document exists to prevent.

### 1. Determine the new version

Follow semver from v1.0.0 onward. See [Versioning policy](#versioning-policy) below for what counts as a breaking change, minor addition, or patch fix.

```bash
git fetch --tags origin
git log v<previous>..main --oneline
```

Review that log to determine the appropriate bump.

### 2. Finalize `CHANGELOG.md`

Every PR merged since the previous tag should have an entry under `## [Unreleased]` in `CHANGELOG.md`. The release-cutter is responsible for filling any gaps before tagging.

- Replace `## [Unreleased]` with:
  ```
  ## [Unreleased]

  ## [X.Y.Z] - YYYY-MM-DD
  ```
  (Preserve an empty `[Unreleased]` heading so the next cycle has somewhere to land entries.)
- Use today's date in ISO 8601 (`YYYY-MM-DD`).
- Sub-section order: `### Added`, `### Changed`, `### Fixed`, `### Infrastructure`, `### Documentation`. Omit empty sub-sections.
- Keep-a-Changelog format: https://keepachangelog.com/

### 3. Bump the version in `pyproject.toml`

mom-bot's version has a single source of truth: `pyproject.toml`'s `[project] version` field (line 7).

- **`pyproject.toml`** — `[project] version = "X.Y.Z"` (line 7)

`src/mom_bot/__init__.py` resolves `__version__` at import time via `importlib.metadata.version("mom-bot")`, so it picks up whatever is installed. `/ping` and `GET /api/version` report this resolved value at runtime — no separate file edit is required, but the package must be reinstalled (`uv pip install -e .`) for the new version to be visible to a running interpreter in a dev checkout. CI and the prod image build install from the bumped `pyproject.toml`, so they pick up the new value automatically.

Bump the version in the same commit as the CHANGELOG promotion.

### 4. Open a release PR

Branch name: `chore/release-vX.Y.Z`.

PR title: `chore(release): vX.Y.Z`.

PR body must contain:
- The release notes (same content as the `[X.Y.Z]` section of `CHANGELOG.md`)
- Any PRs from `v<previous>..HEAD` deliberately skipped from the changelog, with a one-line justification
- `Closes #<release-tracking-issue>` for the milestone tracking issue

Merge via squash-merge after CI is green.

### 5. Verify CI is green on the merge commit

After the release PR squash-merges, `build-image.yml` builds and pushes the GHCR image for that SHA. Before tagging, confirm:

- The CI workflow (`ci.yml`) shows green on the merge commit
- The `build-image.yml` run for that SHA completed successfully
- The image is reachable: `docker manifest inspect ghcr.io/glitchwerks/mom-bot:<merge-sha>`

### 6. Isolated candidate smoke

Before tagging, test the exact CI SHA image with an isolated Discord identity and the portable Compose configuration. Verify `/ping`, sidecar authentication, scheduler progress, and day-role sync in the test guild. Record the immutable digest and source SHA. Do not start a second gateway with the production token. If smoke fails, fix the issue, merge a new commit, and restart from step 4.

## Tag-push procedure

After the release PR is merged, CI is green, and smoke is verified:

```bash
# From your local checkout, on main
git fetch origin --tags
git checkout main
git pull origin main

# Confirm HEAD is the release PR's squash commit
git log -1

# Annotated tag
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

### What to watch after pushing

1. **`release.yml` workflow** — confirm the run published the GitHub Release and attached `:vX.Y.Z` to the CI-approved SHA image.
2. **GitHub Release page** — verify it appears at `https://github.com/glitchwerks/rsl-mom-bot/releases/tag/vX.Y.Z` with the correct release notes.
3. **GHCR image** — confirm the versioned image is reachable: `docker manifest inspect ghcr.io/glitchwerks/mom-bot:vX.Y.Z`.

### Deploy is separate

The tag does not deploy to production. The old Azure `deploy.yml` workflow is disabled; do not re-enable it for the VPS. The future guarded promotion must use a combined Siege/Mom release manifest, a fresh approval, verified backup and compatible migration gate, and post-promotion health evidence. Track that work in [Mom #387](https://github.com/glitchwerks/rsl-mom-bot/issues/387) and [coordination #25](https://github.com/glitchwerks/rsl-mom-apps/issues/25).

## Versioning policy

mom-bot follows semver from v1.0.0 onward.

**MAJOR** — breaking changes that require coordinated action from consumers:
- Sidecar HTTP contract changes that break siege-web consumers (renamed/removed endpoints or fields, changed auth scheme). These must go through the contract change protocol in `rsl-mom-apps` — see `glitchwerks/rsl-mom-apps`'s `CLAUDE.md` for the protocol.
- Discord slash command name or signature changes that disrupt users (e.g., renaming `/ping` to `/health`).

**MINOR** — additive, backward-compatible changes:
- New sidecar API endpoints (siege-web can ignore them).
- New Discord slash commands.
- New environment variables with documented defaults.

**PATCH** — backward-compatible fixes and internal improvements:
- Bug fixes that do not change the external contract.
- Observability improvements (new log lines, metrics).
- Dependency updates and security patches.
- Documentation and runbook updates.

When in doubt, prefer the higher bump.

## Hotfix flow

A hotfix can be developed on a branch from a release tag when `main` has unreleased breaking work. The release tag itself must point to the tested merge commit on `main`, because the release workflow promotes the CI-approved `main` SHA image. If merging the hotfix into `main` would also ship incompatible work, first reconcile or revert that work in a reviewed PR; do not tag the untested hotfix branch.

The hotfix version is the next patch bump from the tag you branched from — e.g. if you're hotfixing `v1.2.3`, the hotfix tag is `v1.2.4`. (Semver uses `.` as the bump separator; `+` is reserved for build metadata and is NOT how patch increments are spelled.)

```bash
# Create a hotfix branch from the release tag (example: hotfixing v1.2.3 → v1.2.4)
git checkout -b hotfix/v1.2.4 v1.2.3

# Cherry-pick the fix commit(s) from main
git cherry-pick <fix-sha>

# Apply the same pre-tag checklist:
# - Add a CHANGELOG entry under the new version
# - Bump version in pyproject.toml
# - Push the branch; open a PR targeting main (for the record and CI)
# - Merge the PR into main; wait for main CI and its SHA image
# - Smoke that exact image in the isolated pilot
git checkout main
git pull origin main
git tag -a v1.2.4 -m "v1.2.4"
git push origin v1.2.4
```

Keep the hotfix branch for the audit trail; the tagged source is the reviewed merge commit on `main`.

## RC and pre-release tags

Use the `-rc.N` suffix for release candidates: `v1.0.0-rc.1`, `v1.0.0-rc.2`, etc. Increment `N` for each successive candidate if the previous RC uncovered a blocking issue.

`release.yml` should treat RC tags the same as full releases: push the `:vX.Y.Z-rc.N` GHCR image and create a GitHub Release marked as pre-release. This lets siege-web (or any consumer) test against the immutable RC image before the stable tag is cut.

RC tags follow the same pre-tag checklist. The only difference: the `[Unreleased]` section is not promoted to a stable version heading until the final tag. Use `## [X.Y.Z-rc.N] - YYYY-MM-DD` in CHANGELOG if you want to record the RC history, or simply accumulate under `[Unreleased]` and promote once at the stable tag.

## Why this document exists

siege-web's v1.3.0 release cut the tag before updating `CHANGELOG.md` or version sources; the tag had to be re-cut at the metadata-fix HEAD after the fact.
