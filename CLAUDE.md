# Working rules

## Writing
- Plain, terse language. No emojis, no marketing tone.
- Keep within the length asked for; prioritize, most important first.
- Rank options by value against effort; size work as S/M/L.
- When asked for a review only, do not change code, commit or open PRs; deliver the report.

## Evidence
- Back every claim with evidence: file:line, command output or a screenshot. Run the thing; do not guess.
- Read the project docs (README, design notes, mockups) before the code.
- Treat the owner's assumptions and "out of scope" decisions as questions to answer honestly, not as givens.
- Look at screenshots yourself before reporting on them; send only the few that show the point.

## UI checks
- Screenshot the main pages and states: hover, selection, search, empty and error states.
- Light and dark themes, device scale 1 and 2, and a narrow viewport (about 1000 px).
- Write browser automation scripts to files, not inline shell.

## Environment
- Use the preinstalled browser; do not reinstall browser tooling.
- Stop servers by port (`fuser -k <port>/tcp`), never by pattern (`pkill -f`).
- Use synthetic or test data when the real backend is unreachable.

# From corrections in this session

## Commits and PRs
- Commit message: one short line in the language of previous messages; no body, no `Co-Authored-By`, no session links, no "Generated with" lines.
- PR: short title; body of terse bullets grouped under plain labels (Map, Search, Other); no headings, footers, links or emojis.
- After posting a PR or comment, read it back and remove anything a tool appended.
- Keep history linear: branch from the current default branch, no merge commits.
- One commit per development cycle; run the test suite before each commit.
- No breaking changes without asking: new settings optional, data formats only gain fields.
- Rewriting history or force-pushing needs explicit approval.

## Domain
- Do not adopt upstream conventions blindly: when a reference tool's semantics look wrong (e.g. container utilization), ask or follow the owner's definition.
