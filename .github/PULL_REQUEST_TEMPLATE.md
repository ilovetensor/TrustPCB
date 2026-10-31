## What and why

<!-- One or two sentences. Link the issue if there is one: "Fixes #123". -->

## Release label (maintainers apply it before merging)

Merging this pull request creates a release automatically, using its labels and title:

- label `major`: breaking change, bumps the first number (1.2.3 becomes 2.0.0)
- label `minor`: new feature, bumps the middle number (1.2.3 becomes 1.3.0)
- no label: bumps the last number (1.2.3 becomes 1.2.4)

The title becomes the changelog line, so write it as `feat: ...`, `fix: ...` or `docs: ...`.

## Checklist

- [ ] The title is in the form `type: short description` (`feat`, `fix`, `docs`, `test`, `chore`, `refactor`)
- [ ] Tests for the area I changed pass (`bash scripts/test-related.sh <path>`, `npm --prefix web test`)
- [ ] I added or updated tests for new behaviour
- [ ] TrustPCB still never edits a design or outputs modified KiCad or manufacturing files
- [ ] Any new finding or number traces to evidence, and simulated results are labelled Simulated
- [ ] No keys, tokens, `.env` files or uploaded board data are in this change
- [ ] I did not change version numbers by hand (the release workflow does that)
