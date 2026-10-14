---
name: report
description: Report a problem with context-gate itself to its maintainers. Drafts the report, checks it for private text, and opens the issue page for the user to submit. Use when the tool cost time. Never start it unprompted for the project's own bugs.
argument-hint: "<what went wrong>"
allowed-tools: Bash(python3 .context-gate/bin/*), Read, Edit, AskUserQuestion
---

# Report a fault in context-gate

The report goes to a public page. Say what the tool did wrong, and nothing about the project it
ran in.

## 1. What belongs

A fault in the engine, a skill, a role's text or a hook. The project's own bugs do not belong.

## 2. Draft

Run `python3 .context-gate/bin/govern report draft --title "<one line>"`. Fill its three
sections in general terms: the command, what it printed, what was expected, and the smallest way
to see it in an empty scratch project. Start from `$ARGUMENTS`.

## 3. What never goes in

The project's name, its code, file names and paths, people, customers, hosts, URLs, data,
anything from its documents. Replace each with a plain stand-in (`a phase branch`, `a test
file`). Output of `govern` itself may be quoted once the same things are taken out of it.

## 4. Check

Run `python3 .context-gate/bin/govern report check <file>`. Fix every line it names by
rewording. Never work around a hit.

## 5. Ask

Tell the user what the report is about in two lines, and that it goes to a public page anyone
can read. Ask with AskUserQuestion: open it, edit it, or keep the file only.

## 6. Send

Only on "open": `python3 .context-gate/bin/govern report send <file>`. It opens the filled-in
issue page in their browser; they read it there and submit it themselves. In a session with no
browser, run `python3 .context-gate/bin/govern report send <file> --print` and give them the
address. Never submit it for them by another route (`gh`, an API call, a browser tool). A
subagent never runs `send`: it writes the draft and names the file in its report.
