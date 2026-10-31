#!/usr/bin/env bash
# Maintainer, once, after the GitHub repository exists: create the two release labels.
# Needs the GitHub CLI (gh), logged in with permission to manage labels on the repository.
#
#   bash scripts/create-labels.sh                 # the repository of the current folder
#   bash scripts/create-labels.sh OWNER/NAME      # a named repository
#
# Safe to run again: --force updates a label that already exists.
set -euo pipefail
repo=()
[[ $# -ge 1 ]] && repo=(--repo "$1")
gh label create major --color B60205 --description "Breaking change: the release bumps the first number (X.0.0)" --force "${repo[@]+"${repo[@]}"}"
gh label create minor --color 0E8A16 --description "New feature: the release bumps the middle number (0.Y.0)" --force "${repo[@]+"${repo[@]}"}"
