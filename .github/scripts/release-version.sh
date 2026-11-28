#!/usr/bin/env bash
# The version a release branch is about to release, checked against its tags.
#
#   .github/scripts/release-version.sh <branch> <version file>
#   .github/scripts/release-version.sh --next <branch>
#
# A release line is version-<xy>, tagged v<xy>.0.<patch>, or version-<xy>.<n>,
# tagged v<xy>.<n>.<patch>. Each release takes the next patch: the last tag's
# patch plus one, or <xy>.<n>.0 for the first.
#
# With a version file, prints the version it holds if it is that next one, or
# if its tag is already on HEAD (a rerun), and fails naming the one it should
# hold otherwise. With --next, prints the next version.
set -euo pipefail

usage="usage: release-version.sh <branch> <version file> | --next <branch>"
if [[ "${1:-}" == --next ]]; then
  branch="${2:?${usage}}"
  file=""
else
  branch="${1:?${usage}}"
  file="${2:?${usage}}"
fi

if [[ ! "${branch}" =~ ^version-([0-9]+)(\.([0-9]+))?$ ]]; then
  echo "${branch}: not a release branch (version-<xy> or version-<xy>.<n>)" >&2
  exit 1
fi
line="${BASH_REMATCH[1]}.${BASH_REMATCH[3]:-0}"

last=$(git tag --list "v${line}.*" --sort=-v:refname | grep -E "^v${line//./\\.}\.[0-9]+$" | head -n 1 || true)
if [[ -z "${last}" ]]; then
  expected="${line}.0"
else
  expected="${line}.$(( ${last##*.} + 1 ))"
fi

if [[ -z "${file}" ]]; then
  echo "${expected}"
  exit 0
fi

version=$(sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p' "${file}")
if [[ -z "${version}" ]]; then
  echo "${file}: no __version__ = \"x.y.z\" line" >&2
  exit 1
fi

if [[ "$(git rev-parse -q --verify "refs/tags/v${version}^{commit}" || true)" == "$(git rev-parse HEAD)" ]]; then
  echo "${version}"
  exit 0
fi

if [[ "${version}" != "${expected}" ]]; then
  echo "${file} holds ${version}; this release of ${branch} is ${expected}${last:+, after ${last}}" >&2
  exit 1
fi
echo "${version}"
