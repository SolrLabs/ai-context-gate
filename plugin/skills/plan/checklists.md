# Plan checklists

Check the plan against each line before handing it to context-gate's build skill; fix it where one fails.

- Phase 1 ends in something the owner can run or look at.
- A stated property (offline, ignored, read-only) names the test that proves it.
- A unit that touches keys or signing starts with the smallest real action. It never adds an identity to the login keychain.
- A step that starts an unattended agent session is an owner "Done when" line, not a unit.
- A screen unit cites its approved mockup as a tracked file.
- A unit's Tests line names the whole-tree tests its files reach.
- Units run side by side say what stays red until a sibling merges.
- The hand-over says which folder to start the build from, and that roles rendered in this session need a new one.
