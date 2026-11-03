# Changelog

All notable changes to hail. Versions follow [semantic versioning](https://semver.org); before 1.0, a minor version may change commands or output.

## Unreleased

### CI and security

- Pin actions to commit SHAs, least-privilege permissions

## 0.6.1 - 2026-11-03

### Fixed

- Keep an agent's own status line out of its replies

## 0.6.0 - 2026-11-02

### Added

- Idempotent send --ref and [policy] cli_access
- Hail tui, a keyboard screen for threads, replies and agents

## 0.5.0 - 2026-11-02

### Added

- Documented exit codes, --json everywhere, help agents, agy preset

## 0.4.1 - 2026-11-01

### Added

- Pinned SSH keys and a daily cap per agent

### Documentation

- Install from GitHub until the PyPI release
- 45-second explainer at the top of the README
- Install from PyPI (hail-agent)

## 0.4.0 - 2026-10-28

### Added

- Inbox mailbox folder and a clear that sticks
- Advice mode enforced by the agent itself

## 0.3.1 - 2026-10-28

### Added

- Talk to your AI agents locally or over SSH
- Let agents consult each other (v0.2.0)
- Hail jobs and hail inbox (read-only view of agent crons and their reports)
- Journal retention, server-side timeout, codex/gemini/ollama presets

### Fixed

- Validate MCP arguments before loading the config; isolate tests
- Keep the journal owner-only, publish to PyPI

### Documentation

- Note the unrelated Hail genomics package

