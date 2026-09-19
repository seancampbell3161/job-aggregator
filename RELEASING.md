# Releasing

Release notes live in [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog format) and
are generated from our [Conventional Commits](https://www.conventionalcommits.org/)
by [git-cliff](https://git-cliff.org/) (`cliff.toml`). Releases are cut at
**version boundaries** (semver tags), not on every push.

> **Tag baseline.** This repository starts at `v0.11.0`, tagged on the initial
> public commit; earlier releases were cut in a private predecessor repo and
> their tags do not exist here. `git-cliff --unreleased` and the compare links
> work normally from `v0.12.0` onward.

> **`publish.yaml` runs from the tagged commit.** GitHub Actions checks out
> `.github/workflows/publish.yaml` as it exists **at the pushed tag**, not
> whatever is on `main` when you push it. Merge any workflow change through
> its own PR first — pushing a tag can never pick up a workflow edit that
> hasn't landed on `main` yet.

git-cliff is not a project dependency and does not need installing —
`uv tool run git-cliff` fetches and runs it. (If you have it on `$PATH`, plain
`git-cliff` works too.)

What it generates is a **skeleton, not the finished notes**: one bullet per
commit subject, grouped. Past entries are edited from there into prose —
grouping related commits, bolding the lead, and dropping bullets that only make
sense to whoever wrote them. Budget for that edit; it's most of the work.

## Cut a release

1. **Pick the version** (semver, pre-1.0): `feat:` → minor bump, `fix:` only →
   patch bump. Set it in `pyproject.toml` (`version = "X.Y.Z"`).

   Then **re-lock** so `uv.lock`'s self-version tracks the bump (otherwise it
   drifts and CI's `uv lock --check` fails):

   ```bash
   uv lock
   ```

2. **Prepend the new section to the changelog** — do NOT regenerate the whole
   file (that would clobber hand-written `### Upgrading` notes in past
   releases):

   ```bash
   uv tool run git-cliff --unreleased --tag vX.Y.Z --prepend CHANGELOG.md
   ```

   Then edit the inserted section into the house style (see above), and **add
   the version's link line by hand** at the bottom of the file, next to the
   others — `--prepend` inserts the body only and never emits the footer that
   defines those links:

   ```
   [X.Y.Z]: https://github.com/seancampbell3161/job-aggregator/compare/vP.R.E..vX.Y.Z
   ```

3. **Add an `### Upgrading` subsection** under the new version heading IF the
   release needs operator action (new/required env vars, migrations, redeploy
   steps, breaking changes). git-cliff can't infer these — write them by hand.
   This is the highest-value part of the notes for anyone running the app.

4. **Commit on a branch.** `main` is protected: it takes no direct pushes, and
   merges need CI green. Release commits go through a PR like any other change.

   ```bash
   git checkout -b release/vX.Y.Z
   git commit -am "chore(release): vX.Y.Z"
   git push -u origin release/vX.Y.Z
   ```

5. **Open the PR and merge it once CI is green.** Title it exactly
   `chore(release): vX.Y.Z` — a squash merge takes the PR title as the commit
   subject on `main`, so the title is what ends up in the history git-cliff
   reads next time.

   ```bash
   gh pr create --title "chore(release): vX.Y.Z" --body "Release vX.Y.Z"
   gh pr checks --watch
   gh pr merge --squash --delete-branch
   ```

6. **Tag the merged commit — not the branch commit.** Squashing creates a *new*
   commit on `main`; the one you made on the branch is not in `main`'s history.
   Tagging before the merge would point the release at a commit nobody can
   reach. So sync first, then tag:

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

   Verify it landed where you expect — the tag and `origin/main` should be the
   same commit:

   ```bash
   git rev-parse vX.Y.Z origin/main     # two identical SHAs
   ```

7. **Watch the image publish.** Pushing the tag triggers
   `.github/workflows/publish.yaml`, which builds slim and `-headless` for
   amd64 and arm64, smoke-tests each, and assembles the manifest lists.

   ```bash
   gh run watch "$(gh run list --workflow=publish.yaml --limit 1 --json databaseId -q '.[0].databaseId')"
   docker buildx imagetools inspect ghcr.io/seancampbell3161/job-aggregator:X.Y.Z
   ```

   The inspect output must list both `linux/amd64` and `linux/arm64`. A tag
   whose manifest has one architecture means one leg's digest never reached
   the merge — check the build matrix before announcing the release.

   > **One-time, on the first publish only:** GHCR creates the package
   > **private**, even for a public repository. Set its visibility to public in
   > the package settings or every `docker pull` in the docs fails with a 404.

8. **Publish a GitHub Release** from the tag so it shows on the repo's Releases
   page, using the `gh` CLI (`brew install gh && gh auth login` once).

   Publish the **`CHANGELOG.md` section you just edited**, not a fresh
   git-cliff render — regenerating would replace your prose with the raw
   commit list and drop the `### Upgrading` notes, which are the whole point:

   ```bash
   awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/notes.md
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/notes.md
   ```

   (Without `gh`: create it in the GitHub UI from the pushed tag and paste the
   new `CHANGELOG.md` section.)

## Notes

- **PR titles are what the changelog is built from.** Merges are squashes, so
  the PR title becomes the commit subject on `main` and individual commits on
  the branch are discarded. A PR titled "fix stuff" produces a changelog bullet
  saying "fix stuff". Write PR titles as Conventional Commits and the rest of
  this file keeps working.
- `cliff.toml` maps commit types to sections — `feat:`→Added, `fix:`→Fixed,
  `docs:`/`refactor:`/`perf:`/`build:`→Changed, anything whose subject reads
  `…: remove`/`…: delete`→Removed — and skips merge commits plus `chore:`,
  `test:`, `ci:`, `style:`, and the per-feature `docs(spec):`/`docs(plan):`
  design artifacts. A `!` breaking marker (`feat!:`) renders the bullet with a
  **BREAKING:** prefix. Keep writing Conventional Commits and the grouping
  stays right for free.
- A scopeless `docs:` commit lands in Changed even when it's an internal spec
  or plan. Scope those `docs(spec):`/`docs(plan):` and they drop out.
- The inaugural `0.2.0` entry back-fills the full history since `0.1.0`; future
  entries are incremental via `--prepend`.
