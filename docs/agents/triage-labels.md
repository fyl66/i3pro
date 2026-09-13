# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## Status in this repo

All five labels **already exist** on `fyl66/i3pro`, so no skill needs to create them:

```
needs-triage      #FBCA04  Maintainer needs to evaluate this issue
needs-info        #D4C5F9  Waiting on reporter for more information
ready-for-agent   #0E8A16  Fully specified, ready for an AFK agent
ready-for-human   #1D76DB  Requires human implementation
wontfix           #ffffff  (GitHub's stock label; meaning already matches)
```

Verify with `gh label list --limit 50`.
