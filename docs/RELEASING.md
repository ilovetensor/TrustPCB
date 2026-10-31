# Releasing

Releases are automatic. When a pull request is merged to `main`, the **Release** workflow (`.github/workflows/release.yml`) bumps the version, updates `CHANGELOG.md`, tags the commit and creates a GitHub Release. Nobody edits version numbers by hand.

## How a release is chosen

The labels on the merged pull request decide the version (X.Y.Z):

| Label | Bump | Example |
|---|---|---|
| `major` | the first number | 1.2.3 becomes 2.0.0 |
| `minor` | the middle number | 1.2.3 becomes 1.3.0 |
| anything else, or none | the last number | 1.2.3 becomes 1.2.4 |

If both `major` and `minor` are present, `major` wins. Labels are read at the moment of the merge, so apply them before merging; a label added afterwards changes nothing.

The base is the highest `vX.Y.Z` tag, or the version in `package.json` if that is higher. **Until the first tag exists, nothing has been released**: the first merged pull request releases the version already in the files, 0.1.0, whatever its labels, and the entries waiting under Unreleased in `CHANGELOG.md` become the 0.1.0 section. After that, labels apply as in the table.

## What the workflow does

1. Starts on `pull_request_target` with type `closed` for pull requests targeting `main`, and does nothing unless the pull request was merged (and is not itself a `chore(release)` change).
2. Checks out `main` (never the pull request's code) with its tags.
3. Runs `node scripts/release.mjs apply`, which reads the labels, computes the next version, writes it to **every** version field in one step and adds the changelog entry:
   - `package.json`
   - `web/package.json` and both version fields in `web/package-lock.json`
   - `server/pyproject.toml` and the `trustpcb-server` entry in `server/uv.lock`
4. Commits the six files as `chore(release): vX.Y.Z (#N) [skip ci]` and pushes to `main`. If the push loses a race with another change, it starts again from the latest `main` and recomputes.
5. Tags the commit `vX.Y.Z` and creates the GitHub Release with the changelog section as its notes.

The changelog line is built from the pull request title and number. `feat:` becomes an entry under Added, `fix:` under Fixed, anything else under Changed, for example `- Flag connectors whose opening a cable cannot reach (#41)`. Whatever is under `## [Unreleased]` when the release runs moves into the new dated section together with that line, so hand-written notes are kept. The date is the day the workflow runs (UTC).

### Safe to retry, and no loops

- The pull request number in the changelog is the record. If the workflow runs again for the same pull request (a re-run, a retry after a failure), it finds the entry, makes no new commit, and only completes what is missing: the tag, then the GitHub Release. Existing tags and releases are never overwritten.
- Runs for the same pull request never overlap (a concurrency group per pull request).
- The workflow cannot trigger itself. It runs on closed pull requests, not on pushes. Its own commit goes in through `GITHUB_TOKEN`, which does not start other workflows, and it also carries `[skip ci]` and a `chore(release)` title that the workflow ignores.
- The workflow's permission is `contents: write` for that one job; the rest of the workflow has none. Actions are pinned to full commit SHAs with the version in a comment.

## Check it locally

```sh
npm run check:versions                                  # all version fields agree (CI runs this too)
npm run test:release                                    # unit tests for the release logic (CI runs these too)
node scripts/release.mjs plan --labels minor            # prints the bump and next version, changes nothing
```

`node scripts/release.mjs apply --pr 12 --title "feat: x" --labels minor` really edits your working tree; use a scratch clone.

## Set up once the repository exists (maintainer)

The repository does not exist yet, so none of this has been done. Do it once, in this order.

1. **Create the labels.** With the GitHub CLI logged in, from a clone:

   ```sh
   bash scripts/create-labels.sh OWNER/REPO
   ```

   or by hand, one line each: `gh label create major --color B60205 --description "Breaking change" --force` and `gh label create minor --color 0E8A16 --description "New feature" --force`.
2. **Allow the workflow to write.** Settings, Actions, General, Workflow permissions: either leave the default and rely on the `permissions` block in the workflow (it asks for `contents: write` itself), or set "Read and write permissions". Pull requests from forks never get a write token from this setting.
3. **Branch protection (important).** The release commit is pushed straight to `main` by the workflow, so a rule that requires pull requests would refuse it. The first release will fail with "Could not push the release commit to main" and the log says so. Pick one:
   - **Let the workflow bypass the rule.** In a repository ruleset for `main`, add `github-actions` to the bypass list if your plan offers it. This keeps the built-in `GITHUB_TOKEN`, which is the simplest setup and the one the workflow assumes.
   - **Or use your own token.** Create a fine-grained personal access token (or a GitHub App token) that belongs to someone allowed to bypass the rule, with Contents read and write on this repository only, and save it as the repository secret `RELEASE_TOKEN`. The workflow uses it automatically when it exists and falls back to `GITHUB_TOKEN` when it does not. Nothing assumes that secret exists. A push made with a personal token does start other workflows; that cannot loop, because the Release workflow only reacts to closed pull requests.
   - **Or protect `main` without requiring pull requests for the bot**, for example by requiring only status checks and not pull requests.

   If you use tag protection rules, allow `v*` tags to be created by the same actor.
4. **Require the CI check.** In branch protection, require the single check named **CI result**. It passes when the real jobs pass or were skipped for a docs-only change, so docs-only pull requests are never stuck waiting for a job that did not run.
5. **Turn on private vulnerability reporting** (Settings, Code security) so that SECURITY.md is true.
6. **Fill in the Code of Conduct contact** (`[INSERT CONTACT METHOD]` in `CODE_OF_CONDUCT.md`).
7. **Add the repository links you left out on purpose.** Badges in the README need the real `OWNER/REPO` and CODEOWNERS needs real usernames; neither was added because the names are not known yet. Set `TRUSTPCB_REPO_URL` for the read-only demo notice if you host one.
8. **Dependabot** (`.github/dependabot.yml`) starts working by itself once the repository is on GitHub. It opens at most one grouped pull request per ecosystem per month. Merging one creates a patch release like any other pull request.

## If a release goes wrong

- **The workflow failed halfway.** Re-run it from the Actions tab (open the failed run, Re-run all jobs). It is idempotent: it completes the tag and the GitHub Release without bumping again.
- **The push was refused.** Fix branch protection (step 3) and re-run.
- **A wrong label was used.** Fix it by hand in a new pull request: do not rewrite tags. Releasing the next version with the right label is usually enough; a published tag should not move.
- **Version fields disagree.** CI fails with a list of the files. Run `node scripts/release.mjs set X.Y.Z` to write one version to all of them.
