# Installed travel contract acceptance

Run `python -m pytest`. `test_rule_references.py` materializes declared skills and
startup rules in the host's native layout, resolves installed pointers and checks
both complete reference bodies against the accepted
[e23553a source](https://github.com/jbaruch/nanoclaw-travel/tree/e23553ac144d7491f2b2ac9c5733c57708f12229).
The fixtures retain the script-conversion clarification and expired rollout
removal from the [review correction](https://github.com/jbaruch/nanoclaw-travel/commit/e23553ac144d7491f2b2ac9c5733c57708f12229)
for [#384](https://github.com/jbaruch/nanoclaw-travel/issues/384).
`tests/` is excluded by `.tesslignore`; this plugin ships no evals.

After changing the schedule router, flight-assist read gate or either reference,
verify native-session behavior:

1. Run `tessl plugin pack --output /tmp/nanoclaw-travel-acceptance.tgz .`.
   Extract into a temporary directory outside the checkout. From the packed
   manifest, copy each complete declared skill directory to
   `/home/node/.claude/skills/tessl__<skill>/` and concatenate declared rules into
   `/home/node/.claude/.tessl/RULES.md`. This mirrors the host's
   [materialization contract](https://github.com/jbaruch/nanoclaw/blob/ef8cf4d6837427f8924deda83b7af00c215996fc/src/volume-mounts.ts#L715).
   Confirm the startup rules omit `operator-local-tz-phrasing`; admin owns the
   [shared timezone rule](https://github.com/jbaruch/nanoclaw-admin/blob/main/rules/surface-uses-operator-local-tz.md).
   Compare both installed reference bodies byte-for-byte with the accepted source.
2. Start fresh Claude Code sessions with those startup rules as context, only the
   native `Read` tool enabled, hooks disabled and external MCP servers disabled.
   Capture tool events with `claude --print --output-format stream-json --verbose`.
   On macOS, place `/home/node` beneath a temporary root and declare that single
   prefix translation in context. For the flight-assist session, load its installed
   `SKILL.md` as context. Keep reference suffixes and skill names intact.
   Keep transcripts outside the checkout; never preload reference text.
3. Request the workflow explanations below, without executing scripts or external
   queries. Each session must successfully read its complete installed reference
   through native `Read` before answering. A partial read, `@` import or unsupported
   claim of having read fails acceptance.

| Affected action | Required reference under `/home/node/.claude/skills/` | Required answer |
| --- | --- | --- |
| Compose flight-assist day-before wording with a supplied `day_label`; explain unavailable-zone handling and displayed flight times | `tessl__flight-assist/references/operator-local-tz-phrasing.md` | Render the supplied label verbatim; model conversion is forbidden; explicit dates on unavailable timezone; departure/arrival clocks retain airport-local offsets; core `current-tz` is sole zone authority. |
| Explain how admin `ask-my-data` reads `travel-schedule.json` with old-version records, uncertain freshness and a historical-flight question | `tessl__nightly-travel-sync/references/travel-schedule-contract.md` | Require integer version 5; never write/migrate; preserve coverage gaps; verify future mtime; sanitize untrusted text; cite source, UID, date and freshness; itinerary evidence never proves a flight was taken. |

4. Repeat each session with its reference missing. Require an observed native
   `Read` failure followed by stopping the affected action and reporting the
   unavailable contract. Restore references afterward.
5. Run the commands in `.github/workflows/test.yml` and `tessl plugin lint .`.
