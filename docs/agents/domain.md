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

**single-context.** `CONTEXT.md` and `docs/adr/` do not exist yet — that is expected and fine; `/domain-modeling` creates them the first time a term or a decision actually needs pinning down.

Vocabulary already load-bearing in this repo (do not drift to synonyms):

| Term | Means |
| --- | --- |
| **session / 场次** | one `.ld` log file, identified by its stem |
| **channel / 通道** | one logged signal; has unit, sample rate, decimals |
| **group / 分组** | channels sharing a unit, drawn on one shared y-axis |
| **lap / 圈** | an interval between two beacon crossings — never "a distance" |
| **beacon / 信标** | a start/finish point (lat, lon) that cuts laps; one beacon = one lap series |
| **run / 运行** | one attempt: sustained movement between two standstills (for 八字, acceleration, skidpad) |
| **component / 组件** | one panel on the worksheet (graph, scatter, track, gauge, delta, status) |
| **worksheet / 工作表** | the ordered grid of components, with its own shareable layout |

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
