# Releasing

Release notes live in [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog format) and
are generated from our [Conventional Commits](https://www.conventionalcommits.org/)
by [git-cliff](https://git-cliff.org/) (`cliff.toml`). Releases are cut at
**version boundaries** (semver tags), not on every push.

> **Tag baseline.** This repository starts at `v0.11.0`, tagged on the initial
> public commit; earlier releases were cut in a private predecessor repo and
> their tags do not exist here. `git-cliff --unreleased` and the compare links
> work normally from `v0.12.0` onward.

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

4. **Commit** the version bump + re-locked `uv.lock` + changelog:
   `git commit -am "chore(release): vX.Y.Z"`.

5. **Tag** (annotated): `git tag -a vX.Y.Z -m "vX.Y.Z"`.

6. **Push** the commit and tag: `git push && git push --tags`.

7. **Publish a GitHub Release** from the tag so it shows on the repo's Releases
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
