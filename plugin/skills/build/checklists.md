# Brief self-check

Check each brief against these before it is committed.

- Each test it asks for is read against the task line it proves.
- A rule with a sign, an order or a position carries one worked example.
- The existing tests the unit will change are named, so the agent does not stop on them.
- Tests that must pass unedited are named as such.
- A test that checks a shape asserts the exact shape.
- A ratchet or a line limit names the files it covers.
- It points at no file under `.context-gate/build` that git ignores (a report is not in the next
  agent's worktree: copy what it needs into the brief).
- An environment variable a grandchild process must see is passed down to it by name.
- A process or window is checked on the running system, not in its code.
- A fix brief carries the project's rules for the files it touches.
