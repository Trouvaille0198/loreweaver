# WORKFLOW — this repo is the baseline

> Recorded 2026-08-21 (user decision, highest priority). The running Docker
> image had drifted from this repo: the deploy-engine checkout carried its own
> local commit (`9f23f98`, protocol 2.4 character.skills) plus 18 uncommitted
> changes and a truncated shallow history, so the deployed code was not
> reproducible from source. Resolution: **the repo is the single source of
> truth; the docker is rebuilt from it.**

## Rule

1. **All engine code changes are made in this repo** (`~/repos/loreweaver`).
2. **Never edit code in the deploy directory** `~/apps/loreweaver`.
3. **Never `docker exec` into the container to edit files.**
4. After committing here, sync the deploy directory and rebuild:

```bash
cd ~/apps/loreweaver && git fetch ~/repos/loreweaver main && git reset --hard FETCH_HEAD
cd ~/apps/loreweaver-web && docker compose up --build -d
```

> Until this repo's `main` is pushed to GitHub, the deploy engine pulls from
> the repo path directly (above). Once `main` lives on GitHub, plain
> `git pull` in the deploy directory works again.

## Why

2026-08-21: the engine deploy checkout `~/apps/loreweaver` — the BuildKit
`engine` context that the docker image is built from — had diverged badly:

- local commit `9f23f98` (protocol 2.4 character.skills) existed only there,
  never pushed;
- 18 uncommitted file changes existed only there (hub/session/admin/iroh/tui
  server work and tests);
- its history was a shallow clone (depth 4), so it could not even be compared
  cleanly with the remote.

The image built from that tree could not be reproduced or traced from source.
The state was folded back into this repo as commit `20c6185`; the uncommitted
dev WIP (room_llm + misc) was snapshotted on branch `wip-room-llm`. The web
client has its own equivalent rule in the `loreweaver-web` repo's WORKFLOW.md.
