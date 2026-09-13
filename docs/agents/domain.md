# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists: it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo (most repos):

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

Multi-context repo (presence of `CONTEXT-MAP.md` at the root):

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## This repo

**single-context.** The glossary lives in [`CONTEXT.md`](../../CONTEXT.md) at the repo root — read it
for vocabulary; it holds nothing but terms. `docs/adr/` holds decisions that are hard to reverse,
surprising without context, and the result of a real trade-off (two so far: read-only logs +
sidecar; single-file no-build front end).

Two terms are easy to get wrong, so they are spelled out in the glossary — read them before
writing anything about lap timing:

- **beacon** is *one crossing of the line*, not the line itself; its position and its time are both
  attributes of it, not two different things.
- **lap** is *the interval between two crossings of the same beacon*; a geometric loop on a
  figure-of-eight is a **loop**, never a lap.

The code matches this as of the beacon merge: `laps.Beacon` carries `name` + optional `lat`/`lon` +
optional `time`, and `LapConfig.beacons` is the single list. `LapConfig.from_dict` still accepts the
pre-merge shapes (`gate`, `gates`, bare float times) so sidecars written earlier keep loading —
keep that compatibility until no such file can exist.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
