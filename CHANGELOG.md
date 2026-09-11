# Changelog

### flight-assist — the `day_before` label is precomputed against the operator's date (#300)

On 2026-09-10 at 12:03 CDT flight-assist sent "Tomorrow: DL1144 MSP→BNA" for a flight departing 19:50 CDT the same day. DL1144 was the post-misconnect rebook — the only flight newly added to byAir — so its fresh state fired `day_before` on the first poll at ~T-8h. Three things compounded. The payload shipped a hardcoded `hours_until_dep: 24` (the `DAY_BEFORE_HOURS` constant, emitted verbatim), a flat lie for any flight created inside the window. Same-day firing is normal, not an edge case: `check_day_before` fires on the first poll where `now ≥ dep − 24h`, so every rebook or late add inside the window fires at whatever T-minus it happens to be. And the Haiku composer, having run the core reader and read `America/Chicago`, skipped the date comparison the rule prescribes and pattern-matched the event name plus "24 hours away" — the run log says "The flight departs tomorrow at 19:50 CDT" with both dates Sep 10.

The label is now data, not date math for a small model. `check_day_before` emits the real whole `hours_until_dep` and a `day_label` (`today` / `tomorrow` / `YYYY-MM-DD`) with the `day_label_tz` it was resolved in, computed by the pure `phase_markers.day_label()` from the departure instant and the operator's current zone. The precheck reads that zone through `travel-core/operator_tz.py` (the door to the core `current-tz` reader) once per cycle at most, and lazily: the memo runs the reader only for a flight whose `day_before` has not fired yet, so a cycle with nothing to label never spawns it, and every flight in one cycle sees the same zone. The reader's timeout joins the byAir and Maps timeouts in the poll loop's reserved headroom (a flight started at the budget edge can be the one that pays for the spawn), which shortens the loop budget from 10s to 5s; `tests/test_flight_assist_precheck_budget.py` pins the relation (Copilot on #307). With no zone available — reader not installed, store unreadable, `available: false`, a name `ZoneInfo` cannot resolve — the label is the explicit date the RFC 3339 departure carries in its own airport-local offset, never the container's UTC date, and `day_label_tz` is `null`. The compose row renders `day_label` verbatim; `rules/operator-local-tz-phrasing.md` says so in one added bullet, and `references/event-payloads.md` documents the new fields. Tests pin the incident (12:03 CDT, 19:50 CDT departure → `today`, 7 hours), the UTC-date trap (22:00 CDT vs a 06:00 CDT departure the next morning: same UTC date, still `tomorrow`), the explicit-date fallback off the departure offset, and the once-per-cycle reader spawn across three flights.

The same notification's calendar heads-up framed a declined football practice as a conflict and read its times off a stale `+02:00` offset. Both are fixed predicates, so they moved out of the compose prose into a script (policy review on #307): `scripts/day-before-calendar.py <flight_id>` fetches the primary calendar across the flight's conflict window (`day_before_calendar.CONFLICT_MARGIN` either side of the effective departure and arrival, the instants the boarding block uses), drops events the operator declined or that were cancelled, and renders every time on the operator's clock through the same core reader, keeping each event's own offset only when no zone is available. The `day_before` row now summarizes that list and makes no filtering or timezone call of its own; calendar failures come back as a named `error` (`gateway`, `tier`, `calendar`) and the check goes out without the calendar part. `day_label()` also names an operator zone that does not resolve on stderr instead of falling back silently. Review rounds tightened three edges. An all-day event's `date` is read as a day in the operator's zone, never at UTC midnight, which had dropped a Sep 10 all-day event from a Sep 10 evening window in Chicago. A malformed persisted departure or arrival returns `{"error": "state"}` instead of a traceback. The payload's `tz` names only a zone that actually resolved. And the precheck asks `phase_markers.day_before_due` before spawning the zone reader, so a first-seen flight more than a day out never pays for it. `travel-core/operator_tz.py` picked up the advisories deferred here from #305. Every unavailable message now says what to check. A reader killed at its timeout still has its partial stderr relayed. A payload whose zone does not resolve, whose `local_now` is naive, or whose `local_date` is not an ISO date reads as unavailable. The stale-offset artifact itself is the drive-engine half of #301.

### travel-core — `operator_tz.py`, the Python door to the core zone reader (groundwork for #300, #301)

Two prechecks need the operator's current zone as data rather than as a phrase for an agent to look up: flight-assist's `day_before` day label (#300) and the drive engine's meeting-drive display zone (#301). The one reader of the host's `tz_state` store lives in `jbaruch/nanoclaw-core` since 0.2.130 (`skills/current-tz/scripts/read-current-tz.py`), and no script here may open the store itself — that is the drift the consolidation removed. `operator_tz.read_operator_tz` spawns the reader at its runtime mount, relays the reader's own stderr verbatim, and returns the `{tz, local_now, local_date}` triple as an `OperatorTz`. Every unavailable outcome is `None` with its own stderr line — reader not installed, spawn failure or timeout, store unreadable (exit 1), `available: false`, output the caller cannot parse. Exit 2 (CLI misuse) degrades the same way instead of raising: a day label falls back to an explicit date and a drive block to the event's own zone, rather than a whole precheck cycle going dark under the outer-boundary catch. `now=` passes through as the reader's `--now` so a caller pins the instant the local fields describe; a naive instant is rejected before spawning, matching the reader's rule. No consumer is wired in this change; #300 and #301 wire theirs.

## 0.2.130 — 2026-09-09

### flight-assist — `read-current-tz.py` moves to `nanoclaw-core` (`jbaruch/nanoclaw#951` follow-up)

The operator-zone reader existed here and in `nanoclaw-admin/skills/scheduler-timezone`, with different contracts, and the two had drifted. `nanoclaw-core@0.1.143` hosts the single copy under `skills/current-tz` with this plugin's no-guess contract, extended on review: `{"available": true, "tz", "local_now", "local_date"}` or the all-null `available: false` shape, exit 1 only when the store itself cannot be read, `home_tz` never a fallback. `rules/operator-local-tz-phrasing.md` names the reader by its repo-relative path and owning plugin and reads `local_date` from its output (for an event, the script run with `--now <scheduled_dep_time>`) instead of converting instants inline; the flight-assist notification step carries the runtime mount path `/home/node/.claude/skills/tessl__current-tz/scripts/read-current-tz.py`; the README points at the core skill. The local script, its tests and the `tz_state` seeding fixture are gone. Core is installed in every tier, so the overlay's independence from `nanoclaw-admin` is unchanged.

## 0.2.127 — 2026-08-28

### drive-engine — the cadence sweep stops waking to relay its own notice (`#285`)

The sweep rendered the operator notice deterministically and then woke a Haiku agent whose only instruction was to send it unchanged. SKILL.md Step 1 was explicit — "Send `data.message` verbatim … do not rewrite, summarize, add to it" — and the wake container composed its own text anyway. The template reads "Added a drive for {meeting} at {when} — reply 'skip' if you're not driving to it"; what reached the operator was "Drive engine synced. Added: … Driving to it, or skip?". Drive-by-default became a coin flip, and neither rewritten phrase appears anywhere in the skill.

`jbaruch/nanoclaw` 1.2.165 added the delivery path this needs: a precheck returning `wake_agent: false` can carry a `data.message`, and the host sends it byte-for-byte without spawning anything. `build_sweep_payload` now returns `wake_agent: False` unconditionally. The notice still renders exactly as before; no model sees it, so none can rewrite it.

Silent sweeps are unchanged — `message` is `None` and the host's delivery branch requires a non-empty string, so the outcome matches what the old wake gate produced. A notice fire now costs zero model tokens instead of a Haiku spawn.

The relay step is gone from SKILL.md, and the remaining steps renumber 2–4 → 1–3. The `description:` frontmatter moves with it — it still promised the sweep would wake you, which is the discovery surface an agent reads first, and the repo's 1024-character limit test caught the first rewrite at 1033. `build_sweep_payload`'s docstring had the same drift: its opening still described the old wake gate while a later paragraph described the new one, and SKILL.md names that docstring authoritative. Putting the description under review also surfaced a pre-existing gap — an action router must list every action it offers, and Step 3's (report a drive block that looks wrong or missing) had never been there. The opening prose was tightened to make room inside the 1024-character limit. `check-travel-bookings`' cross-reference to the drive-or-fly step moved from Step 3 to Step 2 in lock-step. The operator's `skip` / `drive` / `fly` replies are inbound-triggered and wake normally, untouched.

Five tests asserted `wake_agent is True` to mean "the sweep has something to say". That is now expressed by `data.message` being present, so they assert the message and the no-wake invariant rather than flipping to an assertion that would pass trivially; two whose names described the wake are renamed.

## 0.2.126 — 2026-08-28

### README — state the `tessl.json` facts, drop the attached rationale (`#281`)

Three spots in the `## Dev toolchain (tessl.json)` section attached why-content with a colon or an em dash. `coding-policy: context-writing-style` forbids that in auto-loaded artifacts, and the README loads on plugin fetch.

The manifest paragraph's colon introduced an explanation of how `tessl.json` stays out of the published plugin; it is now five direct sentences. `coding-policy`'s renewal cell led with "Floats by design —" before naming the carve-out; it now names the carve-out first. `finsi/codex-review`'s cell explained *why* no scanner covers the manifest across an em dash; the quarterly cadence and `tessl outdated` stay, and Renovate's lack of a Tessl datasource is its own sentence.

Deferred from `#280` as advisory, folded in here rather than spending a round on prose alone.

## 0.2.125 — 2026-08-28

### nightly-travel-sync — decode ICS escapes in `summary` too (schema v5, `#278`)

`refresh-travel-schedule.py` decoded RFC 5545 TEXT escapes in `LOCATION` at v4 (`#275`) but still wrote `SUMMARY` verbatim, so the live feed's escaped commas rode straight into operator-visible text: `Check-in: Radisson Blu Airport Hotel\, Oslo` is what the booking brief and the drive-engine diagnostics printed.

`#278` did not prescribe a fix, because `summary` is load-bearing in a way `location` is not — `trip_key` derives the slug that keys `travel-db.json` and drive-engine's per-trip verdict store from it, so a decode that moved the slug would orphan stored decisions. The issue asked for that to be confirmed before choosing decode-at-writer over decode-at-display.

It is confirmed, and it resolves in favour of the writer. `trip_key` slugifies through `_NON_SLUG_RE = [^a-z0-9]+`, which collapses a backslash and a comma into the same `-` — so `check-in-radisson-blu-airport-hotel-oslo-2026-08` comes out byte-identical either way and no verdict is orphaned. `lodging_pair_key` parses a stay's check-in and check-out from summaries decoded the same way, so the two halves still pair. `flight_summaries` reads IATA designators, which carry no TEXT escapes at all. Each of the three is now pinned by a test rather than argued.

Schema bumps 4 → 5, mirroring the v3 → v4 shape. The schedule is regenerated in full each run, so there is no stored state to migrate; `travel-core`'s `SCHEDULE_SCHEMA_VERSION` moves in lock-step, and a v4 reader parses a v5 record identically because the decode only removes characters no reader ever matched on.

`description` deliberately stays verbatim: `tripit_local_time` and drive-engine's route reader parse it escapes and all, so decoding it would break them. The test that used to assert both fields stayed raw now asserts the split.

## 0.2.124 — 2026-08-28

### drive-engine — distrust a `timeZone` that contradicts its own `dateTime` offset (`#284`)

A meeting-drive notice announced a 09:00 PDT San Francisco event as **16:00**. The event carried `"dateTime": "2026-08-22T09:00:00-07:00"` with `"timeZone": "UTC"` — a Luma/Partiful-style import that wrote a Pacific wall-clock and stamped the wrong zone on it.

`_extract_timezone` took `start.timeZone` verbatim, so `DesiredBlock.timezone` became `"UTC"` and `_start_in_local` rendered the instant in UTC. The unresolvable-zone fallback that exists for exactly this shape never tripped, because `"UTC"` is a perfectly valid IANA name — the fallback was built for a *missing or invalid* zone, and this one is valid-but-wrong. It rendered faithfully in the wrong zone.

The declaration and the offset describe the same instant twice, so a disagreement between them is self-evident: `_tz_is_trustworthy` resolves the declared name at that instant and compares its real UTC offset against the one the `dateTime` carries. On a mismatch the zone is derived from the offset instead (`Etc/GMT±N`, the same fallback the no-`timeZone` path already uses). Comparing at the instant rather than against a fixed offset means a correct name survives a DST boundary — `America/Chicago` is kept at both `-06:00` in January and `-05:00` in July.

A declared name the check cannot *apply* is treated the same as one that contradicts. Alongside an unresolvable zone that covers `OverflowError`: `astimezone` on a boundary instant such as `0001-01-01T00:00:00+14:00` walks past `datetime.min`, and `_parse_event`'s contract is that one malformed event never aborts a wide-window sweep, so it is handled rather than allowed to propagate. Caught in review.

An unresolvable declared name is treated the same as a contradicting one. The first draft kept it, reasoning that a check which could not run is not evidence of a contradiction — but `_start_in_local` cannot resolve it either and falls back to UTC, which reproduces the exact wrong-time notice. Review caught it; `coding-policy: error-handling` Graceful Fallback says try the available alternative before failing, and the offset is available.

The declared name survives in exactly two cases: it agrees with its offset (the overwhelmingly common case), or it is untrustworthy but the offset is not a whole hour so no `Etc/GMT±N` maps — a wrong name still beats no zone at all.

Verified end to end on the incident's instant: `Sat Aug 22, 16:00` becomes `Sat Aug 22, 09:00`.

The regression suite drives the public `scan()` API and asserts the `timezone` it puts on the returned `MeetingClass` — the value `calendar_apply` renders the notice from, so an outcome rather than an internal. An earlier draft asserted the private `_extract_timezone` directly, which `coding-policy: testing-standards` rejects: an internal refactor would have broken those tests without changing behaviour. The render step stays `calendar_apply`'s to cover; reaching into `_start_in_local` from here would have traded one private-helper assertion for another.

Not fixed here: `#285`, the notice-rewrite bug that co-surfaced in the same message. That one needs a delivery path that does not pass the rendered string through a model, and no such path exists in this plugin — every skill here routes chat through the agent's `mcp__nanoclaw__send_message`. It is host work.

## 0.2.123 — 2026-08-28

### check-travel-bookings — stop nagging about a trip that has already started (`#286`)

The Sunday surface flagged "Onboarding CA 2026 (Aug 17–24) — ничего не забукано" on 2026-08-23. The traveller had been in San Francisco for six days and flew home the next day; the bookings were made out of band and never reached TripIt. There was nothing left to book, so the prompt was noise.

`#120` had already established that an elapsed night is un-bookable and floored the night scan at `today` — but only inside the `has_transport` branch. The `if not items:` early return above it never looked at `today` at all, so an empty itinerary surfaced as `is_empty` whether the trip was future, in progress, or ending tomorrow.

The classifier now also returns `has_bookable_window` (`today < trip_start`), and the empty-itinerary alert gates on it. `is_empty` deliberately keeps its meaning: the trip really does have zero items, and collapsing a departed trip into "not empty" would make the flag lie about the itinerary to express a fact about the calendar. Two separate questions, two separate fields — and the `today`-dependent one stays inside the pure, injected-`today` classifier where `#120`'s flooring already lives, rather than leaking a date comparison into the alert-assembly block.

The window closes at departure, not arrival home: a trip starting today is already too late to book for. `#271`'s home-metro suppression and the away-trip signal are untouched — a FUTURE empty away-trip still fires, which is what this check exists for.

Five tests, three on the classifier and two end to end. The end-to-end pair was confirmed to discriminate: with the alert gate reverted, both fail with the "ничего не забукано" gap present.

## 0.2.122 — 2026-08-18

### Fixed — align the manifest with the published 0.2.121 (`jbaruch/nanoclaw-travel#282` publish recovery)

The #282 publish run shipped `0.2.121` to the registry, then `smart-publish`'s manifest bump-push to `main` was rejected by a GitHub backend error — `remote: fatal error in commit_refs`, ten seconds after the publish call returned. The action's diagnostic guesses a protected branch; this repo has neither branch protection nor a ruleset, and the `stamp-changelog` push from the same job and the same token landed seconds earlier, so the rejection was transient server-side.

The registry and `CHANGELOG.md` both carry `0.2.121`; only `.tessl-plugin/plugin.json` was left at `0.2.120`. Re-running the publish would have auto-bumped to `0.2.122` and cut a second release of identical content, so this lands the bump commit the run could not push.

## 0.2.121 — 2026-08-17

### Chore — the manifest declares `"mode": "managed"`

#280 committed `tessl.json` and corrected the ignore rules around it, leaving the manifest's own contents as the untracked copy had them: `"mode": "vendored"`. `jbaruch/nanoclaw-host: tessl-version-floating` requires `"mode": "managed"` on every NanoClaw manifest. Specifiers were already correct — `jbaruch/coding-policy` floats at `latest`, `finsi/codex-review` pins with the quarterly cadence README records. The `.tesslignore` pattern is anchored to the repo root so it cannot match a nested file of the same name.

## 0.2.119 — 2026-08-17

### Fixed — every drive anchor was geocoding a backslash

`San Francisco\, CA`. That is what `travel-schedule.json` has carried in `location` since the file existed, and what `trip_origin.resolve_anchor` has been handing out as a drivable address ever since #122 made it one.

RFC 5545 escapes commas in TEXT values, so TripIt sends `San Francisco\, CA` and `refresh-travel-schedule.py` wrote it through verbatim. Nobody noticed because Google's geocoder is forgiving — 86 of the 104 anchor probes over the live feed carried a stray backslash into the routing request and resolved anyway. It is the kind of defect that stays invisible until an upstream tightens its parser, and then every mid-trip drive leg anchors somewhere wrong on the same afternoon.

The writer decodes now (`\,` → `,`, `\;` → `;`, `\\` → `\`, `\n`/`\N` → newline), one pass over the string so `\\,` reads as a literal backslash then a comma rather than being re-read as an escaped comma. #274 had already written exactly this helper downstream in `build-travel-db.py` for the new `destination` field; it only lived there because that field was new. It has moved to the writer and the downstream copy is gone — a second pass would eat the backslash out of an address that legitimately carries one.

Record schema goes to v4 and `travel-core/trip_origin.py`'s gate goes with it. The bump is bookkeeping rather than a compatibility event: nothing downstream ever parsed the escapes, so a v3 reader reads a v4 record identically, and the one cross-plugin reader (`nanoclaw-admin`'s `morning-brief-cfp.py`) does not gate on the version at all. Writer and gating reader ship in this plugin and update together.

Regression pass on the real thing rather than fixtures: the live schedule replayed through `resolve_anchor` at 09:00 and 19:00 UTC on every day it covers. Zero source drift — every anchor still resolves from the same record under the same rule (home / lodging / trip_location) — and zero backslashes left in any resolved address.

`summary` still ships verbatim, escapes and all. It looks like the same one-line fix and is not: `trip_key` derives the `travel-db.json` trip slug from it, drive-engine keys per-trip decision state by that slug, and `lodging_pair_key` pairs check-ins to check-outs by a hotel name parsed out of it. Changing that string is a state-key question, not a text-escaping one. Filed as #278.

## 0.2.118 — 2026-08-17

### Fixed — a blank address line read the next line as its value

`- current_home:` with nothing after it did not read as empty. It read as `- home_airport: BNA`.

The key-matching regex used `\s*` around the colon, and `\s` includes the newline. With no value on its own line, the match walked to the next line and took whatever was there — so the address every home-anchored drive routes from could have been an IATA code, silently, and `home_address.py` would have returned it rather than raising the "no `current_home:` entry" error that exists precisely for this.

The bug is older than the shared module: the same pattern shipped in `home_address.py` since the Epic #59 block landed, and 0.2.116 inherited it into `travel-core/addresses.py` where `home_metro` picked it up too. It only bites when a key is blank AND another line follows, which is why a suite that tested a blank key at end-of-block went green on it.

Every gap in the pattern is now horizontal whitespace. Regressions pin both sides: a blank `home_metro` reads as unset, and a blank `current_home` raises.

## 0.2.116 — 2026-08-17

### Fixed — the brief nagging about a surgery

`Alice's surgery`, September 16–17, Nashville. The travel-bookings brief listed it under "ничего не забукано" — nothing booked. Correct, in the sense that nothing was booked. Also useless: it is a placeholder trip filed in TripIt so byAir, drive-engine, and cfp-conflict-check all see the day is taken. There is no flight to book to the city you live in.

The check had no way to tell. `classify_trip` saw `"days": {}`, called it `is_empty`, and fired — which is exactly right for a Devoxx trip three weeks out with nothing on it. The empty itinerary is the same signal in both cases; the destination is what separates them, and the destination was nowhere in `travel-db.json`.

The issue (#271) assumed the destination would have to come from the tripit-api service, since nothing local carried it. It turned out `travel-schedule.json` has had it the whole time — `refresh-travel-schedule.py` reads the trip wrapper's `LOCATION` off the iCal feed and writes it out as `Nashville\, TN`. `build-travel-db.py` was dropping it on the floor. So the fix is a field the builder already had in hand: `travel-db.json` v3 carries an optional `destination` per trip, ICS escapes unwound, written only when the feed labels the trip.

`check-travel-bookings.py` then skips a trip whose destination is the operator's home metro, in the same breath as the existing skip-past-trips guard, and counts it under a new `local_trips` in the output rather than folding it into `complete_trips` — nothing about it was checked, and an operator asking "why didn't it flag that one?" deserves the answer somewhere.

The home metro is config, not a string constant: `- home_metro: Nashville, TN` in the trusted profile's canonical `## Addresses` block, next to the `home_airport` that has lived there since Epic #59. An unset key means every trip gets checked, which is the behaviour that predates this change — so the reader is dual-accept from birth and the writer-side schema bump in `nanoclaw-trusted` can land after this ships, per `stateful-artifacts` Cross-Pipeline Schema Bumps. Matching is exact equality on the normalized label (casefolded, whitespace collapsed), never a substring test: `East Nashville, TN 37206` is not home, and neither is a blank destination. An unlabelled trip is a trip whose destination we do not know, and reading unknown as home would silence the check for precisely the trips it exists to watch.

That block's parse now lives in `skills/travel-core/addresses.py`, shared with `skills/drive-engine/home_address.py` rather than copied beside it. The two consumers disagree about what absence means, which is the interesting half: a missing `current_home` still raises, because a guessed drive origin mis-times every leg, while a missing `home_metro` just means suppress nothing. Both now gate on the block's own `schema_version` — the first draft did not, on the theory that reading keys by name is inherently version-agnostic, which is a nice theory and not what `stateful-artifacts` says. A block stamped past what the reader knows is no usable prior state: the booking check reads no home metro and checks every trip, the drive reader refuses outright.

The v3 bump rides through the lock-step readers — `check-travel-bookings.py` and `flight-assist/trip_window.py` accept `{1, 2, 3}`, and `nightly-travel-sync/precheck.py` expects 3 so the next nightly fire rebuilds rather than waiting for the DB to age out (the #268 lesson, applied on purpose this time).

### Changed — one directive per bullet in the travel-history section

The advisory findings from the policy review of #272, folded into a round that was already happening (#273). Three bullets in `flight-data-locality`'s `Travel Already Booked or Flown` section carried more than one directive each: how `using-tripit` reaches the service was bundled with the overlay-tile loading and the never-vendor rule; the iCal feed's ~90-day limitation was bundled with its input contract for two artifacts. Both split, meaning unchanged. The third dropped "is the failure this section names" — incident framing belongs in the entry above, not in a rule that loads on every turn — and keeps the routing directive.

## 0.2.115 — 2026-08-16

### Added — the account this plugin has been reading through a keyhole

TripIt holds every trip Baruch has ever taken, with confirmation numbers, costs, and the hotel he stayed at in Tel Aviv in April. This plugin has been reading it through a `.ics` feed that carries a rolling ~90-day window and no PNRs. On 2026-08-15 a "where did we stay last time in Israel" question ended with an agent hand-parsing that `.ics` — the trip had aged out five days earlier, and the answer was a guess dressed as a lookup.

`jbaruch/tripit-api` is a read-only REST + MCP service over the full account, and its `using-tripit` skill is now the source-of-record for travel already booked or flown: `flight-data-locality` says so, and a second history source is forbidden on the same terms as a second flight-data API. The skill loads as a co-loaded overlay tile next to this plugin. It is not vendored here — it has its own release train, its own tests, and behaviour that keeps moving (per-endpoint scope defaults, never-retry-a-503, the truncation contract). A copy in this repo would be a second source of truth for all of it.

Nothing here changes what the iCal feed does. It is the upcoming window that feeds `travel-schedule.json` and `travel-db.json`, and the nightly sync still runs on it. What it stops being is the thing an agent reaches for when the question is about the past.

Plumbing, in the order it landed: nanoclaw#921 forwards `TRIPIT_API_URL` and `TRIPIT_API_TOKEN` to agent spawns (the bearer stays in the OneCLI vault — the container holds `onecli-managed` and the gateway swaps it on the wire); jbaruch/tripit-api#40 puts the service on the gateway's docker network so nothing is published on the host; jbaruch/tripit-api#41 fixed the skill's script paths, which resolved only in a repo checkout and would have sent every agent command to ENOENT; nanoclaw#924 installs the tile in the orchestrator registry.

## 0.2.114 — 2026-08-13

### Fixed — a shipped fix that sat there for a day and a half doing nothing

#267 landed on the 12th. The morning brief on the 13th still reported the same eight trips needing bookings, four of which had been false the whole time. The code was correct, deployed, and completely inert.

`nightly-travel-sync`'s precheck decides whether to wake the bundle by looking at exactly one thing: how old `travel-db.json` is. The DB was built at 11:01Z on the 12th, three and a half hours before the fix merged. At the 06:01 fire on the 13th it was 24 hours old, comfortably under the 60-hour cap, so the precheck did what it was told and skipped. The rebuild that would have activated the fix was scheduled for the 14th at 23:01Z. A day and a half of a brief confidently reporting numbers we had already fixed.

The precheck knew the file's age and nothing about its contents. Fresh by mtime, stale by schema — the two are independent, and only one was being checked. It now reads the DB's `schema_version` and wakes when it sits below the version `build-travel-db.py` emits, so a schema-bumping fix activates on the next fire instead of idling until the file happens to age out. An unreadable, unstamped, or non-object DB reads the same way: it cannot be at the current schema, so rebuild it.

A DB stamped *above* the precheck's constant is the one case that does not wake. There the precheck is the lagging side, and waking would only drive the builder into its refuse-to-downgrade guard; the age cap governs instead.

The constant is mirrored rather than imported — the precheck runs host-side on the cadence-registry, where the builder's plugin mount is not on the path, and it is stdlib-only by contract. A test asserts the mirror against `build-travel-db.py`'s own `SCHEMA_VERSION`, so drift fails CI rather than surfacing as another quiet day of stale data.

### Changed — one directive per bullet in the expertflyer seat sweep

Step 4's `cabin_openings` reporting bullet was carrying three orders at once: name the cabin and seat, state the transaction it needs, and don't call it a seat selection. Now three bullets, same words, same meaning. Advisory finding deferred from #264 rather than burning a re-review round on it alone; folded in here per the boy-scout rule.

### Changed — travel data refreshes daily instead of every third day

The 60-hour cap encoded an every-third-day refresh. `morning-brief` reads `travel-db.json` every morning, so travel gaps could lag reality by up to two and a half days — long enough for a hotel you already booked to keep getting flagged for two more briefs.

Now 20 hours. Not 24, for the same reason it was never 72: the DB stamps at run completion, so a cap on the exact cron multiple near-misses. The next daily fire finds the file 23.9 hours old, reads it as fresh, skips, and the daily refresh quietly becomes every-other-day — jbaruch/nanoclaw#803, which the 60-under-72 cap existed to dodge. 20 leaves margin for run latency and DST.

## 0.2.113 — 2026-08-12

### Fixed — a night spent on a red-eye home was billed as a missing hotel

The gap check told the operator they had no hotel for Aug 22. They were on a plane: the Residence Inn checked out at noon, and WN1683 left San Francisco at 11:05 PM that night and landed in Nashville the next morning.

TripIt's ICS feed stamps every timed event in UTC and never emits a `TZID`, so an 11:05 PM PDT departure arrives as `20260823T060500Z`. Every consumer that sliced the date off that instant filed the departure on the 23rd. The 22nd was then a night with no flight and no hotel — a gap, by a rule that was reading the wrong calendar. Eight of the 64 events in the live schedule shift date this way, so this was never one unlucky booking.

The local clock was never actually lost. `DESCRIPTION` renders the itinerary the way TripIt displays it, and prints the local time for both halves of a segment. A printed wall clock plus a known UTC instant determines the UTC offset: the two clocks differ by the offset modulo 24 hours, and one candidate falls inside the inhabited range. No zone database, no airport-to-zone table, no network call — `skills/nightly-travel-sync/scripts/tripit_local_time.py` does the arithmetic and resolved all 64 live records with no ambiguity.

Reconstruction fails closed. A record whose clock is missing, unparseable, or genuinely ambiguous gets no local stamp and its readers stay on the UTC date they already used. The one ambiguous band is real: the inhabited offsets span 26 hours, so a clock 11 hours behind UTC is equally consistent with +13:00 a day over. Two candidates is a refusal rather than a coin flip, because a guessed local date moves a night while a missing one changes nothing.

Neither half of a record inherits the other's offset. A segment landing in another zone prints its own arrival clock, which is that half's authority. A single-location record — lodging, a car rental — gets a start stamp and no end stamp: TripIt pads those to a synthetic one-hour DTEND it renders nowhere, so no printed clock stands behind the end, and carrying the start's offset across it would assert an offset a DST transition inside the span could have changed. One stay is two records anyway, check-in and check-out, each printing its own clock.

A second false alarm fell out of the same root cause. A San Francisco turnaround that flies out in the morning and takes the red-eye straight back spends no night on the ground, but its arrival lands inside the trip window rather than past it, and the old "still in transit at the end" test read that as landed-and-staying-over. The overnight span is what marks a red-eye home, not the arrival date. A night the scan already found uncovered now overrides that test either way — how a trip ends says nothing about the nights in the middle of it.

Both alerts were false and both are gone; the four real gaps in the live schedule report unchanged.

### Changed — `travel-schedule.json` v3 and `travel-db.json` v2 carry the traveller's clock

Both artifacts gain optional `start_local` / `end_local` stamps beside the UTC instants they already carried, and `travel-db.json`'s day key follows the local date where one exists. The UTC fields are untouched, so the bumps are additive and every reader that compares instants is unaffected.

Readers accept both versions for the rollout window: the files on disk stay at the old version until the next nightly rebuild, and a single-version reader would have failed open (`trip_window.py`) or hard-errored the whole brief (`check-travel-bookings.py`) on every cycle in between. Verified against the live schedule — the patched code on an unrebuilt DB reproduces today's output exactly.

`check-travel-bookings/state-schema.md` claimed a bump here needed lock-step with the host pre-spawn gate in jbaruch/nanoclaw. It does not: `src/host-plugins/flight-assist-spawn-gate.ts` reads trip-level `start`/`end` and never `schema_version`, so a version bump is invisible to it. A trip-level shape change is what would need coordinating there.

## 0.2.112 — 2026-08-10

### Changed — the held seat's cabin is resolved, not asked for

byAir stores the seat and no cabin the assessment can use. Its `seat_class` is business, premium economy or economy, so Comfort+ and Main Cabin are both economy — the distinction the whole assessment turns on, and the one that had seat 21F judged against the wrong cabin.

Asking the operator for it was the obvious repair and the wrong one. The cabin is a fact about the aircraft, and `rows` from `/seats` (jbaruch/expertflyer-api#20) already states it: every row of a cabin, sold out or not. `--held-cabin` is now optional, and omitting it resolves the cabin by reading from the bottom of the ladder up. Most seats are in the Main Cabin, so the common case costs one request, and the sweep reuses every response the resolution fetched.

`held_cabin_from` reports `stated` or `resolved`, separately from `held_cabin_corroborated`, which reports how it was checked.

A cabin boundary can fall mid-row — the 739's Comfort+ ends a row later on the right, which this repo already documented — so one row number belongs to two adjacent cabins. Resolution reads past the first match into the neighbour above to tell them apart; taking the first would hand a Comfort+ seat to the Main Cabin. Nothing further up the ladder can share a row, so the check costs one response the sweep usually fetches anyway.

`held_cabin_unresolved` carries a `reason`, because the three ways resolution fails send the operator somewhere different: `shared_row` asks which cabin, `no_such_row` questions the seat and the flight, `rows_unavailable` means the service cannot answer. A service that reports no rows is a capability gap rather than an unusable argument, so it is an unanswered verdict rather than a `bad_request` that would have sent the agent into access diagnosis on a service that answered fine.

A neighbour that did not answer is not evidence the row is unshared. An errored fetch, or one from a service that reports no rows, stops the resolution and says so rather than assigning the seat to the lower cabin off a failure — which at `--scan-up 0` the sweep would never fetch again to notice. A cabin the aircraft lacks is a real answer and does rule the split out.

A row two cabins share, or a row no cabin on the aircraft holds, returns `held_cabin_unresolved` rather than a verdict. The seat or the flight is wrong, and assessing it against a guessed cabin is what this replaces. A service without `rows` cannot resolve, and says so rather than guessing.

### Fixed — a seat in a better cabin was reported as one to go and take

Found on the second live trip sweep, which told the operator their Comfort+ seat was beaten by 2D in First. It is not a seat they can select: moving cabins is a fare change or an upgrade clearance, and the airline's app offers no button for it. The reply was actionable-sounding and unactionable.

`is_upgrade` ranks a better cabin above the held seat, which is right — that is the ladder #254 fixed. What was wrong is `assess` presenting the result as an upgrade to take. `upgrades` now holds seats in the held cabin only, which are the ones the operator can select; a better cabin's seats go to `cabin_openings`, and the skill says explicitly that taking one is Step 1's fare-class question rather than a seat change.

`optimal` follows from that: nothing the operator can select beats the held seat. A Comfort+ window opening while they sit in Main is still reported, as an opening rather than as a seat to go and take.

### Fixed — the alert offer followed the sweep instead of the operator

The same sweep offered an alert on First. Step 3 said to offer on `cabins_scanned`, and the sweep had been widened with `--scan-up`, so the offer widened with it — a watch on a cabin the operator does not move into.

`alert_cabins` is now its own answer: the held cabin and one rung up, whatever the sweep read. Widening `--scan-up` sees further and changes nothing about what is worth watching.

It also honours the skill's founding rule. Check first, alert only if absent — a cabin already holding a seat worth taking has nothing to wait for, and a watch on it fires the moment it is created. Those cabins drop out, so on a flight where the held cabin has seats and Comfort+ is sold out, the offer is Comfort+ alone. That is what the operator asked for, arrived at from the check-first rule rather than a special case.

### Fixed — the trip report explained the verdict wrongly, and never offered the alert

Both found in the first live trip sweep, which returned correct verdicts and then described one of them backwards.

The report said 21F "beat even Comfort+". It did not. Comfort+ outranks a Main Cabin exit row — the ladder that #254 fixed says so — and 21F won only because nothing acceptable was open in Comfort+. Right answer, invented reason, and the reason is what an operator acts on next time.

The output gave `upgrades: []` and no way to tell an empty cabin from a beaten one, so the gap was there to fill. `acceptable_by_cabin` now reports, per cabin, how many open seats were worth taking. `0` means a cabin that had nothing, not a cabin that lost. Step 3 states the distinction and forbids the claim: `optimal` compares the held seat against seats that were open, never against a cabin's standing.

Step 4 never offered the alert. Step 3's `optimal` rules require it, but the per-flight reporting list in Step 4 omitted it entirely, so a sweep reported four seats as fine and offered nothing to watch. The trip report now makes one alert offer covering every flight whose `alert_recommended` is true — a seat that is the best of nothing open is exactly the seat worth watching.

### Fixed — the seat pass covered every upcoming flight, not the trip asked about

`upcoming-flights.py` had a lower bound and no upper one, so "make sure I have the best seats" was a work list of every flight on the schedule. Today that is 37 flights: at a request per cabin each, against a service that answers `blocked` when its bot wall trips, an hour of browser-driven traffic to answer a question about tomorrow.

The pass now covers the next trip, which is what the question means. The schedule already carries `Trip` records with their windows, so the bound is the operator's own itinerary rather than an invented day count. `--trips N` widens it and `--trips 0` restores the old everything.

Trip windows are date-only while a departure is a UTC instant, so a return leaving late in the local evening lands on the next UTC day and falls outside a trip that ended the evening before. The window covers its end date's whole day plus a day of slack each side, which absorbs that without reaching a trip separated by more.

Whether a trip is over is decided on its real end, not the slack-expanded window. The slack exists to match a late-evening departure to its own trip; reusing it for selection kept a finished trip eligible for another day, so the default limit would pick it over the real next trip — nothing to check, and tomorrow's trip reported as excluded.

A negative `--trips` is rejected rather than read as the unbounded mode. `limit <= 0` treated `-1` as "every flight", so a typo turned a four-flight pass into the whole schedule against the service the bound exists to protect. Only zero opens it.

Excluded flights are reported, never silently absent. A caller reading `flights` as "everything upcoming" would tell the operator their seats are fine on a trip it never looked at — the same shape of unearned confidence the assess verdicts were fixed for.

### Added — the held seat's cabin is now checked, not assumed

`expertflyer-api` #20 landed `rows` on `/seats`: every row of the cabin holding a real seat, sold out or not. That settles a question this plugin could previously only guess at.

The held seat is occupied, so it appears in no seat list, and `--held-cabin` was taken on faith. The cabin decides the ladder rung and scopes the exit-row layout, so a wrong one corrupts every part of the verdict. On DL2957 seat 21F was assessed as Comfort+ while it sits in the Main Cabin, an exit row that reclines.

`assess` now reads the held cabin's own `rows`. A held row outside that extent returns `held_cabin_mismatch` naming the range and, when the sweep saw it, the cabin the row belongs to. `held_cabin_corroborated` becomes a real three-state answer and `held_cabin_source` says which evidence settled it.

The earlier seat-derived check remains for a service without `rows`, and keeps its old limit: it confirms and never disproves, because a row whose every seat is taken is missing from the cabin it is in. Sold-out cabins were the case it could never settle, and the case `rows` answers outright.

### Fixed — `optimal` off an empty evidence base, and a held cabin nothing checks

Both found on the first live run against the deployed service, and both let `assess` report a confident verdict it had not earned.

`assess` reported `optimal` after observing **zero** open seats. Comfort+ was sold out and Premium Select is not on the aircraft, so the sweep compared the held seat against nothing and said nothing beat it. True the way "no counterexample was found" is true after looking in no drawers, and it reads as a comparison that happened. A sweep that saw no open seat now returns `nothing_open` with `seats_compared: 0`, exits non-zero, and carries no `upgrades` or `alert_recommended`.

`--held-cabin` was taken on faith and still largely is, but the sweep now says how much it could confirm. `held_cabin_corroborated` is `true` when the held seat's row turned up in the cabin it was assessed as, and `null` otherwise; `row_seen_in` names the scanned cabins where the row did appear.

It is never `false`, and an earlier draft of this change that refused on absence was wrong. `/seats` reports bookable seats, so a row whose every seat is occupied is missing from its own cabin's response. Cabins also split mid-row — the 739's Comfort+ ends a row later on the right — so one row number legitimately sits in two cabins. Absence is not disproof, and refusing on it would reject correct assessments.

Disproof needs the cabin's row layout, which the service already parses (that is how `exit_rows` covers a sold-out exit row) and does not report. Filed as jbaruch/expertflyer-api#20.

`held.why` is rendered before the early returns, so every response carrying a held seat describes it — `nothing_open` and `upgrade` alike. Only `held_position_unknown` has no position to render, and the contract says so.

The output contract is now stated per response shape rather than as one flat list. Two responses carry no `verdict` at all — an unusable argument and a cabin that failed to load — and `no_held_seat` carries `verdict` without `held`. Naming them keeps an agent from reading a valid error as a malformed response.

## 0.2.106 — 2026-08-10

### Fixed — the byAir seat fields are named differently on read and on write

Step 3 told the agent the held seat lives on byAir as `seat_number` / `seat_type`. Those are the parameter names `byair_update_booking_info` takes on write; `byair_get_flight` returns the seat as `seatNumber` / `seatType`. An agent reading the skill and looking for the snake_case keys finds neither, concludes byAir has no seat for the flight, and asks the operator for a seat byAir already holds.

Confirmed against a live `byair_get_flight` payload for DL2957 on 2026-08-11.

## 0.2.105 — 2026-08-10

### Added — `assess`, which judges the seat already held rather than the cabin around it

The seat pass answered a different question from the one asked. "Make sure I have the best seats" ran a cabin scan, which reports what is open; whether any of it beats the seat already assigned is a comparison the scan never made. The agent made it by eye instead, and got it backwards — it reasoned that a Main Cabin exit row was equivalent to Comfort+, contradicting the ranker's own documented rule, because nothing forced the comparison through code.

`expertflyer.py assess` takes the held seat and returns a verdict: `optimal`, `upgrade` with the seat named, or a refusal. `is_upgrade()` — written for this and until now called by nothing outside its tests — is what decides it.

The sweep walks up the cabin ladder rather than reading one cabin. A single-cabin check structurally cannot see a Comfort+ window opening while the operator sits in Main, which is the upgrade most worth reporting. `--scan-up` sets the width, because each rung is another request to a bot-walled service.

`optimal` is scoped to the cabins actually read, never to the aircraft. A one-rung sweep from the Main Cabin never looks at Delta One, so "nothing open beats your seat" would be a claim the sweep did not establish — the same overstatement in a new place. `cabins_unscanned` names what was skipped, and the skill reports it alongside the verdict.

Absence of the held seat is a refusal, not a fallback. `no_held_seat` and `held_position_unknown` exit non-zero and carry no `upgrades` field. Answering the cabin-scan question when the comparison could not be made is how the original wrong answer got reported as a confident one.

The held seat is never in the service's response — it is occupied, by the operator — so it is reconstructed. Row and column come from the designator, exit-row membership from the cabin's layout, and position from `--held-position` when stated or from the columns of the open seats beside it when not. `position_source` records which. A column two open seats disagree about is dropped rather than resolved: a cabin running 2-2 forward and 3-3 behind makes one letter both a window and a middle, and picking one decides the operator is in a seat they are not in.

A held middle is assessed rather than crashing. `--held-position middle` is a supported input, and `is_upgrade()` ranked the held seat through `seat_sort_key()`, which has no score for a position rule 1 excludes outright — so the operator most worth answering got a `KeyError`. A middle needs no score: every seat worth taking beats it, including one further back in a worse cabin.

Row 0 is rejected rather than parsed. It sorts ahead of row 1, so a mistyped seat would rank as the furthest-forward seat on the aircraft and report every real seat as worse than it — a confident `optimal` built on a seat that does not exist.

A cabin that fails to load aborts the assessment. That cabin could be holding the upgrade, so a partial sweep must never report that nothing better is open.

Step 4 collects every held seat in one exchange before assessing any flight, and names the flights that came back unanswered. A flight whose verdict never arrived is not a flight with good seats.

The held seat lives on byAir's booking info as `seat_number` / `seat_type`. Reading it there keeps one store rather than a local copy that drifts from what the byAir app shows.

## 0.2.104 — 2026-08-10

### Fixed — First, Delta One and Premium Select ranked below Comfort+

`_cabin_rank` scored `W` at 1 and every other cabin at 0, so First (`F`), Delta One (`C`) and Premium Select (`A`) tied with the Main Cabin. Cabin is the first key in the sort tuple, so a Comfort+ window in row 30 outranked the First seat already held in row 1 — and `is_upgrade` would have reported it as an upgrade worth moving to. Found while wiring the held-seat comparison; the live case is 1A on DL2714 with Comfort+ open further back.

Cabins now rank on the full ladder from `references/web-contract.md`: `F` > `C` > `A` > `W` > `Y`. `cabin_code()` resolves the operator's words ("Delta One", "premium select", "coach") to the service's codes, so a held cabin stated in prose compares against the right rung. Premium economy stays `A` and never collapses into `W` — they are different cabins one rung apart.

`seat_cabin()` is now the single place a seat's cabin is resolved and validated, so a cabin fault carries the seat label the way a position fault already did — `cabin_code()` knows the cabin but not whose it is, and Step 5 relays `detail` on the promise that it names the seat.

An unrecognised cabin raises rather than scoring as Main Cabin. Silently ranking an unknown premium cabin at the bottom is how a downgrade gets reported as an upgrade, which is the defect this entry describes.

`expertflyer.py` reports a refused ranking as `{"error": "unrankable"}` with the offending seat named, rather than letting `SeatQualityError` reach the operator as a traceback — the ranker now raises on the production path where it previously could not. `ranked`, `best` and `acceptable_total` are dropped from that response so a partial ranking cannot read as a complete one.

The skill carries the new error's flow rather than leaving it to the agent. Step 5 names `unrankable` as the one value that is not an access fault — the service answered, so the access diagnostic it otherwise runs would investigate a layer that is working. Step 2 gains a first rule for a response carrying `error`: `best` is absent rather than `null`, and the two mean opposite things. `null` is "nothing here is worth taking, watch it"; absent is "nothing was ranked", so offering an alert would claim a seat is missing when its quality was never established.

A seat whose `position` is present but unrecognised now says so, naming the value. It previously reported "no window/aisle/middle flag", which sends the operator to look for a missing field when the field is there with a word the ranker does not know.

## 0.2.103 — 2026-08-10

### Fixed — a TLS verification failure no longer reports as an unreachable service (#229)

A certificate that could not be verified surfaced as `unreachable` with "check the service is running and EXPERTFLYER_API_URL points at it". The service had answered; only its chain failed validation, so that message sends the operator to the wrong layer.

It is now a distinct `tls` error naming the fix: on a host whose Python does not read the system trust store, point `SSL_CERT_FILE` at the system CA bundle — `/etc/ssl/cert.pem` on macOS, `/etc/ssl/certs/ca-certificates.crt` on Debian. Following the message makes the same call succeed, verified against the deployed service.

The recovery names the system store rather than `certifi`: this client is stdlib-only, so prescribing a package the plugin does not declare could fail with `ModuleNotFoundError` on the very host that needs the fix.

Ordinary connection failures keep the `unreachable` wording, which is still where they should be looked at. In the container this path does not arise — the service is reached over plain HTTP on the docker bridge — but the diagnostic is what a person reads when verifying by hand from a machine on the tailnet.

## 0.2.102 — 2026-08-10

### Added — review seats across upcoming flights (#229)

`skills/expertflyer/scripts/upcoming-flights.py` turns the travel schedule into the seat pass's work list: which flights are coming up, and how to name them to the ExpertFlyer service. It performs no network call — the skill runs it, then runs the per-flight seat check.

Departures inside the next 12 hours are skipped. By then check-in has assigned a seat and moving rarely helps, so reporting them is noise.

The reference instant is injected with `--now` rather than read from the clock, so the suite does not rot as the real date advances. A summary that does not parse is skipped rather than guessed at — better to miss a flight than to invent a flight number from prose like "Rebooked - see email". Re-synced duplicates collapse on the stable TripIt `uid`.

The new skill step reports only flights that need something: a seat worth taking, or an empty cabin worth watching. A flight where the cabin does not exist on the aircraft, or where nothing better is open, is passed over silently.

Without a TripIt `uid` the dedupe key carries route and departure as well as carrier, number and date: a through flight keeps its number across legs on the same day, so a narrower key silently dropped one of them.

Input faults are reported, never raised: a malformed `--now`, a schedule whose root is a valid JSON scalar rather than a list, a file that cannot be read or is not UTF-8, and an unparseable event timestamp each produce structured JSON on stdout with a stderr diagnostic that names the recovery — regenerating the schedule with `tessl__nightly-travel-sync`, or checking the group volume is mounted and readable. A malformed `--date` on the client is caught before the previous-day retry computes it. A single bad timestamp skips its own event rather than losing the whole schedule, exactly as an unparseable summary does.

One known edge is handled in code rather than in prose: the schedule stamps UTC, so a late-evening local departure falls on the next UTC day and the service finds no such flight. The client retries the previous day itself and reports `date_fallback_applied` when it did, since fixed branching belongs in the script rather than in agent judgement. It is opt-in via `--date-fallback` and only the schedule-derived pass passes it: against a date the operator named, a flight that genuinely does not operate that day must say so rather than return the previous day's flight and rank its seats as the requested one's. Only an unresolved route triggers it — an auth failure is not a date problem. When the retry itself fails, its own error is returned rather than the first one, so an expired session does not surface as "no such flight"; `date_fallback_attempted` records that the retry happened.

## 0.2.100 — 2026-08-10

### Fixed — seat-ranking defects found in review, and the ranking is now actually wired (#229)

Three blocking findings from the policy reviewer on #248, all real:

`is_upgrade()` took one `cabin` argument and applied it to both seats, so the documented rule that Comfort+ outranks a Main Cabin exit row was unreachable — the comparison that made cross-cabin ranking worth having could not be expressed. Each seat now resolves its own cabin from the `cabin` the service stamps on it, with the caller's value as fallback, and a seat with neither raises rather than being guessed at.

`describe()` prepended `row` to `label` and rendered `1414B` against the real service response, whose label is already row-qualified. `seat_label()` now accepts both shapes — the service's `14B` and the raw payload's column-only `B` beside a `row`.

The ranking module was never invoked: the skill still reported the service's unranked `matching`, so none of the advertised behaviour could occur. The client now ranks the `seats` response and adds `ranked`, `best`, and `acceptable_total`. `acceptable_total` can be `0` while `available_total` is not — a middle is never offered, so DL2957's Comfort+ cabin, whose single free seat is a middle in row 14, correctly yields nothing.

Also replaced future-date literals in the client tests with fixed past dates (#246 finding): the HTTP layer is mocked, so no live upstream rejects a past date and the Live-Upstream Future-Date carve-out does not apply.

The seats step then had two directives that could disagree. `matching` is the service's own filter and can list a seat the ranking refuses — a middle, for `--want middle` or `--want any` — so an agent following both would report a seat as open AND treat nothing as worth taking. The availability and alert decision now reads `best` / `acceptable_total` alone, with `cabin_present` handled first and `matching` demoted to informational. Prose script references are repo-relative, which also corrected an older one for the client itself.

### Fixed — the exit-row recline tier is derived from the cabin layout (#249)

Ranking distinguished a reclining exit row from a fixed-back one by reading a `reclines` field that the seat map does not carry. The deployed service returns `isExitRow` and `row` and no recline signal, so the distinction never fired and both exit rows ranked identically — on a Main Cabin flight the skill could have pointed at the fixed-back row.

It is now read off geometry. An exit row cannot recline when another exit row sits directly behind it, which is precisely why the forward row of a pair is fixed and the second is the one worth having. A lone exit row has nothing behind it and reclines.

The ranked output carries the derived tier through to its description too: the client computes the tiers once and passes them to both ranking and rendering, so a derived reclining row reads as `21A (window, exit row, reclines)` rather than a bare `exit row`.

Adjacency needs the cabin's FULL exit-row layout, not the seats on offer. The service reports bookable seats only, so an occupied rear exit row is invisible — deriving the tier from that list alone would call the open row in front of it reclining, recommending precisely the fixed-back seat the operator does not want. Ranking therefore takes the layout separately and, without it, claims no exit row reclines: an explicit per-seat flag is still honoured where one exists, but absent evidence never promotes.

Within a supplied layout, a middle in the row behind still fixes the row in front, so tiers are computed across every row rather than only the bookable ones.

The layout reaches `is_upgrade()` as well as bulk ranking. Without it the watch case compared a rear reclining exit row as though it were fixed-back, so the seat that opened could lose to the forward row it should beat — the one comparison that function exists to make. Judging a seat in isolation, with no cabin context, still falls back to an explicit `reclines` field and then to the weaker tier, so an unknown seat is never promoted over one known to recline.

## 0.2.98 — 2026-08-10

### Fixed — the ExpertFlyer client default bypassed the OneCLI gateway (#229)

`DEFAULT_URL` pointed at `host.docker.internal:8090`. That alias is in nanoclaw's `AGENT_PROXY_BYPASS_HOSTS` — the INCIDENT-746 bypass that keeps the Anthropic credential-proxy hop direct — so a request to the hostname skips the OneCLI gateway entirely.

The gateway is what swaps the real bearer in for the `onecli-managed` placeholder the container holds, so the default would have sent the placeholder through unswapped and earned a 401. Deployments that set `EXPERTFLYER_API_URL` explicitly were unaffected; the default was a trap for anyone who did not.

Now `http://172.17.0.1:8090`, addressing the bridge gateway by IP, with a test pinning it so the friendlier hostname cannot be restored without failing the suite.

### Added — seat-quality ranking for the ExpertFlyer skill (#229)

`skills/expertflyer/scripts/seat_quality.py` encodes the operator's seat preferences as an ordering, so "what's open" can become "what's worth taking".

Ranking is preference, not fact, so it lives in this plugin rather than the `expertflyer-api` service: the service reports what a seat *is*, this decides what it is *worth*.

A middle seat is **excluded**, not ranked last — it can never be offered nor count as an upgrade. On DL2957, whose Comfort+ cabin has exactly two free seats and both are middles, the correct output is nothing at all.

Window beats aisle, but graded rather than absolutely: `WINDOW_WORTH_ROWS = 3` is the exchange rate, so a window up to three rows further back still wins and a fourth row back loses to the aisle up front. Closer to the front breaks ties; bulkhead is neutral. An exit row outranks forward position, and reclining beats fixed-back. Comfort+ outranks an exit row, because it buys forward position *and* leg room where an exit row buys only leg room.

Every rule is a named constant with a test that states it, so tuning is a one-line change that fails a named test rather than silently shifting recommendations. An unclassified seat raises instead of ranking arbitrarily.

## 0.2.97 — 2026-08-09

### Added — ExpertFlyer seat and fare-class checks, via a service (#229)

Adds the `expertflyer` skill: check seat availability in a named cabin, check fare-class inventory for an upgrade certificate, and create seat or fare-class alerts.

Every alert request is **check first, alert only if absent**. An alert for something already bookable is worse than useless — it delays the booking while the operator waits for an email describing space they could have taken on the spot. Both checks report `recommend_alert`, and creation refuses to duplicate an active watch on the same flight and class. A seat alert and a fare-class alert on one flight are different watches, so one never blocks the other.

The browser automation, the ExpertFlyer credential and the minted session live in the separate `jbaruch/expertflyer-api` service, not here. ExpertFlyer publishes no API, so the capability needs a real browser and an Auth0 login whose password rides in the request body — the one place OneCLI's gateway cannot substitute a placeholder, unlike the TripIt feed token it injects into a URL path. Rather than put a raw credential back into a container running an LLM with tool access, the automation moved to a service container that runs no LLM, following the `tripit-api` precedent. This plugin ships a stdlib-only HTTP client (`skills/expertflyer/scripts/expertflyer.py`) and holds no credential, no session and no browser.

Response semantics the agent must not re-derive live in `skills/expertflyer/references/web-contract.md`: `seats: 0` is an answer rather than missing data; `display_capped` means *at least* that many because the display stops at 9; `cabin_present: false` distinguishes a cabin the aircraft lacks from a full one, which otherwise look identical and would draw an alert on a cabin that can never open. Premium economy is Delta's **Premium Select** (`A`), a different cabin from Comfort+ (`W`) — the service rejects an unrecognised cabin rather than defaulting to economy.

Failures are relayed, not flattened: `unreachable` (service down or misconfigured URL), `auth` (the service could not authenticate, after retrying a login itself) and `blocked` (ExpertFlyer's bot wall — never retried in a loop). Upstream answers 403 to both a bot-walled and an unauthenticated request, so only the service can tell those apart.

## 0.2.96 — 2026-08-09

### Changed — an unanswered drive-or-fly question is nudged daily instead of asked once (#240)

The engine asked once and stopped. `mark_asked` stamps `asked_at`, `needs_question` goes false, and nothing ever set it back — so a missed Telegram notice left the trip with no drive legs AND no alert, right through departure. That is what happened to the live Gatlinburg trip: the question went out, was missed, and the trip sat unplanned for five hours until the operator was asked again out of band.

The open question now rides `check-travel-bookings`, which already exists to nudge about booking gaps and runs daily — the cadence a nudge wants, where the 30-minute sweep would be a nag. An `unknown` verdict raises the same hotel-without-transport gap as `fly`, worded as a question and naming the `drive` / `fly` reply words the answer path matches. Either answer clears it: `drive` drops out of the gap verdicts entirely, `fly` settles into the ordinary missing-flight line.

The sweep's immediate one-shot ask stays. It reaches the operator within half an hour of the trip appearing, and the daily surface is the safety net under it rather than a replacement — so nothing is lost if the notice lands while they are away from the phone.

`load_flying_trips` becomes `load_transport_gap_verdicts`, returning slug → verdict rather than a set of slugs. Its no-prior-state path is unchanged and deliberately loose: a missing, unreadable, or unrecognized-version store yields no verdicts and therefore no gap, because widening the reader must not widen the alert-storm surface. No `schema_version` bump — the record shape is untouched, so the cross-pipeline skew this reader gates on cannot open.

The reader honours `expires` rather than leaning on the owner's prune. An expired record — or one whose `expires` is absent, unparseable, or naive — is residue, not a verdict, and raises no gap; a reader that depends on another skill's housekeeping having run is a reader that reports stale state. This closes the same hole on the pre-existing `fly` path.

The nudge is bounded without a horizon of its own: an `unknown` verdict only exists for trips inside drive-engine's 14-day `SWEEP_WINDOW`, and the existing per-trip snooze still applies.

## 0.2.95 — 2026-08-09

### Fixed — a meeting belongs to a trip by being reachable from it, not by falling on its dates (#243 review)

The `TripPresence` matching shipped in #243 used date containment alone, so every meeting occurring while a driving trip was under way was claimed by that trip. Two consequences, both bad: an unrelated appointment — a home meeting never cancelled, or a meeting belonging to an overlapping trip — was exempted from the implausible-drive suppression and given a cross-country drive block, which is precisely the invented "drive to Tennessee swim practice while in Europe" that suppression exists to prevent; and its endpoints were rewritten to the wrong trip's lodging.

`meeting_ids_within` becomes `meetings_on_trip`, which additionally requires the venue to be within `LOCAL_TO_LODGING_MAX` of that trip's lodging. A meeting with no location, or an unroutable one, belongs to no trip — membership only ever grants an exemption, so the unknown case declines it. The upper bound now matches `context_from_blocks`, so a stay recorded past the wrapper's end extends both alike.

**Bridge legs keep their prior-venue origin.** `_trip_endpoints` rewrote the origin of every non-`return` leg, and `_DIRECTION_KIND` maps `bridge` onto `meeting_outbound`. A bridge leg runs venue to venue between two tight-gap meetings, so rewriting its origin invented a detour through the hotel between back-to-back events. It now matches `outbound` explicitly.

**Overlapping trips resolve deterministically.** Two trips can both reach one meeting, and assigning presence per trip let whichever iterated last win — making the chosen lodging and the first/last flags depend on trip ordering. The nearer lodging takes the meeting; an exact tie is declined rather than broken arbitrarily, the safe direction since membership only ever grants the suppression exemption. `meetings_on_trip` returns id → drive rather than a bare id set so the caller can settle it.

**One verdict-store read per sweep.** The meeting side loaded verdicts to decide which trips are drives and the lodging side loaded them again to plan those trips, straddling the store's own prune — two reads that could disagree about an operator answer inside a single sweep.

## 0.2.94 — 2026-08-09

### Fixed — the check-in stamp no longer decides which drives exist (#242)

Moving a hotel check-in past the trip's first event erased that event. On the live Gatlinburg trip, restamping check-in from 16:00 to 22:00 Friday deleted both of the opening ceremony's drives and re-anchored the outbound on Saturday afternoon — the operator drove down a day late and missed the thing he booked the hotel for. A check-in time is a fact about a reservation, not an instruction to the engine.

**The away-suppression no longer fires on a trip the operator drives to.** `DEFAULT_MAX_REASONABLE_DRIVE` means "not positioned to drive it — they flew, or are elsewhere". With check-in after the first event the anchor resolves home, the getting-there drive routes home→venue at its true 234 minutes, and the cap deleted it. The sweep now resolves the drive-verdict trips BEFORE the meeting side and passes their meetings through `driving_to`, where that premise is known false.

**A mid-trip home endpoint is rewritten to the lodging.** `scan` resolves one anchor per meeting, at the event's start, and both legs share it — so the ceremony's return leg drove 4 hours home out of the middle of the trip, with the next morning's drive starting from a hotel nothing had reached. `TripPresence` marks each trip's first and last event, the only two where home is real: they set off from the house, and `lodging_source` owns the drive back.

**The window's bounds are the trip's, not the stay's.** `context_from_blocks` keyed its lower bound on check-in, which is the same dependence by another name. It now runs from the trip wrapper's start, and `first_out` is the first drive INTO one of the trip's venues rather than the first drive OUT of the lodging. Venues are derived from the drives that touch the lodging, so a home→dentist errand on the trip's first morning is not mistaken for the trip's first commitment.

Verified on the live trip: the four outer and local legs are byte-identical with check-in stamped at 16:00, 22:00, 23:00 Friday, or 01:00 Saturday.

## 0.2.93 — 2026-08-09

### Changed — the outer legs no longer route through a hotel the operator drives straight past (#231 follow-up)

The outbound landed at the lodging at the exact instant the first local drive left it. Zero dwell — arriving and departing together, a stop made only to leave again. On the live Gatlinburg trip that read as "drive 3h50m to the hotel, then immediately drive to the ceremony."

When the trip's first local drive LEAVES the lodging, the outbound now goes straight to that venue and absorbs the local leg. The operator reaches the hotel on the event's own return drive, checking in when they actually do. The return mirrors it: a last local drive back to a lodging already checked out of is absorbed, and the drive home departs the venue rather than doubling back to a released room.

Both absorptions ride on `TripPlan.subsumed`, and `_plan_lodging_legs` now returns the meeting side with those blocks removed. Reconciling both halves would put two drives on the calendar for one journey and strand the absorbed one there. A failed direct route degrades to the old via-the-lodging shape rather than dropping the leg.

This fixes the nominal-check-in case, not the general one. The plan is still sensitive to the check-in stamp: a check-in moved past the trip's first event erases that event's drives entirely, so the outbound re-anchors on the next day's. Tracked as a bug in #242 — a correct engine plans the same trip the same way wherever the operator puts the stamp.

## 0.2.92 — 2026-08-09

### Fixed — the drive home no longer leaves before the last day's event (#231 follow-up)

The return leg of a flight-less trip departed at hotel check-out, stranding the operator: on the live Gatlinburg trip it drove home Aug 15 11:00 EDT and landed at 14:50, while the calendar still had a 17:47 drive from that hotel to a 18:00 game and a 22:00 drive back to it.

`context_from_blocks` closed its window at check-out, so no drive anchored after check-out ever became `trailing_end`, and `_return_block`'s `max(check_out, trailing_end)` had nothing later to pick. Check-out releases the room, not the operator — an evening event on the check-out day is the ordinary shape of a weekend trip.

The window now closes at the end of the trip's last day. Two things had to change together: the bound moved from check-out to the trip wrapper's end, and the upper comparison moved from instants to DATES. The date reading is the one `_in_span` already documents for the same wrapper — a date-only `end` parses to that day's midnight while items on the final day carry real times past it. On the live trip the wrapper ends 2026-08-16 00:00 UTC and the last game drive ends 02:13 UTC, so an instant comparison against the wrapper would still have dropped it.

The existing return-leg test passed throughout because it hand-built a `TripContext` with a post-check-out `trailing_end` — a value the real producer could not yield. The regression test now drives the return leg from the context `context_from_blocks` actually returns.

## 0.2.91 — 2026-08-09

### Changed — drive-time alerts need to be imminent and worth acting on

"Leave 4 minutes earlier" for a drive two months out was reaching the operator. Two gates were missing, and the alert fired on a percentage alone.

**A two-hour horizon.** A drive that far out gets re-routed dozens of times before anyone leaves, and every intermediate swing was announced. Inside two hours the number is close to final and there is still room to act on it. A block already under way passes the horizon — that is as imminent as it gets.

**A ten-minute absolute floor.** The 10% test alone fires on short drives for swings too small to matter: a 20-minute commute drifting 2 minutes is 10% and cleared the old gate. Both tests now apply, so an alert means the swing is proportionally real AND big enough to change what someone does.

Nothing changes about the calendar: a sub-threshold drive-time change still patches the block silently, so the times stay accurate. Only the interruption is dropped. `_MATERIAL_UPDATE_FLOOR_SECONDS` must stay at or above the reconcile's patch tolerance — below it no Update is scheduled at all, so the alert would promise a heads-up the sweep cannot deliver; a test pins that invariant.

`material_update_delta` now requires `starts_at` and `now`, and `apply_plan` takes the sweep's own cycle clock rather than reading a second one at write time — the "which `now` did the caller pass" scatter #154 was root-caused to.

## 0.2.90 — 2026-08-08

### Fixed — an airport hotel the night before no longer routes the morning drive from the house (#235)

Stage at an airport hotel before an early flight and the morning drive to the terminal started from the house you had already left. The pre-departure gate keyed on the trip's first *transport* departure, so every instant before wheels-up read as home — the #154 shape, reintroduced by the gate added later for the San Francisco→BNA block.

`_trip_begins_at` is now the earliest of the first transport departure and the first lodging check-in, so the check-in begins the trip and the morning anchors at the hotel.

That change alone was not shippable, which is why #233 left it. `engine.build_reconcile_plan` decided which final arrival closes a round trip by asking `position_at(opening_anchor).source == "home"` — a proxy for "did this journey leave home" that only agrees with it until the operator stages somewhere. Widen the gate and the proxy reads `lodging`, `return_home` goes false, and the drive off the final landing routes to the airport hotel instead of the house. Strictly worse than the bug being fixed.

So the proxy is gone. New `trip_origin.opened_from_home` asks the question directly: the journey left home when the planned position IS home, or when this departure is the trip's own first transport departure — in which case any lodging resolving there is a staging stay reached from the house. A later flight inside a trip already under way is not an opening, so a round trip flown out of a foreign city during a long stay still returns to that city's hotel rather than across an ocean.

A trip the operator DRIVES to and then flies a local round trip out of would otherwise read as opened-from-home too, since its first transport departure IS that flight — and the drive off the final landing would cross a state to the house. `STAGING_STAY_MAX_LEAD` separates the two: a check-in within a day of the first departure is an overnight staging stay, while one further back means he has been living at the destination. Without geography the lead time is the available signal, and the two shapes are hours apart versus days apart.

(That case was wrong before this change too — the old proxy also returned home-originating for it, verified against `main` — so it is a fix here rather than a regression avoided.)

## 0.2.89 — 2026-08-08

### Fixed — travel-core: a flight-less trip's pre-check-in anchor resolved to the destination city (#233)

`resolve_anchor`'s pre-departure gate keeps the operator anchored at home until they have physically left — without it, a date-only Trip wrapper is already "active" on the first day and an outbound drive anchors at the destination, which is what drew the 34-hour San Francisco→BNA block. The gate keyed on the trip's first *flight*, and its own comment said what that left open: *"a flightless trip skips this."*

So on a drive trip, an instant on the first day before check-in found no lodging at-or-before it and fell through to the Trip wrapper's `location` — the destination city. A meeting that morning routed Gatlinburg→meeting instead of home→meeting: wrong duration, wrong leave-by. The same cross-country shape the gate exists to prevent, reached by the other door.

The gate now falls back to the trip's first timed lodging check-in when it has no timed transport departure. Rail counts beside Flight on the transport side — a train out is a departure from home too. A check-in only counts with a usable location, since the lodging ladder can anchor on nothing else and a blank one would step past the gate onto the destination city again; a date-only check-in never gates, for the same midnight-parse reason the transport side already ignores date-only records.

Deliberately a fallback rather than an earliest-of. On a trip that *does* have transport, letting a pre-flight staging hotel begin it flips `engine.build_reconcile_plan`'s homecoming test from `home` to `lodging`, and the round trip's drive home then routes to that hotel instead of the house — strictly worse than the bug it would fix. That case (an airport hotel the night before an early flight, the #154 shape) is tracked in #235, and a test pins the current answer so this fix cannot silently widen into that regression.

## 0.2.88 — 2026-08-08

### Fixed — keep skill descriptions under the registry's limit, and catch it before merge

The drive-engine description grew to 1118 characters in the change above; `tessl plugin lint` caps a `description` at 1024. That gate runs inside the publish workflow, which fires only after a merge to main, so the over-long value passed every pre-merge check and then turned main red with a failed publish (no version was published — lint fails ahead of it).

Description trimmed to 1012 with its trigger phrases intact. `tests/test_plugin_manifest_limits.py` now checks the manifest's description and every declared skill's frontmatter description against the same ceiling in the ordinary suite, so the failure surfaces on the branch rather than after the merge. It also asserts each manifest-declared skill directory actually has a `SKILL.md`, which would otherwise make the description check silently vacuous.

### Added — drive-engine: the getting-there legs of a trip you drive to (#231)

A trip booked with lodging and no flight fell through every net the engine has. The intra-trip drives worked — a hotel→event drive is an ordinary meeting leg whose origin `position_at` resolves to the hotel — but nothing planned the drive that gets the operator there, because the airport chain anchors on flights and a hotel check-in is not one. Nothing warned either: for a genuine drive trip a missing flight is normal, so `check-travel-bookings` counted the trip complete. Live case that surfaced it — TripIt "TN TIGERS VS Faith Christian School" (Gatlinburg, TN, Aug 14-15 2026): the hotel→game drive appeared on the calendar, the home→hotel drive never did, and no alert fired.

New `lodging_source.py` finds flight-less trips with lodging and plans the outbound `home → lodging` leg and its `lodging → home` return. Two decisions in it are load-bearing:

**The origin is home by construction, never `position_at`.** Every other leg resolves its non-fixed endpoint through the planned-position ladder, and for this one that ladder gives the wrong answer twice over: at the outbound leg's own anchor the operator is already checked in by its reckoning, so it answers "the hotel" and the leg collapses to zero length; anchor earlier and it answers the trip's own `location` — the destination city — because `trip_origin.resolve_anchor`'s "still at home" guard keys on the trip's first FLIGHT, which a flight-less trip has none of. That guard's hole is real for meeting legs too (a meeting on the trip's first day, before check-in, anchors in the destination city rather than at home) and is filed separately rather than widened here, since `resolve_anchor` is shared with flight-assist.

**The outbound lands by the first local drive, not by check-in.** TripIt stamps a nominal mid-afternoon check-in whether or not anyone agreed to it; the hotel→event drive is anchored on a real commitment. Arriving after that drive leaves means missing the event, so `context_from_blocks` reads the already-planned local drives and the outbound targets the earliest one. The return symmetrically departs after the later of check-out and the last local drive, so it is never planned across an event still under way.

Drive-or-fly is read off the computed home→lodging drive in three bands (`classify_drive`): under 3h it is a drive and is planned silently, over 7h it is not and the booking gap is `check-travel-bookings`' to report, and between them the drive time is not evidence either way. In that middle band the engine asks the operator once and `drive_decision.py` persists the answer, which outranks the band from then on — `record_drive_time` never overwrites an unexpired operator verdict, and the ask stamp survives a re-derivation, so the question is asked once per trip rather than once per 30-minute sweep. The nag it avoids is the lombot #49 scar `skip_state` exists for.

Worth noting for the thresholds: the trip that motivated the issue is roughly a 3h40m drive, so it lands in the ambiguous band and asks rather than building silently. That is the design working as specified, not a miss — but if the intent was "this one should just work", 3h is the number to move.

The 3h ceiling is deliberately NOT shared with `meeting_source.DEFAULT_MAX_REASONABLE_DRIVE`, which is also three hours and means the opposite: there, a drive that long is evidence the operator is elsewhere and the leg is suppressed as implausible. Same number, opposite conclusion; a comment on each says so, since the obvious "consolidation" would be a bug.

Lodging drives apply silently like airport drives, for the same reason — they are not skippable. The drive-or-fly question is a third wake reason beside a new meeting drive and a material re-time. The operator answers via `answer_drive_or_fly.py` (SKILL.md Step 3), which resolves the trip by the name the question used and records `drive` or `fly`.

`check-travel-bookings` reports the other half: a new `отель есть, рейса нет` gap, the mirror of `рейсы есть, отеля нет`. Per the owner's call it lives with the rest of the booking-gap alerts rather than in the sweep, so it reads the verdict rather than computing a drive time it has no router for. It is gated on a `fly` verdict rather than on missing transport alone — a trip the operator drives to has no transport booking by design, and alerting on that would nag about every weekend away. `drive` and `unknown` are not gaps: the first means the drive is planned, the second that the operator has been asked and has not answered.

That reader is deliberately looser than the owner's own: a missing, unreadable, or unrecognized-version store yields no gap rather than raising. A non-owner reader's no-prior-state path must stay non-disruptive (`coding-policy: stateful-artifacts`), and inventing missing-flight alerts out of an unreadable file is the alert storm that rule exists to forbid — under-reporting a gap is recoverable, a storm of false ones is not.

An orphan check-in — a stay TripIt wrote with no check-out record, which `build_lodging_ranges` already handles for coverage — bounds the "at the destination" window at the trip wrapper's end rather than at the check-in instant. Collapsing it to the instant made every local drive invisible: the outbound ignored the onward drive it must land before, and the return leg was dropped as having nothing to depart after even with trailing drives on the calendar. (Caught in review on #232.)

Rail counts alongside Flight as booked transport, so a train trip with a hotel is not mistaken for a drive. Car Rental deliberately does not: renting a car is compatible with driving there.

Surface sync: `drive-decisions.json` documented in `skills/drive-engine/state-schema.md` (owner, writer/reader contract, tolerance on both sides).

### Changed — travel-core: host the trip key and the lodging-role discriminator

Two things had grown copies. The trip slug had two implementations, one in `build-travel-db.py` taking an ISO string and a dead divergent copy in `check-travel-bookings.py` taking a `date`, reachable only from its own test; the drive engine's verdict store keys on that same slug, so a third copy would have decided whether a cross-skill read hits or silently misses. The `Check-in:` / `Check-out:` prefix that discriminates the two `Lodging` records TripIt writes per stay was spelled in three places, and reading it wrong is silent — a check-out record answers `start` as happily as a check-in does, so a stay looks like it begins on its last morning.

Both now live in `travel-core` (`trip_key.py`, `lodging.py`) with every consumer converted. `trip_origin._parse_when` is public as `parse_schedule_time` for the same reason: the schedule's date-only-wrapper-reads-as-UTC-midnight convention is a travel-core concern, and `lodging_source` needed it rather than a fourth parser with subtly stricter rules.

## 0.2.85 — 2026-08-05

### Changed — retire Dependabot again; Renovate stays the sole scanner

`.github/dependabot.yml` came back in #208 and is removed for the second time. c8988f2 had already retired it in July for the duplicate-PR problem — both scanners cover the same two managers (`github-actions`, `pip` via `requirements-dev.txt`), so every upstream release arrives twice and each merge publishes a version. #208 re-added it on the premise that the repo "had no scanner config", which was not the case; Renovate has onboarded since #109 and has opened every dependency PR this repo has seen (12 of them), while the re-added Dependabot opened none in the two weeks it sat there.

The pin #208 wanted covered — the SHA-pinned reusable publish workflow in `.github/workflows/publish.yml` — is renewed by Renovate: it extracts as a `github-tags` digest dep (confirmed with `renovate --platform=local --dry-run=extract`, and historically via the `Update jbaruch/coding-policy digest to <sha>` PRs #148, #152, #207 and #209). The Dependency Dashboard prints that pin as a blank row because a digest-only dep carries no `currentValue` to render — that blank is a display artifact, not a coverage gap, and is the likely reason the pin looked unmanaged. `renovate.json` now carries a top-level `description` recording all of this so the third re-add doesn't happen. GitHub-native Dependabot **security** alerts remain a repo setting, unaffected by this file.

## 0.2.84 — 2026-08-05

### Fixed — sync-tripit: declare a precheck budget so the delegation timeout can actually fire (#212)

`_SYNC_SUBPROCESS_TIMEOUT = 60.0` was a bare literal sitting behind a wall half its size. Pre-`jbaruch/nanoclaw#890` the agent-runner SIGTERMed every precheck at a flat 30s, so the `subprocess.TimeoutExpired` handler in `main()` could never run: a hung `sync_tripit.py` surfaced as `precheck-error: execfile-error` with no payload instead of the safe-shape `{"wake_agent": false, "data": {"reason": "sync_subprocess_timeout"}}` the handler already emitted. The branch was dead code for the life of the skill.

`#890` removed that flat global and made the budget per-skill, which un-buried the branch but replaced the problem with the opposite one: a skill declaring nothing now runs unbounded up to the container kill (`MAINTENANCE_CONTAINER_TIMEOUT`, 300s). sync-tripit's cadence is `*/5 * * * *` — the same 300s — so an undeclared wedge runs right up to the moment the next fire is due and is still holding the maintenance slot when it arrives.

sync-tripit now declares `precheck_timeout_ms: 90000` and derives the delegation budget from it: `_SCRIPT_KILL_BUDGET_SECONDS - _INTERPRETER_TEARDOWN_HEADROOM_SECONDS`, leaving the handler room to write its stderr note and payload before the kill lands. It joins flight-assist (30s at a 2-min cadence) as the second travel precheck to declare one; every other travel precheck still deliberately declares nothing, `drive-engine` especially.

The effective delegation budget moves 60s → 86s. Deliberately loosened, not tightened: `sync-tripit` has never hit the wall across 3,108 runs, so this is an unreachable-handler correctness fix and a tighter number would start deferring real work it had time to do.

`sync_tripit.py` also bounds its own byAir client at 20s (`_BYAIR_CALL_TIMEOUT_SECONDS`) instead of riding the client's 30s default — the inner-vs-outer collision `#28` fixed for `flight-assist` and the second half of `#212`'s scope. A hung upstream now fails fast into the existing `URLError` transient-transport branch, which skips the pass and lets the next 5-min fire retry, rather than consuming a third of the subprocess budget and losing the diff to a blunt kill.

Coverage: `test_sync_bounds_the_byair_client_below_the_precheck_budget` asserts the bound at its construction site — the ordering checks below all still pass if the `timeout=` argument is dropped, so the constant needs a test that it is actually *used*. New `tests/test_sync_tripit_precheck_budget.py` locks the chain `byAir per-call < subprocess < declared kill` and asserts the SKILL.md declaration and the script constant stay one number (the same drift `#890` opened for flight-assist). New `test_subprocess_timeout_branch_is_reachable` drives a genuinely hung child through the real `subprocess.run` call site — the pre-existing timeout test raises `TimeoutExpired` directly, which proves the handler converts but not that anything can reach it, and reachability is precisely what `#212` was about.

Also corrects `sync_tripit.py`'s module docstring, which still claimed "Run cadence: daily at ~04:00 local" — a claim untrue since the `sync-tripit` scheduler took over invocation.

## 0.2.83 — 2026-08-02

### Added — check-travel-bookings: warn when a hotel stay's TripIt location is garbage

TripIt sometimes drops a non-address into a Lodging's `location` — a resort-fee / rate note (`"Stay resort fee: $72.03"`) or a blank — which breaks drive planning (the anchor resolves nowhere) and reads as nonsense on the calendar. New `check-lodging-locations.py` scans `travel-schedule.json` (the only artifact carrying `location`) for upcoming stays and flags these; the `check-travel-bookings` skill now sends a Telegram warning naming the hotel, the bad value, and the check-in date. The fix is a manual TripIt edit, so this only detects and alerts — nothing is auto-corrected. It runs on-demand and nightly (via `nightly-travel-sync` Step 6, unchanged). Detection is deliberately conservative and fully enumerable (blank / currency amount / rate-fee keyword — never an "is this an address?" judgment), so real addresses never false-positive.

## 0.2.82 — 2026-08-02

### Fixed — drive-engine: drop the dormant `<!--dengine:-->` description-reader fallback (#200)

The drive-block writer moved its machine state to `extendedProperties.private` in
#178, and the calendar has been verified to carry zero remaining description-carried
`<!--dengine:-->` blocks. The now-dead unified description reader and its build/marker
machinery are removed from the block codec; `parse_block` now reads unified state from
`extendedProperties` and still recognizes the two legacy description shapes (fadrive,
dp) for R4 cutover convergence.

## 0.2.81 — 2026-08-02

### Fixed — flight-assist: drop the dormant `<!--fa:-->` description-tag reader fallback (#200)

`decode_private_props` no longer falls back to the `<!--fa:{...}-->` description
comment — it reads managed tags only from `extendedProperties.private`, the sole
live source since the #178 migration. The now-unused `encode_tags`/`decode_tags`
codec is gone; `strip_tags` survives to scrub any stray legacy comment out of a
human description on write. Verified zero live boarding/flight events still carry
the description tag.

## 0.2.80 — 2026-08-01

### Fixed — drive-engine: scope shared trip aliases to homecoming detection

Version 0.2.79 used the shared TripIt alias to place split byAir journeys in one
connection chain. That repaired the final BNA homecoming, but a TripIt itinerary
also spans the multi-day stay between outbound and return. With no lodging record,
connection classification treated that stay as an airside layover and removed the
valid `Drive: OSL → Oslo, Norway` arrival block.

Primary byAir trip IDs again define operational connection chains and preserve the
ground endpoints around a stay. The connected source aliases now answer only the
itinerary-level homecoming question: whether the last arrival closes at the first
departure airport after opening from home. Regression coverage pins both outcomes
together—`BNA → home` is repaired while `OSL → Oslo, Norway` remains present.

## 0.2.79 — 2026-08-01

### Fixed — drive-engine: reconnect round trips split by byAir trip IDs

byAir can assign the outbound and return halves of one TripIt itinerary different
trip IDs. When each flight existed in both sources, the merge kept only byAir's
preferred ID and discarded TripIt's shared itinerary alias. The return flight then
became a singleton chain, so a BNA landing still resolved its destination to the
active San Francisco trip location and recreated the 33-hour
`Drive: BNA → San Francisco, CA` block.

Merged flights now retain every source-side trip alias while preserving the
existing preferred `trip_id` compatibility field. Chain assembly groups flights
by connected aliases, so the shared TripIt itinerary rejoins split byAir halves
transitively and the closing BNA arrival is recognized as the homecoming leg. The
regression fixture uses the exact live shape: distinct byAir IDs for outbound and
return plus one shared TripIt ID.

## 0.2.78 — 2026-08-01

### Fixed — drive-engine: return arrivals go home and current repairs outrun cleanup

A final SFO→BNA flight produced a 33-hour `Drive: BNA → San Francisco, CA` block. The trip's date-only wrapper and last lodging were still active at the post-arrival anchor, so the generic trip-position resolver sent the arrival drive back to the destination city. The engine now recognizes a flight chain that started from home and closes at the same airport, routing only that closing arrival home. Same-airport side trips that begin while already away still return to their trip lodging.

The far-future cleanup introduced in 0.2.76 exposed several thousand duplicate and orphan deletes. The 20-second write budget applied every delete before any update or create, leaving current corrections behind hours of cleanup. Apply now processes updates, creates, and conversions before orphan deletion. Scheduled runs retain their bounded write phase; an operator repair can set `DRIVE_ENGINE_UNBOUNDED_APPLY=1` to drain the complete reconcile plan in one run.

## 0.2.77 — 2026-07-31

### Fixed — nightly-travel-sync: cadence cap no longer near-misses and skips (jbaruch/nanoclaw#803)

The precheck's `CADENCE` cap was `timedelta(days=3)` — an exact 3× multiple of the daily cron. The cursor stamps at run completion, so every third daily fire found `travel-db.json` ~71.8h old (< 72h) and skipped, slipping the run by a whole period — which is why the sync stopped running and the OOO home-midnight migration never applied. The cap is now `timedelta(hours=60)`: same every-third-day intent, but a half-period under the multiple with slack for run latency and DST (per nanoclaw-host `rules/overlay-tile-authoring.md`). Adds the mandated near-miss regression test — a cursor just under the 72h multiple must still wake.

## 0.2.76 — 2026-07-31

### Fixed — drive-engine: reconcile now sees far-future blocks, ending the duplicate storm

Desired airport legs are anchored to flight times across the whole itinerary (months out) with no future bound, but the reconcile fetched *current* blocks only `now+21d`. Every leg past 21 days was desired-but-never-matched, so each 30-minute sweep created a fresh block and never deduped the pile — a trip weeks out accumulated dozens of identical `Drive: → OSL` / `Drive: → AMS` blocks, and stale/suppressed legs (a `Drive: Oslo → AMS` from an obsolete plan) were never orphan-deleted.

`reconcile_sweep` now extends the current-blocks fetch `time_max` past the last flight instant across both flight sources (`_latest_itinerary_instant`), keeping a `+21d` floor. With the far-future blocks in view, `plan_reconcile`'s existing dedup (keep one per identity, delete the rest) and orphan-deletion drain the piles and remove stale legs over the next sweeps. `tests/test_drive_engine_reconcile_sweep.py` pins the horizon across both sources.

## 0.2.75 — 2026-07-29

### Fixed — drive-engine: a same-day layover with a destination-hotel check-in drew a bogus connection drive

A BNA→DTW→CDG→TLV trip drew a `Drive: CDG → Tel Aviv-Yafo` block — a ground drive from a Paris layover to Israel. The CDG connection was misclassified as an overnight because the Crowne Plaza Tel Aviv **check-in** (12:00Z = 15:00 local) fell inside the same-day CDG layover window (06:40Z–14:25Z). TripIt records hotel check-in / check-out at nominal local times, so a destination hotel reached only by a *later* flight can show a check-in timestamp that lands in an earlier daytime layover.

`build_pair_contexts` now sets `lodging_between` only when a lodging check-in falls in the gap **and** the gap spans a night (arrival and departure on different UTC days — `_spans_overnight`). A real overnight crosses midnight; a same-day layover cannot be one however a nominal check-in time lands in it. The genuine Tel Aviv stay (Aug 2 → Aug 6) still crosses a day boundary and keeps its drives. `tests/test_drive_engine_chain_builder.py` pins both the same-day regression and the cross-night overnight.

## 0.2.74 — 2026-07-28

### Fixed — drive-engine: anchor the outbound airport drive at home, not the trip destination

A BNA→SFO trip drew a ~34-hour `Drive: → BNA` block described "San Francisco, CA → BNA airport" — the outbound airport-departure drive resolved its origin at the trip's *destination* instead of home, so the engine computed a cross-country San-Francisco-to-Nashville "drive."

`trip_origin.resolve_anchor` treated the date-only `Trip` wrapper as "on-trip" for the whole departure day, and with no lodging check-in yet it fell through to the trip's own `location` (the destination). But before the trip's first flight departs the operator is still home. `resolve_anchor` now gates on the trip's first timed `Flight` departure: for any instant before it, the anchor is home. Flights after departure (the #122 mid-trip case) are unaffected; a trip with no timed flight in the feed keeps the prior behavior. `tests/test_trip_origin.py` pins the outbound-departure regression and the departure-day-before-flight boundary.

## 0.2.73 — 2026-07-28

### Changed — flight-assist: declare its own precheck budget (jbaruch/nanoclaw#890)

`jbaruch/nanoclaw#890` removed the flat 30s precheck kill the agent-runner applied to every skill. Two timeouts bound a precheck now: what the skill declares via `precheck_timeout_ms` frontmatter, and the host's container kill. A skill that declares nothing runs unbounded up to the container kill.

flight-assist declares `precheck_timeout_ms: 30000`, keeping exactly the budget it had. It is the one skill in the fleet that should: it fires every 2 minutes, so a wedged cycle running to the container timeout would still be holding the maintenance slot when the next fire is due. Every other travel precheck declares nothing deliberately — `drive-engine` especially, whose cold sweep is the reason #890 exists.

The declaration and `_SCRIPT_KILL_BUDGET_SECONDS` in `precheck.py` are one number in two places; `_run_cycle` sizes its poll loop against the constant so a poll started at the budget edge still returns before the kill. Previously the 30s was a fleet-wide global nothing could desync. `tests/test_flight_assist_precheck_budget.py` pins them together and asserts the loop still leaves room for one in-flight poll.

## 0.2.72 — 2026-07-27

### Fixed — drive-engine: drop the plan-phase time budget that froze the calendar (#211)

The sweep's 15s plan-phase budget (`_PLAN_PHASE_BUDGET_SECONDS`, from #172) was conceived to stop an LLM from wandering into deep reasoning and burning tokens. But `reconcile_sweep.py` is a deterministic script — there is no LLM in the plan / route / apply path, so there is nothing to bound. Once the tracked itinerary grew to ~13 unique airports, byAir's `get_airport` (~0.6s each, ~7.6s total) pushed elapsed past the deadline while `build_plan` was still resolving airports; `make_route` then refused the next cache-miss route and raised `PlanBudgetExceeded`, abandoning a perfectly valid, fully-computed plan. Every ~30-min cycle skipped with `plan_budget_exceeded` and applied nothing — the drive blocks froze for ~4.5 days.

The plan phase now runs to completion. The only bound on it is per-call network timeouts (`_SWEEP_MAPS_TIMEOUT_SECONDS`, and a new `_SWEEP_BYAIR_TIMEOUT_SECONDS`) — the real watchdog against a genuinely hung provider, where a failed call skips one leg and retries next sweep rather than stalling. `PlanBudgetExceeded` and both plan-budget gates are removed.

The write phase keeps its bound but no longer starves: `finish_sweep` gives `apply_plan` a **fixed** `_APPLY_PHASE_BUDGET_SECONDS`, decoupled from plan elapsed. The old `budget - elapsed` formula dropped the apply budget to 0 whenever planning ran long, so a slow-but-valid plan also wrote nothing. The write phase still stops with margin before the host precheck kill and defers the rest to the next idempotent sweep.

**Cross-sweep static airport-facts cache** (`airport_facts_cache.py`): IATA / country flag / IANA timezone are immutable, so they now persist to `airport-facts.json` and a warm sweep resolves known airports with zero byAir calls — cutting the dominant plan-phase cost to ~0. byAir's live `delay.index` congestion nudge is not cached; the sweep refreshes it only for departures within 24h, where it still moves the block. The cache is a hint, not authority: a missing, corrupt, or future-versioned file degrades to a refetch, never a raise (the deliberate opposite of the skip store's fail-closed read).

A cache-miss airport that byAir also can't resolve fails the whole sweep closed (`AirportUnresolved`) instead of dropping the flight — a partial plan would leave the flight's drive block with no desired leg, and the reconcile deletes unified blocks with no desired leg as orphans. So the refetch fallback never escalates to deleting live calendar state, only latency. A byAir failure where the static facts are cached degrades to those facts (no live delay), which is strictly safe.

The host-side precheck kill (`SCRIPT_TIMEOUT_MS`, 30s, in the `jbaruch/nanoclaw` agent-runner) is a separate layer — a cold sweep can still exceed it, though the mid-apply kill is idempotent-safe. A per-skill precheck-timeout override for real headroom is tracked in jbaruch/nanoclaw#890.

## 0.2.66 — 2026-07-21

### Added — `nightly-travel-sync` logs newly-appeared trips to daily memory (#204)

A new Step 5 gives the main agent durable awareness of trips it did not book. The existing sync surfaces timezone changes, OOO, conflicts, and booking gaps to chat, but a newly-appeared trip that is already fully booked — no TZ change, no conflict — entered silently, leaving nothing in memory. The new `scripts/detect-new-trips.py` diffs the freshly-rebuilt `travel-db.json` trip set against a persisted snapshot (`travel-trips-seen.json`, this skill's second owned artifact) and reports the new trips; the skill appends one line per trip to the group daily log via the co-loaded `trusted-memory` `append-to-daily-log.py`, then commits the snapshot.

Log-only by design — **no `send_message`**: the owner books the trips himself, so a per-trip chat ping is noise; the requirement is durable *agent* awareness only. The first run with no snapshot seeds silently (the itinerary is never dumped as "new"), detection is scoped to upcoming trips (`end` on/after today), and re-logging is guarded by both the snapshot and the daily-log helper's line-dedup. The snapshot is committed only after logging succeeds, so a logging failure retries on the next nightly run. Steps renumber: the travel-bookings check is now Step 6.

## 0.2.61 — 2026-07-18

### Changed — flight-assist managed-event tags now write to `extendedProperties.private` (writer flip, #193)

The flight-assist writer flip, the second phase of the #193 tag migration (after the dual-read reader shipped in 0.2.59). `calendar_reconcile` now stamps the managed tags (`faFlightId`, `faKind`, `faManaged`) into `extendedProperties.private` on create and adopt via `_create_event_args` / `_patch_event_args`, and the event `description` carries only the human content — tag-free. An adopt rewrites byAir's description through `strip_tags`, so a pre-flip event still carrying a `<!--fa:-->` comment is migrated on its next adopt (the comment dropped, the tags moved to `extendedProperties`); one that never re-adopts ages out.

Safe because the dual-read reader (0.2.59) was already live everywhere before this flipped, so no container runs the new writer against an old reader. `decode_private_props` reads `extendedProperties.private` first (a complete tag set only) and the description second, so both shapes round-trip through `normalize_event`. `encode_tags` stays for tests and the description-reader fixtures. Stale write-side wording in `calendar_plan.py`, `airport_block.py`, and `state-schema.md` is corrected.

## 0.2.59 — 2026-07-18

### Added — flight-assist managed-event tags read from `extendedProperties.private` too (dual-read, #193)

The flight-assist boarding-block / flight-adoption tags (`faFlightId`, `faKind`, `faManaged`) are migrating off the human-visible event `description` into `extendedProperties.private`, the same live-data migration #178 ran for drive blocks. This is the first, safe step: `calendar_tags.decode_private_props` reads `extendedProperties.private` FIRST and the `<!--fa:-->` description comment SECOND, and `calendar_normalize.normalize_event` now calls it, so an event tagged either way is recognized. The ext branch is taken only when the private map carries a COMPLETE managed-tag set, so a partial or malformed new-shape map never shadows a valid legacy description tag (`coding-policy: stateful-artifacts`, safe fallback).

Nothing writes the extended-properties form yet — the writer (`calendar_reconcile` create/adopt) still encodes into the description, so the new branch is dormant until the writer flips (a later phase). Shipping the dual-accept reader first is what keeps a deployed boarding block or adopted flight event from being orphaned mid-rollout (`coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps). The tag keys stay `fa`-namespaced (collision-safe in the shared private map) and are kept in sync with `calendar_plan`'s `TAG_*` by a drift-guard test. The flight-assist reconcile fetch already returns full event resources (no `fields` mask), so `extendedProperties` reaches the reader with no projection change.

## 0.2.58 — 2026-07-18

### Removed — the retired flight-assist airport-drive reconcile (#193)

`skills/flight-assist/airport_drive_reconcile.py` (and its test) is deleted. It was the flight-assist airport-drive assembler, orphaned when airport drives moved to the unified drive-engine in #156 — nothing but its own test imported it, and the live reconcile entry (`scripts/reconcile.py`) drives `calendar_reconcile.run_reconcile` instead. The rest of the airport codec (`airport_block`, `airport_drive`, `airport_drive_inputs`) stays — it is reused live by the drive-engine sweep (`reconcile_sweep` → `airport_drive_inputs` → `airport_drive` → `airport_block`), so only this genuinely-dead module goes. Stale doc references in `travel-core/SKILL.md` and `precheck.py` are repointed.

## 0.2.57 — 2026-07-18

### Changed — `nightly-travel-sync` Step 1 runs the TripIt → Reclaim sync in-container (#748)

Step 1 no longer calls the `mcp__nanoclaw__sync_tripit()` host-op. It now runs `reclaim-tripit-timezones-sync` inside the agent container via `scripts/sync-tripit.sh`, with TripIt / Reclaim / Google credentials swapped at the OneCLI gateway (the script sends placeholders and requires `ONECLI_URL` to be engaged, failing fast otherwise). The parsed `segments[]` are handed back to the host through the new credential-free `mcp__nanoclaw__persist_tz_segments` tool, which keeps the owner-timezone backbone (scheduler local-tz, the 30-min heartbeat advisory) exactly as the host-op's success path did; `timezoneChanges` / `ooo` / `conflicts` / `errors` still drive the chat summary. The wrapper's contract test gains coverage for the gateway-placeholder exports and the `ONECLI_URL` guard. Requires the host side of #748 (agent-image dependency, `ONECLI_URL` + `ENABLE_OOO=1` on the spawn, the `persist_tz_segments` handler) deployed and the TripIt/Reclaim vault entries configured before this activates.

## 0.2.56 — 2026-07-18

### Changed — drive blocks now write their state to `extendedProperties.private` (writer flip, #178)

The drive-engine writer flip, the second phase of #178 (after the dual-read reader shipped in 0.2.55 and materialized in production). `calendar_apply` now writes the block's machine state to `extendedProperties.private` via `build_extended_properties` on both create and patch, and the event `description` carries only the operator-facing route line (`origin → destination`) — the leg marker and `<!--dengine:-->` state comment no longer squat in the human-visible field. State squatted there only because the retired Composio toolkit exposed no writable `extendedProperties`; the native Calendar API (nanoclaw#638) does.

Safe because the dual-read reader (0.2.55) was already live everywhere before this flipped, so no container runs the new writer against an old reader. No recognizer needed changing: `meeting_source.exclude_drive_block_events` recognizes a block through `parse_block`, so it inherited dual-read in phase 1, and `scan.py`'s marker matches the unrelated legacy `drive-planner` shape. A block still carrying description-state from before the flip is read by the fallback and migrated to `extendedProperties` on its first post-flip shift (the patch replaces its description); one that never shifts ages out of the near-term window. `UNIFIED_BLOCK_SCHEMA_VERSION` is unchanged — the state's fields and meaning are identical, only the carrier moved.

Remaining: phase 3 (drop the description branch of `parse_block` once no description-state block remains in the near-term window) and the flight-assist `airport_block` / `calendar_tags` scope question, both tracked in #193.

## 0.2.55 — 2026-07-18

### Added — drive blocks read their state from `extendedProperties.private` too (dual-read, #178)

Drive-block machine state is migrating off the human-visible event `description` into `extendedProperties.private`, a machine-only field the native Calendar API (nanoclaw#638) exposes and the retired Composio toolkit did not. This is the first, safe step of that live-data migration: `block_codec.parse_block` now reads `extendedProperties.private` FIRST and the description SECOND, so a block written either way round-trips, and `fetch_events` carries `extendedProperties` through its field projection so the reader actually receives it.

Nothing writes the extended-properties form yet — the writer flip is a later phase — so every deployed block still parses off its description and the new branch lies dormant until the writer starts emitting it. Shipping the dual-accept reader before the writer flips is what keeps a deployed block from being orphaned mid-rollout (`coding-policy: stateful-artifacts`, Cross-Pipeline Schema Bumps; #178 acceptance: "dual-read ships before the writer flips").

`block_codec.build_extended_properties` is the schema's source of truth and the writer's phase-2 target: a flat `dengine_`-namespaced string map (the only value type the field accepts), one key per state field, carrying the same fields and `UNIFIED_BLOCK_SCHEMA_VERSION` as the description JSON — only the carrier moves. `drive-engine/state-schema.md` documents the full rollout order and the map's keys. The human line stays in the description on purpose (it is what the operator sees in the calendar UI); only the machine state migrates. Follow-up work (writer flip + `scan.py` / `meeting_source` marker-source move, then dropping the description reader once no description-state block remains) is tracked separately, along with whether flight-assist's own description-squatting codecs (`airport_block`, `calendar_tags`) migrate independently or are subsumed by the drive-engine cutover.

## 0.2.54 — 2026-07-18

### Added — drive blocks are Tangerine so they stand out from meetings and flights

Every drive block the engine writes now carries Google Calendar `colorId` "6" (Tangerine), the accepted half of #158's colour split tracked in #167 (owner decision 2026-07-12). Under the old Composio toolkit this was impossible — no create/patch action exposed a colour field, and a patch carrying `color_id` was a silent no-op. The native Calendar API (nanoclaw#638) exposes `colorId` on both `events.insert` and `events.patch`, so `calendar_apply.py` sets it on create and re-asserts it on every shift. A block created before this recolours on its next patch, so no separate backfill pass is needed.

Tangerine is the only orange among Calendar's 11 preset event colours. It currently also matches the Reclaim travel-block colour, but that overlap is transient — the owner is retiring Reclaim travel blocks as NanoClaw drive blocks take over.

## 0.2.52 — 2026-07-17

### Fixed — drive-engine notice is rendered deterministically, no longer composed by the wake

On 2026-07-17 the drive-engine cadence wake posted an escalating series of *false* "the engine is broken / disable it" alarms to the main chat (#187). The engine was fine — its telemetry shows `created:0, deleted:0` on every run that day; the only activity was routine drive-time recomputes. The alarms were hallucinated.

Root cause was a session-lifecycle defect in the host, but Step 1 handed it the fuel. The `isolated` cadence task carried a pinned `session_id` it resumed on every 30-min wake, so a `claude-haiku-4-5` agent re-read ~65k tokens of its own prior-wake output, treated its earlier (hallucinated) alarms as established fact, and ratcheted them — directly against Step 1's stateless-by-design intent ("compose ONE message from this sweep's payload, then finish").

`build_sweep_payload` already emits fully structured `material_updates` and `added_meeting_drives`; Step 1's "compose" was pure string interpolation over them — the one job that added no value and all the escalation risk. `render_notification` now builds the one-line notice in the precheck and puts it in `data.message`; Step 1 relays it verbatim. The LLM has nothing to compose, so a resumed session cannot escalate regardless of the host's session behavior. The templates moved out of `SKILL.md` into the script per `coding-policy: script-as-black-box`.

This is fix #2 of the two in #187. Fix #1 — stopping the isolated cadence row from resuming a pinned session — is host-runner behavior tracked in `jbaruch/nanoclaw#801`.

## 0.2.51 — 2026-07-17

### Added — `DRIVE_ENGINE_SHADOW`, a dry-run for the reconcile sweep

`shadow.py` has rendered a reconcile plan without writing since #156 R4, and nothing has ever called it. Its only importer was its own test — the same shape as the uncalled modules #181 deleted. `reconcile.py` meanwhile described shadow mode in the present tense ("the I/O layer applies the returned plan — or, in shadow mode, just logs it") when `reconcile_sweep` had no such branch and called `apply_plan` unconditionally.

Wired rather than deleted (#183). The renderer was already written and tested, and #178 — moving block state out of the event description — is exactly the block-shape cutover a dry run against the production calendar exists to de-risk. Deleting it would have meant rebuilding it for #178; wiring it gives it the named caller its absence made it debt for, and makes `reconcile.py`'s claim true instead of needing a correction.

`DRIVE_ENGINE_SHADOW=1` (or `true` / `yes` / `on`) makes the sweep plan, render, and return before the write phase. Off unless explicitly set, so the scheduled sweep is untouched; an operator opts a single run in from the shell.

Two contracts the wiring respects. The rendered diff goes to **stderr** — stdout carries the JSON payload the scheduler parses, so writing the plan there would corrupt it. And a shadow sweep **never wakes**: it changed nothing, so there is nothing for the operator to act on. Its payload is marked `data.shadow` and reports `planned` counts (from `shadow.plan_counts`, the #156 R4 acceptance surface) rather than the `applied` counts a live sweep returns, so a reader cannot mistake a rendered plan for applied work.

## 0.2.50 — 2026-07-17

### Fixed — a future-version skip file no longer re-nags every declined meeting

`load_active_skips` read a `skip-state.json` stamped newer than this plugin as "no usable prior state" — an empty map — citing `coding-policy: stateful-artifacts`' lagging-reader clause. That clause also requires the fallback be "safe and non-disruptive — never a path that escalates work (wake-always, full recompute, alert storm)", and an empty skip map is not inert. It drops every active skip, so the sweep re-plans each meeting the operator declined, creates drive blocks for them, and pings about each one: the trust-eroding nag of lombot #49, which is the scar `skip_state.py` exists to prevent. The file's own defence — "worst case the sweep re-asks; it never escalates work" — was self-contradictory, since re-asking *is* the wake.

Both paths now raise. The write path already refused (a write would rewrite the file as v1 and clobber a newer writer); the read path now matches it.

The objection to failing closed was that it trades a nag storm for a silent outage — `load_active_skips` raising surfaces at `reconcile_sweep`'s fail-closed boundary, so the sweep plans nothing until the plugin is upgraded. That turned out not to be a new failure mode. The engine already skips the whole cycle cleanly whenever it cannot build a trustworthy desired set — `PlanBudgetExceeded` does exactly this, because "a partial `desired` set reads as orphaned blocks to delete". Unreadable skip state is that same condition, so failing closed routes a new cause into an existing, deliberate path rather than inventing one. The cost stays explicit in `state-schema.md`: while the file is future-versioned, no drive blocks are planned.

A third option — read empty but suppress meeting planning for that sweep — was rejected as actively destructive. Handing the reconcile an empty meeting desired-set does not mean "do nothing"; it means every existing meeting block is an orphan, and the engine would delete the operator's drive blocks.

Reachable only via a plugin downgrade after a future v2 ships, or a hand-edited file: `#181` folded writer and reader into one bundle in one plugin, published together, so there is no cross-pipeline skew window.

`_read_skips`' `for_write` parameter is gone — it existed only to branch read-vs-write on this case, and both branches are now the same raise.

## 0.2.49 — 2026-07-17

### Removed — `drive-planner`, folded into `drive-engine`

`#156` retired `drive-planner` and kept it declared as a library, on the stated grounds that drive-engine's reconcile sweep imports its meeting-detection modules. That was true of three modules. The bundle shipped nine.

Establishing what drive-engine actually imports (`reconcile_sweep.py` + `skip_drive.py`) against what shipped found the list was wrong in both directions. It named `scan.py`, `fetch_events.py`, `skip_state.py` — and missed `home_address.py`, which the sweep has imported since #162 to resolve the home origin. The other five had no production caller at all: `apply.py` (create / remove were invoked by the sweep #156 disabled; suppress by the recheck skill #180 removed), `precheck.py` (the removed cadence), `recheck.py` (the removed skill — #180's note that it was "part of the kept library" was wrong; nothing imported it), `block_props.py` and `route_error.py` (imported only by the two dead modules above). ~91k of code held alive by its own tests.

`scan.py` carried a tenth surface: a JSON-stdin CLI whose only caller was drive-planner's `SKILL.md`. drive-engine imports the `scan()` function, never the process. Removed with its tests; the pure classifier is untouched.

With the dead five gone, four library modules remained and drive-engine was their only consumer — so the bundle is folded into it and `skills/drive-planner/` is deleted. Keeping it as a declared skill meant shipping a retired non-invocable directory to every container and through the skill-review gate on every change, for code with one importer that lives next door. The fold also removes a latent hazard: drive-planner's `precheck.py` shared the bare module name `precheck` with flight-assist's, and the sweep put both bundles on `sys.path` — flight-assist won by insertion order. `tests/test_drive_planner_sweep_precheck.py` carried a `_load()` helper working around exactly that collision.

The skip store keeps its `/workspace/state/drive-planner/` directory and `DRIVE_PLANNER_STATE_DIR` env var. The name is now deployed state, not a reference — renaming either would strand the skips already on disk, and that is a migration, not a side effect of moving code. Both the module and `state-schema.md` say so.

**Prose.** `#180` fixed the two contracts the policy reviewer named and deliberately left the module-level narrative for this issue. Swept: `fetch_events.py` described "the recheck poll" as a present-tense reader of the block description (the field survives because `scan` reads the legacy marker off it and `exclude_drive_block_events` filters on it — not because a poll re-evaluates it); `scan.py`'s marker comment described drive-planner stamping the marker into blocks it creates, when nothing has written it since #156 — the regex is a reader of blocks left on the calendar, frozen by what is deployed rather than by a writer. `state-schema.md`'s Calendar-as-State section documented `block_props`' full record — `v`/`b`/`a`/`o`/`d`/`al` — whose only reader (`block_props.parse_block`) is now deleted; `block_codec` recognizes a legacy block by its marker plus the *presence* of the `<!--dp:-->` comment and never decodes the payload, so those keys are inert bytes on deployed events. The section now says that instead of documenting a live format.

Four modules in sibling bundles named deleted code as their caller and were corrected: `travel-core/trip_origin.py` and `travel-core/SKILL.md` ("drive-planner's sweep precheck imports this module"), `flight-assist/google_calendar_client.py` ("the drive-planner apply / fetch paths"), `flight-assist/airport_block.py` (a "self-contained sibling of drive-planner's `block_props.py`" — the #90 decision is kept as history, the live twin claim is not), `flight-assist/maps_client.py`.

The `drive-planner` name survives where it is accurate: the frozen on-calendar marker literal, and history ("legacy drive-planner blocks", "the retired planner"). drive-engine still leaves those blocks for the operator (`_MANAGED_LEGACY` is empty); once none remain, `scan._MARKER_RE` and `block_codec`'s legacy branch are dead code and can go.

**Surface sync:** deleted `skills/drive-planner/`; moved `scan.py`, `fetch_events.py`, `skip_state.py`, `home_address.py`, `state-schema.md` to `skills/drive-engine/`; renamed their tests to the `test_drive_engine_*` convention; dropped the entry from `.tessl-plugin/plugin.json`, the row + script line from `README.md`, and the execution environment + `extraPaths` mentions from `pyrightconfig.json`; removed the now-needless `_on_path("drive-planner")` from `reconcile_sweep.py` and `skip_drive.py`.

## 0.2.48 — 2026-07-16

### Removed — the retired `drive-planner-recheck` skill

`#156` retired `drive-planner-recheck` when the unified drive-engine took over drive blocks: its `cadence` was removed, so it never fires, and it is `user-invocable: false` + `disable-model-invocation: true`, so nothing can reach it. Unlike `drive-planner` — which stays declared because drive-engine's reconcile sweep imports its meeting-detection modules as a library — nothing imports `drive-planner-recheck`. It was a directory the registry shipped to every container for a skill that cannot run and cannot be called.

The skill-review gate had been saying so. Its `SKILL.md` scored 77% against the 85% threshold, marked down for lacking positive trigger terms and a `Use when...` clause — which a retirement notice correctly does not have. The reviewer's own first suggestion was to remove it from the registry rather than keep tuning a description whose purpose is to never match.

It surfaced during `jbaruch/nanoclaw#638`: that migration made a two-line credential fix inside the directory (`CalendarFetcher.from_env()` -> `CalendarFetcher()`, since `from_env` no longer exists), and the changed-skills loop reviews any skill whose files move. A required fix in a dead skill blocked the publish. Removing it is the fix; scoring the notice higher would only defer this.

`tests/test_drive_planner_recheck.py` stays — despite the name it exercises `skills/drive-planner/recheck.py`, part of the kept library, not the removed skill.

Its removal orphaned two surfaces inside the still-shipped `drive-planner` library, both of which named it as their reader/caller. `state-schema.md`'s Writer/Reader contract said "the recheck poll" reads blocks back and applies suppression patches; `apply.py`'s `suppress` mode documented itself as "invoked by the recheck SKILL.md". Both now state what is true: the reader is gone, `suppress` is uninvoked, and the block format remains the contract for blocks already on the calendar. The wider question — drive-planner ships entry points (`apply.py create` / `remove`) whose only caller was its own disabled sweep, and prose across the kept modules still describes the removed poll in the present tense — is `#181`, since it is `#156` cleanup rather than a prerequisite here.

**Surface sync:** removed `skills/drive-planner-recheck/` and `tests/test_drive_planner_recheck_precheck.py`; dropped the entry from `.tessl-plugin/plugin.json`, the row from `README.md`, and the execution environment + `extraPaths` mentions from `pyrightconfig.json`.

### Fixed — the Gmail freshness fallback no longer puts raw email in the session

`nightly-travel-sync` Step 3's `stale` branch had the AGENT call a Gmail tool
and hand-build the JSON array for `filter-tripit-bookings.py`. That put raw
third-party subjects straight into the session transcript — the exact hole
`/workspace/group/nanoclaw-poison-defense.md` exists to close after the
2026-04-24 incident, where invisible-Unicode padding in a marketing preview
killed a maintenance session. The new `scripts/fetch-tripit-emails.py` fetches
AND sanitizes in-container and prints only `{id, from, subject, date}`; bodies
and snippets are never projected, because nothing downstream reads them and
every field omitted is untrusted text the agent never sees.

- **It was also already broken.** The step told the agent to discover the tool
  via `COMPOSIO_SEARCH_TOOLS(query="gmail fetch emails")`, which returns a
  recommended PLAN, not a callable slug (nanoclaw#649). It never screamed
  because the branch only fires when the schedule goes stale (>7 days).
- **Fails closed, loudly.** A missing sanitizer aborts before any fetch —
  emitting unsanitized output would defeat the point of the script. Gateway
  (401) / tier (403 `access_restricted`) exit 2 with an actionable diagnostic,
  the same split as `reconcile.py`'s `{"error": "gateway"|"tier"}`. A failed
  Gmail call exits 1 and emits NOTHING: this probe reports "no booking found"
  by printing an empty array, so a partial list would read as a false silence —
  the one outcome the step exists to prevent.
- **`format=metadata`, not `full`.** The probe reads only the subject, so
  bodies are never pulled over the wire at all.
- **Bounded, no pagination.** One `list` capped at 100 (`gmail-ops` refuses to
  paginate — the unbounded-crawl shape from nanoclaw#656), then one `get` per
  stub.

### Fixed — a bounded fetch that admits its bound

The cap above can hide a confirmation behind a burst of TripIt Pro alerts or
marketing, because the query is sender-scoped. Left there, that is not just a
missed alert — it feeds back. The window is `after:<schedule mtime>`, so a
missed confirmation leaves the schedule stale, which WIDENS the window, which
makes the next truncation likelier: stale -> wider -> more truncation ->
staler, converging on permanently broken with nothing ever surfaced. It is
nanoclaw#171's shape — a partial view read as the whole one — wearing different
clothes.

- **The signal is the fix; the cap is only a mitigation.** Any cap can be
  exceeded, so raising it cannot close this. When the list returns full, the
  script exits `3` (`EXIT_TRUNCATED`) with a `WINDOW_TRUNCATED` marker on
  stderr, and SKILL.md Step 3 reports the blind spot instead of applying its
  silent-on-zero rule. Silence is correct only on exit 0, where the whole
  window was actually seen. The cap did go 25 -> 100, so the signal stays the
  rare exception rather than routine noise.
- **stdout is still emitted on exit 3.** The rows found are genuine and a match
  among them is still a match; what changes is that a zero count proves
  nothing. `filter-tripit-bookings.py`'s stdin contract is untouched — it stays
  the authority on what a booking is, which is also why the confirmation prefix
  was NOT pushed into the Gmail query: `subject:` phrase-matches fuzzily, so a
  miss there would become a false silence server-side, where the filter could
  never see it.
- **Detected as `len(stubs) >= MAX_RESULTS`.** Gmail signals "more exist" with
  a `nextPageToken`, but `gmail-ops.list_messages` returns the stub list and
  drops it — and that helper is admin's, not this plugin's to change. A full
  page over-reports by exactly one case (a window holding precisely the cap and
  no more), which is the safe direction: a spurious note costs the operator a
  line, a missed one costs a booking.
- **`set -o pipefail` is now required** in Step 3's command, and the stderr
  marker backs the exit code up: without pipefail the filter's exit 0 masks the
  fetch's code, and this probe must not go quiet because a shell option was
  dropped. Either signal is sufficient.
- **Tested both directions**, since the whole point is that they are
  distinguishable: a truncated window and a genuinely-empty one both make the
  real filter report `count: 0`, and the test asserts that — then asserts the
  exit codes differ anyway.

### Changed — a co-loaded dependency on nanoclaw-admin, for Gmail only

`fetch-tripit-emails.py` imports `google-rest.py`, `gmail-ops.py`,
`gmail-message.py` and `sanitize-email-body.py` from admin's heartbeat skill
over the `tessl__heartbeat` mount, exactly as `nanoclaw-orders` does. Gmail is
not this plugin's domain, and re-implementing RFC822 MIME parsing plus the
poison sanitizer here would be strictly worse than depending on the one tested
copy. Calendar is the opposite case and stays self-contained.

Admin's helpers are unavailable to this repo's CI (it checks out one repo) and
vendoring them is forbidden, so the tests inject doubles via
`NANOCLAW_HEARTBEAT_SCRIPTS` — the same override the script uses for a dev
clone. The doubles are not re-implementations: the `gmail_message` double
asserts it was handed an untouched native Gmail resource (raw RFC822 header
list, base64url body in a nested MIME tree, `internalDate` as an epoch-ms
string), so a regression that pre-digests the resource fails the test. Parsing
that shape is admin's contract, tested in admin. Transport is real `urllib`
against a local `http.server`.

### Changed — Google Calendar over native REST, no credential in the container

This plugin held the last Composio Google transport in the fleet. `nanoclaw#638`
retired it: calendar access is now the native Google Calendar v3 REST API,
brokered by OneCLI's TLS-MITM gateway, which injects the OAuth `Bearer` on the
wire and refreshes it. `nanoclaw-admin@0.1.458` and `nanoclaw-orders@0.1.20`
already shipped and live-verified this shape; this is the same move.

- **`composio_client.py` → `google_calendar_client.py`.** `ComposioClient` →
  `GoogleCalendarClient`, `ComposioError` → `GoogleCalendarError`. The single
  `POST /tools/execute/{SLUG}` becomes the five real endpoints (`calendarList`,
  `events` list/insert/patch/delete), so the action-slug constants are gone.
  Still stdlib-only, still one client per process, still the `byair_client` /
  `maps_client` shape.
- **No credential, anywhere.** `from_env`, `api_key`, `user_id`,
  `COMPOSIO_API_KEY`, `COMPOSIO_USER_ID`, `COMPOSIO_BASE_URL` and the
  `x-api-key` header are all deleted; construction takes nothing. A test pins
  that no `Authorization` header is ever sent — one would mean a credential
  leaked into the container. `GOOGLE_CALENDAR_API_BASE` remains as a test-only
  base override.
- **`.status_code` survives.** Composio faked the upstream status inside an
  HTTP-200 `successful: false` envelope; Google returns a real one. The envelope
  dies, the attribute does not — callers gate idempotency on `404 = already gone
  = success` in five places, and all five still read it, now from the actual
  HTTP status.
- **Two config failures are no longer per-op failures.** A 401 raises
  `GatewayNotInjecting` and a 403 + `access_restricted` raises
  `TierAccessRestricted`, neither a `GoogleCalendarError` — so the per-op
  handlers that collect a failure and retry next cycle can't swallow them and
  defer forever. `reconcile.py` reports them as `{"error": "gateway"}` /
  `{"error": "tier"}`, replacing the `{"error": "credentials"}` exit for a
  credential that no longer exists.

### Changed — create/patch bodies are native event resources

The Composio tool schema had leaked past the transport into four arg builders
(`calendar_reconcile`, `calendar_apply`, `block_props`, `airport_block`) and
their consumers. Flat `start_datetime` + `event_duration_hour` /
`event_duration_minutes` + `timezone` is a Composio invention with no native
equivalent — Calendar takes nested `start` / `end` objects — so it went with it.

- **Three workarounds deleted, not ported.** `_create_time_fields`' offset →
  `Etc/GMT±N` synthesis (#82/#83) existed because Composio's adapter ignored a
  bare `start_datetime`'s offset and re-read the wall-clock as UTC; Calendar
  honours the offset, so the create sends `dateTime` and no `timeZone`.
  `exclude_organizer: true` (#158) suppressed the `needsAction` self-attendee
  Composio injected; `events.insert` adds no attendees, so there is nothing to
  suppress. `_find_window` reconstructed each block's end from its duration
  fields; the end is now simply read off `end.dateTime`.
- **#131's mechanism is gone; its behaviour is kept.** The drive codecs still
  render `dateTime` in the block's IANA zone and declare that zone in
  `timeZone`, so the two agree and the event is self-describing — but the offset
  alone now fixes the instant, so a mismatch could no longer land a block 6h
  early. An unresolvable zone (a raw `+01:00`) is dropped rather than forwarded:
  Calendar requires an IANA name and would reject the offset form.
- **A `_min_end` floor is now explicit.** The flat contract clamped to
  `max(minutes, 1)` for free; nested start/end has to say so, or a zero-length
  block renders as an unclickable hairline.
- **#158's colour half is unblocked.** It was deferred because no Composio
  action exposed `colorId`. Calendar does, so it is now merely unimplemented.

### Fixed — two defaults Composio was quietly supplying

Both would have been silent behaviour changes: no test failed on either, and
neither surfaces as an error at runtime — they just produce wrong answers.

- **`drive-planner/apply.py` now asks for `singleEvents`.** Its three finds
  never passed it, because Composio's `FIND_EVENT` slug defaulted it on;
  `events.list` defaults it OFF. `scan.py` classifies events fetched WITH the
  expansion, so the `meeting_id` in the skip store is an instance id, and
  `_remove_mode` / `_resolve_candidates` match fetched ids against exactly
  those. Unexpanded, a recurring meeting comes back as a master whose id
  matches nothing — a daily standup would have silently failed to skip.
- **`apply.py` catches the config errors at its boundary.** A 401 used to
  arrive as `HTTPError`, which `_WRITE_ERRORS` caught, so the agent got its
  documented `{"error": ...}` JSON. `GatewayNotInjecting` is a `RuntimeError`
  and would have sailed past that tuple into a traceback — which the agent,
  parsing only this script's output, reads as "no result at all".

### Changed — one calendar transport, not three

`drive-planner/fetch_events.py` carried a second, self-contained Composio
transport — its own POST, auth headers, envelope handling, pagination loop and
`FetchError` type — making the same calls to the same API as flight-assist's. It
now delegates to `GoogleCalendarClient` and keeps only what is genuinely
drive-planner's: the window contract and the projection to what `scan.py` reads.
`CalendarFetcher.from_env()` → `CalendarFetcher()`.

- **`FetchError` deleted.** Nothing outside the module caught it, and its
  "successful response with no event list" guard only existed because Composio's
  envelope could carry the list under any of several keys, or none — a mis-read
  that looked exactly like an empty calendar and would have made the sweep a
  silent no-op. `events.list` has one shape.
- **The shape-guessing goes with it.** `_items` / `_find_event_containers` /
  `_extract_events` walked tuples of candidate containers (`calendars`,
  `event_data`, `event_data.event_data`, `response_data`, `items`) because
  Composio's shape varied per action and drifted between toolkit versions. Both
  list surfaces put their rows in `items`, and `find_events` merges its pages
  into that same shape, so each is a one-line read.
- **#171 stays fixed.** `maxResults=2500` + the `nextPageToken` drain + the
  `_MAX_PAGES` bound moved into the client intact, and are tested there once for
  every caller rather than twice.

### Changed — `check-env.py` no longer reports on retired variables

`composio_key_present` / `composio_user_present` described env vars that no
longer exist. They are removed rather than replaced: calendar access has no
env-var precondition any more — the gateway either reaches this process or it
does not, and only a real API call can tell. Synthesizing a flag from
`HTTPS_PROXY` would be a guess about orchestrator wiring this skill does not
own, and `check-env` makes no network call by contract. `reconcile.py` is where
that failure surfaces, actionably. A test pins that a stale `COMPOSIO_API_KEY`
left in the env cannot resurrect a flag.

## 0.2.47 — 2026-07-16

### Changed — drive blocks are Busy, not Free

A drive block showed as Free (`transparency: "transparent"`, the Epic #59 §5 /
#90 decision), so scheduling tools happily booked meetings on top of time the
operator is in a car. Blocks are now created Busy (`transparency: "opaque"`).

- **Create-time only.** `GOOGLECALENDAR_CREATE_EVENT` is the only Composio action
  that accepts `transparency` — `PATCH_EVENT` has no such param (verified against
  the live toolkit: 14 params, none of them `transparency`). So the shift path
  cannot flip an existing block's busy-ness, and a block's Free/Busy state is
  fixed when it is created. `UPDATE_EVENT` does accept it, but it is a full PUT
  replacement with no `exclude_organizer`, which would risk re-opening the #158
  self-invite regression — the shift path stays on PATCH.
- **No drift check.** With no way to patch transparency, the reconcile does not
  detect or heal a Free block; there is nothing it could do about one. Existing
  Free blocks were drained by a one-time delete of future blocks, letting the
  next sweep recreate them Busy.

## 0.2.46 — 2026-07-14

### Changed — drive-engine notifies only on what the operator can act on

The sweep woke the operator on every write (`total_writes > 0`), so routine
traffic re-times — a few seconds of jitter every 30 minutes — spammed the chat.
It now wakes ONLY for two things: a new MEETING drive, or a MATERIAL drive-time
change. Everything else applies silently.

- **Material re-time alerts.** A shifted block alerts only when its routed drive
  duration changed by ≥10% (either direction) AND by at least the reconcile's patch tolerance (`_BASELINE_SHIFT_TOLERANCE_SECONDS`, 2 min) — `reconcile.material_update_delta`. The two thresholds are deliberately aligned: a smaller drift schedules no patch, so it never reaches `apply_plan` to be notified — an alert must never promise a heads-up the reconcile can't deliver. The message is actionable: "leave {N} min sooner/later for your {meeting} at {time}" (sooner when the drive got longer). Sub-threshold shifts still patch the calendar, silently.
- **Skippable meeting-add notifications.** A new meeting drive is announced and can be declined: the operator replies "skip" (or "skip 1", "skip 2 and 3" when several were added — a local index, never an internal id). `skip_drive.py` resolves the name to the meeting, deletes its blocks, and records a skip so no sweep recreates them. Airport drives to/from a flight are NOT announced — a flight is not skippable. The `skip-state.json` writer/reader contract (`drive-planner/state-schema.md`, `skip_state.py`) is updated to name drive-engine as the live writer (via `skip_drive`) and reader (via the sweep) — the drive-planner sweep that used to write is retired; `skip_state.py` still owns the schema. `skip_drive`'s CLI boundary always emits a structured JSON result (never a bare traceback) with meaningful exit codes.
- **Silent by design.** Removes, airport-drive adds, converts, and routine re-times no longer notify.
- The drive-engine SKILL is now an action router (report sweep changes / skip a drive / flag a wrong block). `reconcile_sweep.build_sweep_payload` owns the wake gating and per-meeting grouping.
- Confirmed the engine only ever creates a drive for a meeting with a real, routable address: `scan` filters a location-less or virtual meeting to `filtered`, and `meeting_source` drops legs whose location won't geocode or whose drive is implausible.

## 0.2.45 — 2026-07-14

### Fixed — unpaginated calendar fetches truncated the window → drive-engine duplicate storm (#171)

Both Composio calendar-fetch primitives returned only the first page of a time
window, so every caller reconciled against a partial view. `ComposioClient.find_events`
(`GOOGLECALENDAR_FIND_EVENT`) sent no `maxResults` and never followed `nextPageToken`,
capping at ~10 events; the drive-engine sweep built `current_blocks` from that truncated
fetch, so its G1 dedup was blind to the ~100 `Drive:` blocks it had already created and
re-created them every sweep — 40+ surplus duplicates on the live `primary` calendar and
growing. The same truncation also fooled a diagnostic into reporting "0 Drive blocks" off
the capped fetch. `CalendarFetcher.fetch_window` (`GOOGLECALENDAR_EVENTS_LIST`, the meeting
scan path) had the same latent bug, silently capping at Google's default 250.

Both now drain the complete window: `maxResults=2500` (Google Calendar's max page size)
returns any realistic window in a single call, and a `nextPageToken` loop — bounded at 40
pages so a non-clearing token can't spin forever — is the safety net for a window that
still exceeds one page. Verified against the live NAS toolkit: both actions honor
`maxResults` and `pageToken` (page 2 disjoint from page 1, token clears on the last page).
`find_events` merges the pages back into the same `event_data.event_data` shape a one-page
response has, so every caller's own event extraction is unchanged. Once `current_blocks` is
complete, reconcile G1 sees the surplus and self-heals — deleting the duplicate pile with
no manual cleanup.

## 0.2.44 — 2026-07-14

### Fixed — drive-engine routing storm hung the sweep (#172)

The airport-endpoint fix in #165 (`"STN airport"` instead of a bare `"STN"`) made
airport legs geocode *successfully* — but many airports return Google Distance
Matrix `ZERO_RESULTS` and fall back to TomTom's three-call geocode+geocode+route
chain at up to 10s each. Uncached and unbounded, a multi-leg itinerary routed for
minutes on every sweep (observed live: 80+ `maps.googleapis.com` + 30+ TomTom calls
in 15 minutes), and the agent container that ran it was SIGKILLed at its timeout
with no output. Two fixes:

- **Per-sweep route memoization.** `reconcile_sweep.make_route` wraps the maps
  client in a `(origin, destination)` cache shared by every airport and meeting
  leg, so an endpoint that appears in several legs (a departure destination that is
  also a transfer origin) is routed once per sweep, not per leg — the caller-level
  caching `MapsClient`'s own docstring prescribes. A failed route caches `None` so a
  dead endpoint isn't re-attempted (and re-failed-over) every leg.
- **Wall-clock routing budget, enforced at the route call.** `make_route` serves a
  cached (origin, destination) pair even past the deadline, but refuses to START a
  new cache-miss route call once the deadline passes — raising `PlanBudgetExceeded`
  before entering the provider-fallback chain. Enforcing at the call (not per leg)
  means a single leg's fallback chain can't push the sweep past its budget, while
  already-routed legs still plan. The exception propagates out of `build_reconcile_plan`
  unwound (never a partial `desired` set — which would read as orphaned blocks and
  get deleted, the exact churn #164 fixed) and is caught at `main()`'s outer boundary,
  covering meeting-side and airport-side routing alike, so the sweep skips the cycle
  cleanly (`wake_agent: false`) instead of being killed mid-route before it prints JSON.
- **Sweep-budget gate before the post-routing network work.** After meeting routing,
  before the current-block fetch and airport resolution, the sweep skips cleanly if
  the whole-sweep budget is already spent — so a meeting leg that returned well past
  the deadline can't drag more network work toward the host precheck kill. The gate
  uses the sweep budget (not the tighter routing deadline) so cheap cache-served legs
  aren't needlessly abandoned.
- **Tightened per-call timeout for the sweep's maps client** (4s, from the shared
  10s default) so a single `travel_time` — one Google call plus up to three
  sequential TomTom fallback calls — finishes within the margin between the routing
  deadline and the host's ~33s precheck kill.
- **No PII in the budget-exceeded log.** The `PlanBudgetExceeded` message no longer
  includes the raw origin/destination (which can be a home address or live GPS fix),
  since it is printed to stderr.

## 0.2.43 — 2026-07-13

### Fixed — drive-engine airport legs never produced ('origin route failed') (#165)

Every airport drive leg failed routing while meeting legs built fine. Two causes:

- **Past/completed segments weren't filtered.** The engine built airport legs for
  *every* flight in the schedule, including a trip that already flew — so it tried to
  route the operator's current home (Tennessee) to a departure airport abroad that flew
  last week (London Stansted). There's no driving route across the Atlantic, so Maps
  returned nothing and the leg "failed." The pipeline now skips any leg whose drive
  window is already in the past (`anchor`/`window_end` < now) before routing it — a
  completed trip correctly produces no drives, cleanly, with a `past, skipped` diagnostic
  instead of a route-failure.
- **Airport endpoints were bare IATA codes.** Airport legs fed the maps route a bare
  3-letter code (`"STN"`), which Distance Matrix can't reliably geocode (meeting legs
  route because they carry full addresses). They now use the geocodable form the
  `MapsClient` documents (`"STN airport"`), so future flights route reliably instead of
  the airport half being silently dead while meeting drives work.

## 0.2.42 — 2026-07-13

### Fixed — drive-engine duplicate storm + apply timeout (#164)

Every ~30-min sweep was killed mid-write past the host's ~33s precheck budget and left
a fresh set of duplicate drive blocks — the calendar accumulated 10+ copies of each leg.
Two root causes, both fixed:

- **Update churn (the storm).** The maps route returns a slightly different
  `baseline_seconds` on every sweep (traffic-recomputation jitter — 1–46s swings for an
  unchanged leg). `_needs_update` compared it exactly, so every sweep re-"shifted" all
  ~15 legs, and each shift was a **recreate-then-delete**: the ~33s kill landed after the
  recreates but before the deletes, duplicating every leg every cycle. Fix: (1) an update
  now only fires on a **meaningful** baseline change (≥ 120s; sub-2-min jitter is ignored),
  and (2) a shift is a single **in-place `PATCH_EVENT`** of the same event — no second
  event is ever created, so a kill can no longer leave a duplicate. Verified live that a
  PATCH shifts a block's start + description to the correct instant without a duplicate.
- **Unbounded apply.** `apply_plan` now takes a wall-clock **budget** (the sweep gives it
  whatever of a 27s budget the fetch/plan phase left) and stops starting new write ops
  once it elapses — deletes first (so a duplicate backlog drains), then creates/converts/
  updates — returning a clean payload with a `deferred` count instead of being killed
  mid-write. Deferred ops drain on the next sweep; the reconcile is idempotent, so
  resuming never duplicates. The existing duplicate backlog is cleaned up by the engine's
  own G1/G7 delete path over the next few sweeps.

## 0.2.41 — 2026-07-13

### Added — flight-assist trip-window defense-in-depth (#147)

The host now owns the primary control: a pre-spawn gate (jbaruch/nanoclaw#754) that
reads the group `travel-db.json` and does not spawn the flight-assist container outside
a trip window, so the `*/2` cadence costs nothing off-trip. This adds the plugin-side
belt-and-suspenders: `trip_window.evaluate_trip_window` reads the **same** file with the
**same** window (`(start − 24h) ≤ now < (end + 24h)`, union of trips) and the **same**
asymmetric fail semantics (absent → out of window; corrupt / unreadable → fail open so a
bad file never blinds an active trip). The precheck consults it first and, if a container
was spawned off-window anyway, exits before any byAir call with
`{"wake_agent": false, "data": {"reason": "outside_trip_window"}}`.

`travel-db.json` (owned by `check-travel-bookings`, written nightly) is the single source
of truth for active trips — no second trip store. As a cross-plugin non-owner reader,
`trip_window` gates on `schema_version` (`coding-policy: stateful-artifacts`): a version
other than the accepted `1` is no-usable-state and **fails open**, so a cross-pipeline
schema bump defers to the host gate instead of blinding a trip. The trip-window gate is
trip-level and stacks with the existing flight-level `_POLL_HORIZON_HOURS = 24`. Documented
in `skills/flight-assist/state-schema.md` and the owner's reader contract in
`skills/check-travel-bookings/state-schema.md`; `FLIGHT_ASSIST_TRAVEL_DB` overrides the
path for tests.

## 0.2.40 — 2026-07-13

### Fixed — drive blocks show as accepted, not an unconfirmed invite (#158)

Drive blocks were created with a `needsAction` self-attendee, so they rendered as
pending invites the operator had to RSVP to. The unified engine's create args now pass
`exclude_organizer: true`, which stops Composio from injecting the connected user as an
attendee — the block has no attendees and shows as a plain accepted event. Verified
against the live Composio toolkit (create with `exclude_organizer` → zero attendees).

The companion ask — a distinct calendar colour (Tangerine / `colorId: "6"`) — is deferred:
no Composio Google Calendar action (create / update / patch / quick-add) exposes an event
colour field in the deployed toolkit, so it cannot be set through the current write path.
Tracked separately, blocked on the Composio retirement / workspace-MCP migration.

## 0.2.39 — 2026-07-13

### Fixed — flight-assist day-before notification: wrong flight code + hallucinated airport (#159)

Two independent defects in the day-before notification, both trust-eroding though the
underlying tracking was correct:

- **Bug 1 — operating designator shown instead of marketing.** For a codeshare, byAir's
  two endpoints describe the same flight from opposite sides: `list_trips` (what
  `sync_tripit` seeds from) carries the marketing code `DL4908` with `operator.code` =
  `9E4908`, while `get_flight` (the poll) carries the operating code `9E4908` at top level
  and exposes the marketing code only in free-text `note`. The precheck poll was
  overwriting the sync-seeded marketing code with `get_flight`'s operating designator every
  cycle. `code` is now preserved across polls as a seed-time identity field (alongside
  scheduled times / airport ids), and the persisted snapshot is realigned to it, so no
  reader can surface the operating code. No structured marketing field exists in
  `get_flight` to read, so preservation — not re-extraction — is the fix. Because
  preservation would also keep an already-corrupted value, `sync_tripit` now repairs the
  `code` on every retained flight from `list_trips` (the marketing-code authority) each
  daily run, healing records poisoned by pre-fix polls.
- **Bug 2 — arrival airport free-typed.** State carried only numeric `dep_airport_id` /
  `arr_airport_id`, so the LLM composer invented "Stansted" (the trip's origin) for a
  JFK→Nashville arrival. The poll now captures the resolved airport `code` + `name` off the
  byAir payload it already fetches (`depAirport` / `arrAirport`) into `last_snapshot`, and
  the compose step renders the airport strictly from those fields — never free-typing from
  an id.

State schema bumped **v6 → v7** for the new `last_snapshot` airport slice and the realigned
`code` semantics. The airport fields are byair-owned and repopulated by the next poll, so the
owner-side `state.py:_migrate` v6→v7 step is a schema_version bump only (no backfill).

## 0.2.38 — 2026-07-13

### Changed — wire the drive-engine's remaining designed features live (#156)

Three refinements the earlier live cutover built but left unwired are now active in
`reconcile_sweep.py`:

- **R2 byAir ∪ TripIt union** — the sweep now also parses the travel-schedule's
  `Flight` segments (new `tripit_flights.py`, a bounded `<DEP> to <ARR>` iCal parse)
  and unions them with the byAir records, so a flight tracked by only one source
  still produces legs. `build_plan` gains `tripit_flights`.
- **R5 identity flight-mask** — flight events are now dropped from the meeting scan
  input by `flight_mask.is_flight_event` (identity only, never time overlap, so a
  ground meeting overlapping a redeye window survives), and `scan` runs with an
  empty flight context.
- **V3 boarding-block presence** — trivial-leg suppression is gated on a real
  boarding block on the byAir calendar; absent one, the trivial airport drive is
  kept (R6 — never silently drop the only "head to the gate" signal). `build_plan`
  gains `boarding_present`.

## 0.2.37 — 2026-07-13

### Changed — drive-engine goes live; drive-planner and flight-assist airport drives retired (#156)

The unified drive-engine now WRITES: `reconcile_sweep.py` plans airport drives from the byAir itinerary and meeting drives from the calendar (reusing drive-planner's proven `scan` for meeting detection), diffs both against the primary calendar in one reconcile, and applies the result — creating / updating / deleting its own unified-codec blocks. Meeting drives gain travel-awareness (a drive whose routed time is implausible — the operator is abroad while the meeting is at home — is suppressed, not invented) and render in the meeting's local timezone. The engine touches ONLY its own blocks (`managed_legacy` empty): existing drive-planner blocks are left for the operator to remove.

The two legacy engines are retired: flight-assist's `scripts/reconcile.py` no longer runs the airport-drive pass (a dormant `airport_drive: {"status":"retired"}` marker remains), and `drive-planner` / `drive-planner-recheck` lose their cadences and become non-invocable library skills (their `scan` / `fetch_events` / `skip_state` modules are imported by the engine). New modules: `calendar_apply` (the write path, atomic convert/update with rollback), `meeting_source` (meeting legs + travel-away suppression). `DesiredBlock` gains a `timezone` field for local-time rendering.

## 0.2.36 — 2026-07-13

### Added — travel-core shared library bundle (#156)

Extracted `trip_origin.py` (TripIt-over-home position/anchor resolution) and `airport_lead.py` (airport clearance / post-arrival buffer policy) out of `flight-assist` into a new `travel-core` skill bundle, so flight-assist, drive-planner, and the incoming unified drive engine import one source of truth instead of reaching cross-bundle into flight-assist for shared logic. `travel-core` is a background library skill (`user-invocable: false`, `disable-model-invocation: true`) — no workflow, just hosted modules the consumers put on `sys.path` via the runtime-mount / dev-sibling pattern already used for `maps_client`. Consumers (flight-assist `precheck` / `airport_drive_reconcile` / `airport_drive_inputs`, drive-planner `precheck`) and the pyright execution-environment config were repointed accordingly; behavior is unchanged.

### Added — unified drive engine foundations (#156)

First two pure, deterministic modules of the leg-based drive engine that will replace the flight-assist / drive-planner two-engine patchwork. `flight_identity.py` unions the byAir and TripIt flight sources on a canonical identity of `(dep_airport, arr_airport, scheduled_dep_instant ± tolerance)` that excludes the designator — so a single physical flight tracked under two byAir ids / codeshare codes (FR7382 / MW7382) collapses to one flight instead of storming the calendar with duplicate drive blocks. `chain.py` classifies each consecutive flight pair (overnight / different-airport transfer / same-airport connection) and plans the ground legs a trip yields: same-airport connections default to airside silence and interior connections are suppressed, so a drive is never routed to an airport the operator reaches by a prior flight.

### Added — drive-engine skill in read-only preview mode (#156)

The unified engine now ships as the `drive-engine` skill, running in read-only preview (shadow) mode on a ~30-min precheck. It assembles the airport drive legs the byAir itinerary needs (`position_at` origins, §B anchors, GPS-imminence overlay, connection suppression, trivial-leg suppression), diffs them against the primary calendar's current drive blocks (recognizing the new codec and both legacy fadrive/dp shapes), and logs the add/move/delete/replace plan — storm dedup, legacy convergence, orphan deletion — WITHOUT touching the calendar. `wake_agent` is always false; the precheck fails closed to a no-wake payload on error. This is the validation harness to confirm the plan against the live calendar before the write path is enabled; the two legacy engines stay in place until it is validated.

## 0.2.35 — 2026-07-09

### Fixed — drive-planner: catch flight events by content, not only by schedule time (#85 follow-up)

The 0.2.32 flight filter matched a calendar event to a scheduled flight by time overlap alone, and that was defeated by garbage in the calendar. Google "events from Gmail" auto-created three copies of one flight ("Flight to Nashville (DL 4908)"); two carried a corrupted timezone (span 19:55–22:01Z) that ended before the true flight window (22:59–01:46Z) began, so they missed the overlap, slipped through as ordinary New-York meetings, and drew a Stansted→New-York transatlantic "drive" (`ALL_PROVIDERS_FAILED`). The filter wasn't broken — the time-only match was too narrow for duplicate, time-corrupted input.

`scan()` now treats an event as air travel by **any** of three independent signals: (1) time overlap with a scheduled flight window (the 0.2.32 behavior); (2) a flight-template summary — `Flight to …` or a `✈` prefix — which is intrinsic to `scan` and needs no schedule, so it catches the corrupted duplicates; (3) an IATA flight designator (e.g. `DL 4908`) in the summary matching a scheduled flight's identity (new `trip_origin.flight_summaries()`), which survives a corrupted time. The precheck loads windows and summaries in one schedule read (`_build_flight_context`). The template signal is deliberately narrow (`Flight to `/`✈` prefix) so a real ground meeting is not silently withheld; a Gmail restaurant reservation ("Reservation at Fletchers House") stays a valid drive target. The filtered reason generalizes to "air travel — flight event".

## 0.2.32 — 2026-07-09

### Fixed — drive-planner: filter TripIt flight events out of ground-meeting classification (#85)

TripIt syncs each flight segment onto the primary calendar as its own event ("Flight to Nashville (DL 4908)", location = an airport). The sweep fetches every primary event and `scan.py` classified these flights as routable ground meetings, so the precheck tried to drive between airports — Stansted hotel → JFK for a layover, an ocean apart. Ocean routes return `ZERO_RESULTS` and woke the agent with "Couldn't compute drive time"; layover bridges surfaced as "doesn't fit the gap" noise. This is the unbuilt half of #85: its downstream sanity gates (`MAX_REASONABLE_DRIVE_SECONDS`, bridge>gap) catch implausible *drives* but never stopped a flight *event* from being treated as a meeting; the #122 anchor fix handled the restaurant-origin case but not this one.

`scan()` now takes `flight_windows` — the UTC spans of the TripIt schedule's `Flight` segments (new `trip_origin.flight_windows()`, read from the same `travel-schedule.json` the #122 anchor resolver already loads). Any event overlapping a flight window is bucketed `filtered` ("air travel — TripIt flight segment") and excluded as a routing neighbour, so a flight's airport location never draws a cross-continent drive and never bridges an adjacent real meeting. Matching is interval overlap (the calendar event and its schedule twin derive from the same TripIt data); only timed flight segments produce a window, so a date-only artifact can't suppress a real same-day meeting. Empty windows (no schedule, or the flight-unaware `scan.py` CLI) preserve the pre-#85 behavior — a real meeting is never suppressed. Also fixed a pre-existing duplicate `# 4.` step number in `_classify`.

## 0.2.30 — 2026-07-08

### Changed — consolidate dependency automation on Renovate; retire Dependabot

Both scanners ran after Renovate onboarded (#109), so every upstream release arrived as two PRs and each merge published a version — the github/gh-aw-actions patch stream alone shipped two published bumps in one day (0.82.3 via Dependabot #138, 0.82.4 via Renovate #141) plus a third still-open PR for 0.82.6 (Renovate #145). Removed `.github/dependabot.yml`; Renovate's `config:recommended` already covers both managers Dependabot tracked — `pip` (`requirements-dev.txt`) and `github-actions` — so no coverage is lost. Ported Dependabot's `dependencies` label to `renovate.json` to preserve PR filtering. GitHub-native Dependabot **security** alerts are a repo setting, not this file, and are unaffected.

## 0.2.29 — 2026-07-08

### Changed — renovate: stop proposing CI Python bumps past the container runtime

Renovate's onboarding config treated the CI `python-version` pin as a dependency to chase upstream (its first sweep filed a 3.11 → 3.14 bump, closed unmerged). The pin exists to mirror the NanoClaw agent container's interpreter — `nanoclaw-agent` builds on `node:24-slim` (Debian bookworm), whose python3 is 3.11.2, verified against the live image. Testing on a newer interpreter than production executes would let 3.12+ syntax and stdlib usage pass CI and fail in the container's prechecks. A `packageRules` entry disables renovate's `python` dep updates; the pin moves manually when the container's base image does.

## 0.2.28 — 2026-07-08

### Changed — bump github/gh-aw-actions/setup to 0.82.4 (renovate, PR #141)

## 0.2.27 — 2026-07-08

### Changed — update jbaruch/coding-policy action digest to 759e589 (renovate, PR #140)

## 0.2.26 — 2026-07-08

### Added — Renovate onboarding config (`renovate.json`, PR #109)

## 0.2.25 — 2026-07-08

### Changed — bump github/gh-aw-actions/setup from 0.81.6 to 0.82.3 (dependabot, PR #138)

## 0.2.24 — 2026-07-08

### Changed — bump actions/cache/save from 5.0.5 to 6.1.0 (dependabot, PR #139)

## 0.2.23 — 2026-07-08

### Changed — bump actions/cache/restore from 5.0.5 to 6.1.0 (dependabot, PR #136)

## 0.2.22 — 2026-07-08

### Changed — bump ruff from 0.15.19 to 0.15.20 (dependabot, PR #111)

## 0.2.21 — 2026-07-08

### Changed — bump pyright from 1.1.408 to 1.1.411 (dependabot, PR #137)

## 0.2.20 — 2026-07-07

### Fixed — CREATE wall-clock expressed in the event's timezone arg (`jbaruch/nanoclaw-travel#131`)

Drive blocks landed ~6h early while traveling (live case 2026-07-07: the Fletchers House Rye outbound block sat at 09:15 BST for a 15:45 reservation). PR #87 passed an explicit venue `timezone` to `GOOGLECALENDAR_CREATE_EVENT` but left `start_datetime`'s wall-clock in whatever offset `leg_start` carried (the home −05:00); the Composio adapter ignores the offset and re-reads the wall-clock in the `timezone` arg, shifting the block by the home↔venue delta — invisible at home where the two zones agree. `build_block_args` (drive-planner `block_props.py` and flight-assist `airport_block.py`, same latent bug) now converts `leg_start` to the target zone via a `_wall_clock_in` helper before formatting; an absent or unresolvable `timezone` (raw offset strings — `_extract_timezone`'s `Etc/GMT±N` fallback resolves fine) leaves the datetime untouched, preserving prior behavior. Tests pin the Chicago→London 6h case, the `Etc/GMT±N` fallback, the unresolvable-tz guard, and the airport-block UTC→Chicago case. The deeper question of normalizing internal `arrive_by` to the venue tz at parse time stays open in #131.

## 0.2.19 — 2026-07-07

### Fixed — move drive-planner-recheck sibling imports inside the precheck JSON boundary (`jbaruch/nanoclaw-travel#126`)

`drive-planner-recheck/precheck.py` resolved and imported the co-shipped drive-planner bundle at module import time, before `main()`'s outer-boundary try block — a missing or mis-mounted sibling skill raised `FileNotFoundError` as a raw process crash instead of the scheduled-task JSON contract's `{"wake_agent": false, ...}` payload. The bundle is now resolved lazily via `_ensure_drive_planner_on_path()` (idempotent, same pattern as sync-tripit's `_load_flight_assist`), called from `main()`'s try block and from `evaluate_blocks`; the shared-module imports moved to function scope. A new test patches the resolver to raise and asserts the no-wake payload with exit 0.

## 0.2.18 — 2026-07-07

### Fixed — snapshot readers treat newer flight state as no usable prior state (`jbaruch/nanoclaw-travel#125`)

`state.py`'s non-owner snapshot readers (`read_active_flights_snapshot`, `read_flight_state_snapshot`) raised `StateError` on a `schema_version` above the module's own, contradicting `coding-policy: stateful-artifacts` (a reader seeing a newer record is lagging, not looking at broken state). During a cross-pipeline rollout where flight-assist bumps its schema first, `sync-tripit/precheck.py` — a non-owner reader — hit that `StateError` and emitted `wake_agent:false` with `precheck_internal_error` every poll until the consumer plugin upgraded. The `migrate=False` snapshot path now returns `[]` / `None` (no usable prior state) for newer versions, which degrades to the bounded mtime-based stale-state gate instead of wedging. Owner-side reads stay strict — the owner must never run behind its own state files. `state-schema.md` documents the split; the future-version snapshot tests are inverted to assert the fallback and that the file is never rewritten.

## 0.2.17 — 2026-07-07

### Fixed — harden build-travel-db against malformed travel-schedule input (`jbaruch/nanoclaw-travel#127`)

`build-travel-db.py` handled only a missing `travel-schedule.json`; a corrupt, non-UTF-8, partially-written, or wrong-root-shape file produced a raw traceback instead of the documented Step 4 failure surface. The schedule read now catches `OSError` / `UnicodeDecodeError` / `json.JSONDecodeError` and validates the root is a JSON array of event objects, exiting 1 with a stderr diagnostic that names the recovery path (re-run `refresh-travel-schedule.py`). Tests cover truncated JSON, non-UTF-8 bytes, object root, and array-of-non-objects.

## 0.2.16 — 2026-07-07

### Fixed — document Composio credentials in the README environment contract (`jbaruch/nanoclaw-travel#128`)

The README required-environment table listed only `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`, while `.env.example`, `check-env.py`, and the runtime clients also require `COMPOSIO_API_KEY` and `COMPOSIO_USER_ID` — a fresh install following the README got flight data working with calendar reconciliation and drive-block operations silently broken. The table now lists all four required credentials, a separate optional table covers `COMPOSIO_BASE_URL` and the `TOMTOM_API_KEY` routing fallback, and the `check-env.py` description names everything the script actually checks.

## 0.2.14 — 2026-07-07

### Fixed — trip-aware drive origins: lodging over static home while traveling (`jbaruch/nanoclaw-travel#122`)

The drive planners had no trip awareness: drive-planner routed every meeting leg from the static home (live case 2026-07-07: a UK dinner drew a "leave by 6:16 PM — 39-min drive" block computed from the Tennessee residence via a mis-geocoded venue), and flight-assist's origin ladder fell back to the same static home when the live-location snapshot was stale. New shared `skills/flight-assist/trip_origin.py` resolves the anchor from `travel-schedule.json` (TripIt truth): off-trip → home, unchanged; on an active Trip → the `location` of the latest Lodging event (check-in or check-out) within the trip span at or before the anchor time, which also resolves check-out→check-in gaps; before the trip's first lodging → the Trip's own location, else unresolved — home is never used mid-trip. drive-planner's scan resolves the anchor per meeting (`anchor_for`) so a 14-day sweep window can span on- and off-trip meetings; an unresolved anchor surfaces the leg as `unplannable` instead of routing it. flight-assist's time-to-leave origin and drive-home destination use the trip-aware effective home per cycle.

### Fixed — correct stale plugin-home claim in `home_address.py` docstring (`jbaruch/nanoclaw-travel#122` comment)

The docstring claimed drive-planner lives in `nanoclaw-trusted`; it lives in this plugin. The `trusted-memory` ownership references (which genuinely point at `nanoclaw-trusted`) are untouched.

## 0.2.13 — 2026-07-07

### Fixed — stop flagging elapsed nights as lodging gaps (`jbaruch/nanoclaw-travel#120`)

`classify_trip` in `check-travel-bookings.py` scanned trip nights from `trip_start` with no floor at today, so a trip already underway reported every un-booked past night as a gap (live case 2026-07-07: the Scotland trip surfaced 10 phantom past-night gaps that buried the correctly-matched current Airbnb). The night scan now starts at `max(trip_start, today)`; `today` is threaded in from `main()` as a parameter so the classifier stays pure and testable. Future-night gaps of underway trips and future-trip flags are unaffected.

## 0.2.11 — 2026-07-02

### Changed — backfill CHANGELOG entries for released versions 0.2.7–0.2.10

Versions 0.2.7–0.2.10 shipped without CHANGELOG entries. Every released version now has a heading; the entries are reconstructed from the merge commits that produced each release. No code change.

## 0.2.10 — 2026-07-02

### Added — wire pyright into CI as a zero-findings gate (`jbaruch/nanoclaw-travel#115`)

Add a `python -m pyright --warnings skills/ tests/` step in CI after ruff and before pytest (`--warnings` fails on warnings, not just errors), completing the diagnostics gate whose config landed in 0.2.8. The tree was already clean (0 findings); no source changes.

## 0.2.9 — 2026-07-02

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-travel#117`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.2.8 — 2026-07-01

### Added — pyright config and test-suite strictness (`jbaruch/nanoclaw-travel#116`)

Land `pyrightconfig.json` (per-bundle `executionEnvironments` for the skill-bundle `sys.path` layout) and bring `pyright skills/ tests/` to zero findings, tightening test-side typing. Pins `pyright` in `requirements-dev.txt`. The CI gate that enforces this lands in 0.2.10 (#115).

## 0.2.7 — 2026-07-01

### Changed — refresh coding-policy PR review workflows (`jbaruch/nanoclaw-travel#114`)

Upgrade the gh-aw `jbaruch/coding-policy` PR review workflow templates to the latest published version.

## 0.2.6 — 2026-07-01

### Changed — migrate manifest from legacy `tile.json` to `.tessl-plugin/plugin.json`

Ran `tessl plugin migrate`: the manifest moved to `.tessl-plugin/plugin.json`, `.tileignore` was renamed to `.tesslignore`, and the obsolete `tile.json` was removed. `tessl plugin lint` passes on a clean tree (the local-only, git-ignored `.mcp.json` is absent in CI). This unblocks #77 (drive-planner evals, which require the plugin-manifest form). Package-sense "tile" wording throughout the prose and docstrings is reconciled to "plugin" per `jbaruch/coding-policy: migrate-to-plugin`; NanoClaw config identifiers (`additionalTiles`), `v1/tiles/...` API routes, and the CI publish workflow (still `tessl tile lint`, which works via the alias — a separate CI-scoped change) are intentionally left as-is.

## 0.2.5 — 2026-07-01

### Fixed — correct owner tile for the `## Addresses` block: `nanoclaw-trusted`, not `nanoclaw-admin`

`home_address.py`'s docstring and its three `HomeAddressError` messages named `nanoclaw-admin` as the owner of the canonical `## Addresses` block. The owner is the `trusted-memory` skill in **`nanoclaw-trusted`** (`tessl__trusted-memory`), whose `state-schema.md` documents the block (schema v1) and names this tile's `home_address.py` as its reader. The block is populated and correct on the NAS; only the attribution was wrong, so the reader worked but its "block missing" errors would have sent the operator to the wrong tile. Origin of the error is Epic #59 §4/§7 (`nanoclaw-admin`), carried into the reader and a prior CHANGELOG entry; both corrected. The legitimate `nanoclaw-admin` references (the `composio-fetch` calendar-fetch precedent, `check-travel-bookings`/`nightly-travel-sync` migrations) are unaffected.

## 0.2.4 — 2026-06-30

### Changed — drive-planner sweep notification is script-built, id-free, skip-by-number (`jbaruch/nanoclaw-travel`)

`apply.py create` now returns a ready-to-send `message` string (`build_notification`) that the wake agent relays verbatim, instead of composing the notification itself. The Haiku cadence agent was improvising a raw calendar event id into the skip affordance ("Reply skip `<id>` if you're not driving") despite the skill forbidding it; deterministic message assembly removes the improvisation surface entirely. One created block ends with the plain line `Reply skip if you're not driving.`; several render a numbered list ending with `Reply skip 1, or skip 1 and N, to drop any.` (N an index that exists for the count) — so the operator skips by a bare word or a list number, never an id. Route-error / unplannable / failed lines and the silence rule are preserved in the script. The skip-reply handler now treats `skip` as the primary verb (numbered `skip 1` / `skip 1 and 3`), with `cancel` kept as a synonym.

## 0.2.3 — 2026-06-30

### Added — gate + terminal readout at the pre-boarding window; gate changes only after it (`jbaruch/nanoclaw-travel#103`)

All-day gate-assignment churn is replaced by a one-time departure gate + terminal readout, fired the first cycle a gate exists inside the pre-boarding window (`scheduled_dep − boarding_lead − 1h`), plus ordinary `gate_change` alerts only after that readout. Before the window, the latest gate is recorded to state silently and never notified — WN482's BNA gate changed four times across 2026-06-25 (D3 → D1 → C2 → D6), each a separate wake, none within an hour of boarding. The new `phase_markers.check_gate_assignment` marker carries dep gate + dep terminal (the navigation signal: which terminal to head to), defers to the first in-window gate appearance when assignment is late, and stays silent once the flight is already boarding/departed/cancelled/diverted (shared with #102's leave-by suppression via `_boarding_or_gone`); `precheck` resolves the boarding lead from the snapshot through `boarding_lead.py` and gates `gate_change` against the readout anchor: suppressed until the readout fires; on the readout's own cycle only the redundant departure gate_change is dropped (a simultaneous arrival-gate move still surfaces); and a flight already boarding or gone — whose readout never fires — surfaces gate moves rather than muting them forever. The lead reads the same snapshot fields as the calendar boarding block (`calendar_reconcile._resolve_lead`): today the widebody lead resolves only via the inbound-aircraft chain (`inbound.aircraft_model` → 50 min), and the narrowbody default (30 min) covers everything else; full top-level-model and transoceanic resolution arrives when the precheck stamps `aircraft_model` + airport coordinates into the snapshot (#55), at which point the window widens automatically with no change here. New event `gate_assignment` documented in SKILL.md Step 3 and `references/event-payloads.md`. `STATE_SCHEMA_VERSION` bumps 5→6 with an additive owner-side migration adding `gate_assignment_fired: false` to per-flight `phase_markers`.

## 0.2.2 — 2026-06-29

### Fixed — suppress `time_to_leave` once the flight is boarding or gone (`jbaruch/nanoclaw-travel#102`)

The traffic-aware leave-by gate (`phase_markers.check_time_to_leave`) no longer wakes the agent when the flight has already started boarding or departed. On a delayed flight or with a stale travel estimate, the leave-by moment can land after boarding begins, so the marker fired, the agent woke, found nothing useful to say, and stayed silent — 2 of 16 flight-assist wakes on 2026-06-25 were this wasted pattern. The gate now takes the current snapshot and returns silent when it reads real-boarding (via `wake_rules.is_real_boarding`, which screens out byAir's premature "boarding" label per #54) or a `departed`/`en_route`/`landed`/`cancelled`/`diverted` status. The `_is_real_boarding` predicate is promoted to the public `is_real_boarding` since it is now shared across `wake_rules` and `phase_markers`. The normal pre-boarding leave-by alert is unchanged.

### Changed — run the flight-assist cadence wake on Haiku (`jbaruch/nanoclaw-travel#101`)

The flight-assist `agentModel` moves from `claude-sonnet-4-6` to `claude-haiku-4-5-20251001`, joining sync-tripit, nightly-travel-sync, drive-planner, and drive-planner-recheck on Haiku. The wake-cycle work is deterministic-script output (`reconcile.py`) plus a fixed `reason → sentence` template lookup — the hard logic lives in scripts, not the LLM — so the cheaper model carries it. The interactive diagnose / set-home-base paths are user-triggered and unaffected.

### Added — wire airport drive blocks into the wake-cycle reconcile (`jbaruch/nanoclaw-travel#90`)

The airport drive blocks now run for real. `airport_drive_reconcile.run_airport_drive_pass(composio, now=)` is the wake-cycle entry point — it resolves the inputs from the environment and on-disk state (config `home_address`, the live drive origin, the byAir + Maps clients, the active flights' states) and runs `run_airport_drive_reconcile`; the `reconcile.py` script calls it after the byAir-calendar reconcile and folds the result into its JSON under `airport_drive`. The drive blocks live on the **primary** calendar, so the pass runs even when the byAir-flight reconcile returns `no_calendar`; it stays a dormant zero-op summary when routing is unavailable (no `GOOGLE_MAPS_API_KEY`, no `BYAIR_MCP_URL`, or no tracked flights), and a transient byAir/Maps/Composio failure during it is logged and recorded as `{"status": "error"}` without failing the rest of the cycle. The origin ladder (fresh `current-location.json` → `home_address` → None) is extracted to `state.resolve_live_origin`, the single resolver the precheck's time-to-leave query now delegates to as well, so the two paths can never disagree on where the user is. SKILL.md Step 3 documents the new `airport_drive` output object.

### Added — airport drive block orchestration: fetch, plan, execute (`jbaruch/nanoclaw-travel#90`)

`airport_drive_reconcile.py` gains `run_airport_drive_reconcile(states, composio=, byair=, maps=, origin=, home_address=, config=)` — the orchestration that drives the assembler end to end against the calendar. For each active flight it builds the warranted blocks, fetches the primary calendar once over the spanning window (reusing `calendar_reconcile`'s live-verified `_find_events_args` / `_items`), and per block runs `plan_drive_block` and executes the create / shift via Composio. Calendar-as-state, no ledger: an existing block's no-op `signature` is derived from the block's OWN stored `anchor` + baseline (round-trip-stable, byte-identical to `DesiredDriveBlock.signature()`'s arithmetic) rather than from Google's start/end echo, so the offset-format ambiguity that bit #83 can't cause spurious shifts. A shift is a recreate-then-delete — create the replacement first so a transient create failure never leaves a gap, then delete the old, rolling the new one back if that delete fails so a cycle never leaves a duplicate — and every write goes through `build_block_args`' timezone-aware create path; a re-routed leave-by that drifts under `_REANCHOR_THRESHOLD` (5 min) from the block already on the calendar is suppressed, so traffic jitter doesn't rewrite the event every poll (#90 §7). The fetch window is anchored on each flight's stable scheduled times (not only the delayed desired window), so a block created before a delay is still found and shifted rather than duplicated however far the flight has moved. Per-op create/delete failures are collected, not raised — one bad write defers that op to the next cycle; a one-shot calendar fetch failure propagates (matching `calendar_reconcile`). Not yet wired into the wake-cycle `reconcile.py` (the client construction + origin resolution land in the follow-up PR on #90).

### Added — airport drive block assembler, the reconcile/route half (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_drive_reconcile.py` — the I/O-bearing layer that turns a flight's persisted state into the `DesiredDriveBlock`s the planner reconciles. `build_drive_blocks_for_flight(state, byair=, maps=, origin=, home_address=, config=)` gates on the flight's `computed_status` (a `to_airport` block while it hasn't left — scheduled/check-in/boarding; a `from_airport` block once airborne or down — departed/en_route/landed), resolves each direction's airport context via `byair.get_airport` (flag/`delay.index`/IANA-tz/code through `airport_drive_inputs.airport_context`), routes the leg via `maps.travel_time` (traffic-aware seconds when modelled, else free-flow), picks the byAir-truth dep/arr instant from the snapshot (live value over scheduled), and hands those to `airport_drive_inputs.departure_block` / `arrival_block`. The airport leg endpoint is the airport `name` (falling back to `code`) — what the precheck's existing time-to-leave query already routes, and what reads cleanly as the block's calendar location; the routed origin/destination pair is captured on the block so the recheck re-routes the same leg. Errors degrade per leg, never abort: a byAir lookup or Maps route failure drops just that block (the primary airport's code is required; a secondary-airport lookup failure falls back to the safe international classification), the next cycle retries. Pure of calendar I/O — given injected clients and a resolved `origin`, it returns the desired blocks; the primary-calendar fetch + `plan_drive_block` + Composio create/shift executor, and the precheck moving-origin re-anchor, land in the follow-up PRs on #90.

### Added — airport drive block input builder, groundwork for the integration (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_drive_inputs.py` — the pure, deterministic seam between the live world (byAir airport context + Maps routing + the resolved origin) and the `airport_drive.plan_drive_block` planner. Given a flight's already-fetched dep/arr `get_airport` payloads, the two airport codes, the byAir-truth dep/arr instants, the routed leg (origin/destination/baseline seconds), and the optional `config.json` clearance overrides, it builds the two `DesiredDriveBlock`s the planner consumes: the departure block anchored to the be-at-the-airport deadline (`dep − clearance`, the route-class buffer plus the departure airport's `delay.index` nudge) running `[anchor − drive, anchor]`, and the arrival block anchored to the earliest the drive home can start (`actual_arr + post_arrival_delay`) running `[anchor, anchor + drive]`. Summaries are the #90 §10 literals (`Drive: → BNA (DL123)` / `Drive: BNA → home`); the CREATE timezone is the relevant airport's IANA tz. An `airport_context` extractor pulls the flag/`delay.index`/tz slice out of byAir's raw payload defensively (a non-dict or a missing field degrades to None, never raises — an absent flag classifies international, an absent tz omits the timezone). Clearance/post-arrival math stays in `airport_lead`; this module only reads the config-override keys and passes them through (a malformed hand-edited override is ignored, the default applies). Pure (no I/O, no clock), fully unit-tested, and the test guards the seam by asserting a built block survives `airport_block.build_block_args` and parses back. Not yet wired into the precheck or reconcile; the fetch/route and moving-origin re-anchor land in the follow-up PRs on #90.

### Added — airport clearance config fields (`jbaruch/nanoclaw-travel#90`)

`config.json` gains five optional, non-negative airport-clearance fields — `airport_clearance_domestic_minutes`, `airport_clearance_international_minutes`, `airport_post_arrival_domestic_minutes`, `airport_post_arrival_intl_us_minutes`, `airport_post_arrival_intl_abroad_minutes` — the operator's risk-tolerance knobs for how early to be at the airport before departure and how long after landing before the drive home can start. Each overrides the matching `airport_lead.py` default (60 / 120 / 20 / 40 / 60); absent → the default applies. `STATE_SCHEMA_VERSION` bumps 4→5 with an additive, no-op migration (an old v4 config gains no keys). The byAir `delay.index` nudge (low/med/high → +0/+15/+30) stays an `airport_lead` constant — it's keyed on byAir's index and doesn't fit the flat int-field config shape. Not yet consumed; the precheck wiring that reads these lands in the follow-up PR on #90.

### Added — airport drive block planner, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_drive.py` — the pure create/shift/skip planner for the airport drive blocks. Given a flight's already-resolved drive inputs (a `DesiredDriveBlock` the precheck will compute from the byAir airport context + Maps routing + the resolved origin), it finds this flight+direction's block by scanning the fetched calendar events for its `[flight-assist:flight=<id>:dir=<dir>]` marker — **no local ledger** (calendar-as-state, the drive-planner model, matching how `state-schema.md` documents the blocks) — and emits 0–1 ops: create when none exists, no-op when the live window already matches, or update when a re-anchor/re-route shifted it. The op `create_args` is the `airport_block` `build_block_args` dict, ready for the executor to pass to CREATE/PATCH (`create_args["calendar_id"]` always equals the op's `calendar_id`, so the PATCH target never diverges). Pure (no I/O), two block kinds (`airport_drive_dep` / `airport_drive_arr`). It lives outside `calendar_plan.py` deliberately: those reconcile ops carry `{summary, start, end, private_props}` bodies for byAir-calendar events, whereas the airport blocks use the self-contained `airport_block` codec on the primary calendar. Not yet wired into the precheck; the I/O wiring lands in the follow-up PR on #90.

### Added — airport drive block codec, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

flight-assist gains `airport_block.py` — the calendar-as-state codec for the airport drive blocks: it builds the `GOOGLECALENDAR_CREATE_EVENT` args for a block and parses a fetched event back into a typed `BlockState`. State (schema v1) rides in the event description as a `[flight-assist:flight=<id>:dir=to_airport|from_airport]` marker plus an `<!--fadrive:{...}-->` JSON comment carrying baseline drive seconds, the anchor instant, routed origin/destination, and the alert-suppression record (the `fadrive` prefix is distinct from flight-assist's existing `<!--fa:-->` event tags, to avoid collision). Free transparency, airport-IANA-tz create, recheck-window + once-per-alert logic. A deliberately self-contained sibling of drive-planner's `block_props.py` — the shared-extraction approach was dropped as too complex for the cross-skill coupling it required (#90 decision); drive-planner is untouched. Not yet wired into block creation; the integration lands in the follow-up PR on #90.

### Added — byAir airport-context client methods, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

`byair_client` gains `get_airport(airport_id)` and `get_airport_tips(airport_id)` — the airport context the drive blocks need: `countryName`/`countryFlag` for international classification, the structured `delay` index for the congestion nudge, the IANA `timezone` for correct block placement, and free-text community tips for the reasoning layer. Both cache per airport id for the client's lifetime: byAir throttles ~10 calls/session and a single precheck cycle queries the same departure/arrival airports across flights, so repeats are served from cache without spending a call. `_call_tool`/`_tools_call` return types are corrected to `Any` (some byAir tools — `byair_get_airport_tips` — return a JSON array, not an object). Not yet wired into block creation; the integration lands in follow-up PRs on #90.

## 0.1.50 — 2026-06-25

### Added — airport clearance resolver, groundwork for airport drive blocks (`jbaruch/nanoclaw-travel#90`)

New `airport_lead.py` (sibling of `boarding_lead.py`) resolves the two ground-transit deadlines around a flight: how early to be at the airport before departure (domestic 60 / international 120 min, nudged up by byAir's airport `delay.index`), and how long after landing before the drive home can start (domestic 20 / intl-to-US 40 / abroad 60 min). International vs domestic is decided by decoding each airport's `countryFlag` emoji to its ISO 3166-1 alpha-2 code (byAir exposes no ISO field, only a native-spelling `countryName`) and matching a canonical Schengen set, so intra-Schengen counts as domestic. An undecodable flag falls back to the international (larger) buffer. Pure, config-overridable, fully unit-tested; not yet wired into block creation — the integration lands in follow-up PRs on #90.

## 0.1.49 — 2026-06-25

### Fixed — drive-planner cancel UX: by list number or name, never an internal id (`jbaruch/nanoclaw-travel#86`)

The sweep notification told the operator to "Reply `skip <meeting_id>`", where `meeting_id` was the raw Google Calendar event id (opaque base32, effectively untypeable) — internal plumbing leaked to the user. Now the user-facing surface never carries an id: when one block is added the notification offers a plain "reply `skip` to cancel"; when several are added it numbers them and offers "`cancel 2`" / "`cancel 1,3`". A new `apply.py list` mode returns the current drive blocks (one per meeting, ordered by leave-by, summary stripped of the "Drive: " prefix) with their internal `meeting_id`s, so the cancel step maps the operator's ordinal or meeting name onto the id itself and confirms by name. The id never appears in, or is required from, a user message.

## 0.1.48 — 2026-06-25

### Fixed — drive-planner no longer plans impossible cross-city ground drives (`jbaruch/nanoclaw-travel#85`)

The sweep bridged any two consecutive in-person meetings within the tight-gap window by clock gap alone — so a St. Louis conference talk (flown to) chained to a Brentwood TN swimming practice produced a 309-min "drive" inside a 45-min gap, and the flown-to talk itself drew a ~4.5h ground drive. `plan_meetings` now applies two sanity gates after routing: a bridge whose routed drive overruns the gap between the meetings, or any leg whose drive exceeds `MAX_REASONABLE_DRIVE_SECONDS` (3h — the operator almost certainly flew), is recorded under a new per-meeting `unplannable` list with a human reason instead of becoming a block. The leg is surfaced, never silently dropped (§5): the SKILL.md tells the operator "no drive block for X — likely flying". Flight/TripIt awareness (knowing where the operator physically is) stays a future enhancement; this gate catches the nonsensical output regardless.

## 0.1.47 — 2026-06-25

### Fixed — calendar blocks land at the right instant: explicit CREATE timezone (`jbaruch/nanoclaw-travel#83`, `#82`)

Live verification of the *placement* (not just the description round-trip) showed drive blocks landed ~5h early: the live `GOOGLECALENDAR_CREATE_EVENT` reads a bare `start_datetime`'s wall-clock as **UTC** unless an explicit `timezone` is supplied, so an offset-bearing string alone is mis-anchored (created events came back stamped `timeZone: UTC`). The earlier flat-create fix (0.1.46) corrected the duration half of #83 but not this timezone half.

- **drive-planner** threads the meeting's IANA `start.timeZone` (which live Google events carry) from `scan` → `MeetingClass` → `build_block_args`, emitted as the CREATE `timezone`; a block missing its IANA `timeZone` but carrying an offset falls back to a fixed-offset `Etc/GMT±N` zone. Verified live: a 14:00-CT meeting's block now lands at 13:30 America/Chicago, not 08:30.
- **flight-assist** has only the departure offset, so `calendar_reconcile` maps a whole-hour offset to a fixed-offset `Etc/GMT±N` zone (correct instant + local-clock display); a rare non-whole-hour offset (e.g. +05:30) normalizes `start_datetime` to UTC instead. This also closes the boarding-create half of #82 (the create itself was fixed in 0.1.46).

Both paths verified end-to-end against the live toolkit (create → fetch → assert wall-clock placement → delete).

## 0.1.46 — 2026-06-25

### Fixed — calendar writes rebuilt for the live Composio v3 contract (`jbaruch/nanoclaw-travel#59`)

Live NAS verification of the *write* path showed both skills' calendar I/O was built against an assumed Composio contract that does not exist on the live v3 toolkit — every create silently failed (`executed: 0`), so no blocks or boarding events were ever written. Probed every `GOOGLECALENDAR_*` action against the NAS and rebuilt to the real shapes:

- **No writable `extendedProperties`.** Neither `CREATE_EVENT` nor `PATCH_EVENT` exposes it, so the machine state both skills stamped there could never be written. drive-planner's block state (baseline seconds, arrive-by, routed endpoints, alert record) and flight-assist's managed-event tags (`faFlightId`/`faKind`/`faManaged`) both move into the event **`description`** — drive-planner as a `<!--dp:{...}-->` comment beside its `scan` marker, flight-assist as a `<!--fa:{...}-->` comment via the new `calendar_tags` codec. This supersedes the `extendedProperties.private` design described in 0.1.44.
- **Flat create/patch.** `CREATE_EVENT` takes flat `start_datetime` + `event_duration_hour`/`event_duration_minutes` (the old nested `start.dateTime`/`end.dateTime` was rejected); `PATCH_EVENT` takes flat `start_time`/`end_time`. flight-assist's adopt path now appends its tags to byAir's existing description (preserving it, stripped back off on the next read so tags never accumulate) instead of clobbering a separate field.
- **Response shapes.** `FIND_EVENT` double-nests events at `data.event_data.event_data`; `LIST_CALENDARS` returns the list under `calendars`. The old `items` reads found nothing. Both skills' `_items` walk the live shapes; drive-planner's recheck-poll suppression PATCHes the rebuilt `description`.

The internal `private_props` abstraction is unchanged end-to-end — `normalize_event` decodes the description comment back into it on read, the reconcile write helpers encode it on create/patch — so the flight-assist planner is untouched. Verified live with the real modules: create → fetch → parse round-trips for a drive block, and create → normalize → adopt-patch (description preserved) for a boarding event, each cleaned up after. Added direct create/patch arg-shape regression tests (the gap that let the nested-format bug ship) plus a `calendar_tags` codec test.

### Fixed — drive-planner never plans a drive to a declined or cancelled meeting (`jbaruch/nanoclaw-travel#59`)

`scan` filtered virtual / all-day / past meetings but ignored the operator's RSVP, so a meeting you declined still got a drive block — and `fetch_events` dropped `attendees` entirely, so the data to detect it wasn't even carried through. Live probing confirmed the shape: the operator's own attendee row carries `self: true` + `responseStatus`. `scan` now filters an event whose self-attendee is **explicitly** `declined` (and event `status: "cancelled"`); `accepted` / `tentative` / `needsAction` all still plan, and a declined meeting is excluded as a routing neighbour so it can't strip a real meeting's home legs. `fetch_events` carries `attendees` + `status` through its projection.

### Fixed — flight-assist state-validation crash + Optional-flow type bugs (`jbaruch/nanoclaw-travel#59`)

Resolving pyright across the `sys.path`-insert bundle layout surfaced real source bugs. The flight-state validators formatted type-mismatch errors with `expected_type.__name__`, but the schema dicts permit tuple types like `(dict, type(None))` — a tuple-typed field on a mismatch would raise `AttributeError: 'tuple' object has no attribute '__name__'`, masking the intended `StateError`; a `_type_name` helper now handles both shapes. `byair_client` guards a `raise __cause__` where `__cause__` could be `None`. `phase_markers`' `check_*` functions had `scheduled_*_time: str` params that already tolerated `None` internally (`_parse_iso8601` accepts `str | None`); the annotations were corrected and the fired-event appends in `precheck` guarded so a `None` event can't reach `events.append`.

## 0.1.45 — 2026-06-24

### Fixed — drive-planner calendar fetch action slug (`jbaruch/nanoclaw-travel#59`)

Live NAS verification surfaced the action-slug caveat `fetch_events.py` flagged: the sweep's calendar fetch used `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`, which does not exist in the live Composio v3 toolkit and 404s. Corrected to `GOOGLECALENDAR_EVENTS_LIST` with the schema-required camelCase `calendarId: "primary"` + `singleEvents: true` arguments (verified against `GET /api/v3/tools/GOOGLECALENDAR_EVENTS_LIST` and matching the proven `nanoclaw-admin` `composio-fetch` precheck). Scope adjusts from the epic's aspirational "all calendars" to the **primary** calendar — the all-calendars slug isn't real, and primary is where in-person meetings live; multi-calendar fan-out (list calendars → fetch each) is a future enhancement. The `data.items` container is the Google-native events.list shape; the response-key candidates now check `items` first. Probed live against the operator's calendar (HTTP 200, 44 events with summary/location). Test asserts the slug + the `calendarId`/`singleEvents` args.

## 0.1.44 — 2026-06-24

### Added — drive-planner sweep + recheck poll, wired into the tile (`jbaruch/nanoclaw-travel#59`)

The two drive-planner skills that turn the deterministic core (scan / fetch / recheck-gate / skip-store, shipped over #59) into a live, registered capability (Epic #59 §3, §4, the confirmed create-first interaction model and the poll-based recheck model).

**Calendar IS the state (§4).** New `block_props.py` is the codec: `build_block_args()` stamps the `scan` self-marker into the block description and the machine state — baseline drive seconds, arrive-by, routed endpoints, an alert-suppression record — into `extendedProperties.private`; `parse_block()` reads a fetched event back into a typed `BlockState` with leave-by + recheck-window math. A test pins the built marker against `scan._MARKER_RE` so the two never drift. There is no local block store — the recheck poll re-derives every block from the calendar each cycle, so a recheck can never be silently forgotten (lombot #48). `fetch_events.py` now carries `extendedProperties` through its projection so the poll can read its own blocks; `scan` ignores the field it doesn't read. `next_alerts()` fires each recheck condition (traffic grew past threshold / leave-by arrived) at most once per block — re-pinging a still-grown drive every poll is the trust-eroding nag (§5 #49 in spirit).

**Sweep (`drive-planner`, ~2h cadence).** `precheck.py` is the deterministic spine: fetch the wide window → `scan` → for each `needs_decision`/`bridge`/`back_to_back` meeting, pre-route every leg with live traffic and build the exact `GOOGLECALENDAR_CREATE_EVENT` args. Routing is deterministic, so it lives in the script, not the agent; a leg the router can't price is reported, never dropped (no silent miss, §5). The SKILL.md is an action router: on a wake it runs `apply.py create` (idempotent — finds existing markers first, never double-books, lombot #50) then sends one "added drive block for X, leave by HH:MM — reply skip to remove" notification; a "skip `<id>`" reply runs `apply.py remove` (delete the blocks + record a skip so the next sweep won't recreate them, expiry derived from the block when the reply omits the meeting end).

**Recheck poll (`drive-planner-recheck`, ~15-min cadence).** `precheck.py` re-fetches the near-term window by direct API call, parses its own marked arrival-anchored blocks back off `extendedProperties`, re-routes each due leg, runs `evaluate_recheck`, and fires each condition once. It only *produces* the suppression patches (each carrying the block's full private map with the alert record updated); the recheck SKILL.md applies them via `apply.py suppress` AFTER the send confirms, so a failed send never permanently suppresses a leave-earlier / leave-now alert (a forgotten patch merely re-pings next poll — the safe direction). The SKILL.md composes the push, then records suppression. Outer-boundary prechecks fail closed; the leave-by alert is re-derived each poll, so one skipped cycle never loses it permanently.

**Home address (§4).** `home_address.py` reads `current_home` from the canonical `## Addresses` block in `/workspace/trusted/user_profile.md` (owned by the `nanoclaw-trusted` trusted-memory skill — a separate change there lands the block). It deliberately ignores `new_home_wip` and refuses to guess on a missing block — a silent wrong origin would mis-route every leg — raising an actionable error pointing at the trusted tile.

**Packaging.** Both skills registered in `tile.json`; `state-schema.md` documents the calendar-as-state block contract alongside the skip store; README skills + scripts tables updated. `maps_client` and `composio_client` are imported read-only from the co-located flight-assist bundle via the runtime-mount-with-dev-fallback pattern `sync-tripit` already uses — flight-assist's mission-critical leave-by path is untouched (zero flight-regression risk), so `maps_client` was not moved. Composio is mid-retirement (nanoclaw#638) — the API fetch + patch are the pieces that re-point later. ~50 new tests across the codec, home reader, suppression, sweep planner, apply step, and recheck poll (injected routers + a fake Composio client; no live calendar/maps). Live NAS verification + the admin address block are tracked separately under §7.

## 0.1.43 — 2026-06-24

### Added — drive-planner wide-window calendar fetch (`jbaruch/nanoclaw-travel#59`)

The live calendar read that feeds the sweep (Epic #59 §4): `skills/drive-planner/fetch_events.py`, a self-contained Composio client that makes one wide-window `GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS` call over a `[time_min, time_max]` window and returns the raw Google Calendar event dicts in the exact shape `scan(events=...)` consumes (`id`, `summary`, `location`, `start`, `end`, `description`). drive-planner owns its own fetch rather than importing flight-assist's per-calendar `composio_client` (a different action, a separately-loadable skill bundle), but mirrors that module's transport faithfully: stdlib-only `urllib`, HTTP-mockable in CI, the Composio `successful`/`error` envelope, read-timeout normalized to `URLError`, one client per process. The action slug and the candidate `data` event-container keys (`events` / `items`) are isolated at the top of the file for one-line correction against the live toolkit; a `successful: true` body carrying no recognizable event list raises `FetchError` rather than silently returning zero events (which would make the sweep a no-op and quietly stop planning). Window inputs are guarded (tz-aware, `time_max > time_min`); a tool-level failure raises `FetchError` with the upstream `status_code`. 18 mocked-HTTP tests incl. an integration check that the fetched events feed `scan`. Like `maps_client`/`composio_client` it is a transport library with no CLI; the sweep precheck that composes fetch → scan lands with the SKILL.md. Composio is mid-retirement (nanoclaw#638) — this is the one piece that re-points later.

## 0.1.42 — 2026-06-24

### Added — drive-planner skip store (`jbaruch/nanoclaw-travel#59`)

The on-disk store of "skip this meeting" decisions that feeds `scan()`'s `skip_state` (Epic #59 §3, §5 #49): `skills/drive-planner/skip_state.py`, owning `<state_dir>/skip-state.json` (`{"schema_version": 1, "skips": {<id>: "<ISO expiry>"}}`). Re-asking about a meeting the user already skipped is the trust-eroding nag LoMBot hit, so a skip sticks — but with auto-expiry: the writer sets each skip's expiry to the meeting's end, and `load_active_skips(now)` drops anything expired so a stale skip never suppresses a meeting forever. API: `add_skip(id, expires=, now=)`, `load_active_skips(now)` (the `{id: expiry}` mapping `scan` consumes, read-only), `clear_skip(id, now=)`, `prune(now)`. Writes are atomic (temp-file + rename, temp cleaned in `finally`). Per `coding-policy: stateful-artifacts`: drive-planner is the sole owner, the state dir is overridable via `DRIVE_PLANNER_STATE_DIR`, and `state-schema.md` documents the schema, the writer/reader contract, and the tolerance rules — a missing file reads as "no skips", a present-but-corrupt file (bad JSON, non-object root, missing/newer `schema_version`) raises `SkipStateError` rather than silently resurrecting every skip, and malformed individual entries are dropped. 28 tests incl. an integration check that the loaded mapping drives `scan` to the `skipped` bucket. Not yet wired into `tile.json` — the SKILL.md sweep that writes and reads it lands next.

## 0.1.41 — 2026-06-24

### Added — drive-planner recheck gate (`jbaruch/nanoclaw-travel#59`)

The deterministic gate the scheduled T-45 / T-30 / T-15 rechecks use (Epic #59 §3, §5 #48): `skills/drive-planner/recheck.py`, a pure function `evaluate_recheck(baseline_seconds, current_seconds, arrive_by, now, …)` → `RecheckDecision`. Most rechecks are no-ops; pinging on every couple-minute fluctuation erodes trust, so the gate alerts only when the drive grew at least `threshold_seconds` over the baseline (default 10 min) OR the recomputed leave-by (`arrive_by − current − buffer`, default 5-min buffer) is at/before `now` — you must leave now regardless of growth. It does not route — `current_seconds` comes from live traffic (`maps_client`) upstream, the gate's caller's job — so the function stays pure and fully testable. The CLI follows the precheck-gating contract (`coding-policy: script-delegation` Precheck Gating): stdin JSON request → stdout `{"wake_agent": <alert>, "data": {<decision>}}`, so a scheduler runs it and only wakes the agent on an alert, with `data` carrying the delta and recomputed leave-by for the ping. All boundary inputs are validated (non-negative integer durations, tz-aware datetimes with `Z`-normalization and naive rejection) with `RecheckError` → JSON stderr + non-zero exit, matching the scan classifier's hardening. 36 tests (alert triggers, silence cases, leave-by math, input guards, CLI contract); no live routing. Not yet wired into `tile.json` — the SKILL.md that schedules and consumes rechecks lands with the sweep.

## 0.1.40 — 2026-06-24

### Added — drive-planner scan classifier (`jbaruch/nanoclaw-travel#59`)

The deterministic brain of the new `drive-planner` skill (Epic #59 §3, §5): `skills/drive-planner/scan.py`, a pure events-JSON → buckets classifier. Given the wide-window calendar events plus `now`, the home address, and the skip-state, the pure `scan()` returns one `MeetingClass` per event in one of the buckets `needs_decision` / `bridge` / `back_to_back` / `has_block` / `skipped` / `past` / `filtered`, plus the concrete `TransitLeg`s each routable meeting needs (outbound / return / bridge with deadlines). It does not route — drive time needs live traffic (`maps_client`) downstream — so bridge legs expose `gap_seconds` for the router's drive-time > gap warning. Every scar from LoMBot's 16 closed `drive_planner` issues is baked in: handled = ANY marker, not both directions (#50); skips persist with expiry and virtual locations are filtered, never asked (#49); past guard everywhere, including exclusion from neighbour linking so a stale same-venue meeting can't strip a future meeting's outbound leg (#28 × #14/#7); neighbour-aware same-venue-tight = back_to_back vs different-venue-tight = bridge (#14/#7); whitespace-normalized location before routing (#37); return and bridge first-class (#2/#40). Datetime parsing normalizes a trailing RFC3339 `Z` and rejects timezone-naive values (which would raise against the tz-aware `now`). Nothing is silently dropped — filtered/past events come back with a reason so the sweep can audit. The module also ships a CLI entry point (stdin JSON request → stdout `{"results": […]}`, stderr + non-zero on bad input) so the deterministic operation is a runnable script per `coding-policy: script-delegation` / `file-hygiene`, while the pure `scan()` stays the unit-tested core. 39 fixture tests (no live calendar), each neighbour/idempotency/skip/past case named after the lombot issue it encodes. The skill is not yet registered in `tile.json` (no `SKILL.md` until the fetch + recheck pieces land); this is the classifier slice only.

## 0.1.37 — 2026-06-23

### Added — TomTom backup routing in `maps_client` (`jbaruch/nanoclaw-travel#59`)

`maps_client` gained a Google-primary → TomTom-backup chain behind the unchanged `travel_time() → TravelTime` interface, the first piece of the drive-planner epic (#59) and a hardening of the existing flight `time_to_leave`. Google Distance Matrix stays primary; on any Google `MapsError` or transport (`URLError`) failure, the client falls back to TomTom when `TOMTOM_API_KEY` is configured. TomTom routing is coordinates-only, so the new `TomTomClient` does geocode-origin → geocode-destination → route-with-`traffic=true`, mapping `noTrafficTravelTimeInSeconds` / `travelTimeInSeconds` onto the same free-flow / in-traffic split Google returns. `TravelTime` gained a `source` field (`"google"` / `"tomtom"`) so callers can tell which provider answered. There is deliberately no no-traffic fallback (e.g. OSRM) — a duration without a live-traffic model is false confidence for a leave-by deadline; when both providers fail the client raises `MapsError("ALL_PROVIDERS_FAILED", …)` naming what each reported. `MapsClient.from_env` wires the backup only when `TOMTOM_API_KEY` is set, so a Google-only deploy is unchanged. The caller in `precheck.py` already catches `MapsError` + `URLError`, so the fallback integrates with no caller change. 16 new tests (TomTom geocode+route success, no-baseline traffic split, geocode/route zero-results, full Google→TomTom fallback on both `MapsError` and `URLError`, combined-failure error, Google-success-skips-TomTom, `from_env` wiring with/without the key); `.env.example` documents the optional key.

## 0.1.36 — 2026-06-22

### Added — calendar teardown tombstone sweep + wake-cycle wiring (`jbaruch/nanoclaw-flight-assist#55`)

The final reconciliation slice: switched-away flights now get their managed calendar events torn down, and the reconcile runs on the wake cycle. `calendar_reconcile.run_reconcile` gained a second pass — a tombstone sweep over on-disk flights that have dropped out of `active-flights.json` but still carry a `calendar_events` ledger. The per-flight wake loop only visits active flights, so this sweep is the only place a switched-away flight's stale events (which byAir leaves behind) get deleted. It resolves each tombstone's disposition off the retained ledger (switched_away / cancelled / diverted → teardown deletes; completed → leave the events as a historical record), executes the deletes, then **archives** (removes) the state file once teardown settles — every delete succeeded, or the flight has completed. A failed delete keeps its ledger entry, so the tombstone is retained for the next cycle's retry rather than archived with events still live. The summary gains an `archived` count. Teardown is ledger-driven, so when there are no active flights the cycle skips the calendar fetch entirely.

For the tombstone to survive, `sync_tripit._reconcile_active_flights` now retains `flight-<id>.json` (instead of deleting it on upstream removal) when the record still holds a non-empty `calendar_events` ledger; a removed flight with nothing to tear down is still deleted immediately. `state.py` gained `list_flight_state_ids()` to enumerate on-disk per-flight files regardless of active-flights membership — the sweep needs to see exactly the flights the index no longer lists.

`SKILL.md` Step 3 became "Handle the precheck wake cycle": it runs `scripts/reconcile.py` first (idempotent, delta-only, safe alongside byAir's own delay-shifts), then composes the notification. `no_calendar` / `no_flights` / missing-Composio-credentials all mean reconciliation is inactive this cycle and are handled silently — calendar reconciliation stays optional. 11 new tests (sweep teardown + archival, completed-leaves-events, failed-delete retention, empty-ledger non-tombstone, active+tombstone in one cycle, `list_flight_state_ids`, sync_tripit tombstone retention vs immediate delete).

## 0.1.35 — 2026-06-22

### Added — calendar reconcile orchestrator (`jbaruch/nanoclaw-flight-assist#55`)

The I/O layer that connects the pure planner to live Google Calendars (#55). `calendar_reconcile.py` resolves the calendar IDs, fetches + normalizes the current calendar state via Composio, builds the per-flight planner inputs (disposition via `disposition.py`, boarding lead via `boarding_lead.py`, byAir-truth dep/arr times), runs `plan_reconciliation`, executes the returned ops (`create` / `update` / `adopt` / `delete` / `forget`) through `composio_client`, and writes the owned event IDs back into each flight's `calendar_events` ledger. `scripts/reconcile.py` is the wake-cycle entry point: it emits a single-line JSON summary (`status` ∈ `ok` / `no_calendar` / `no_flights`) and collects per-op Composio failures rather than aborting the cycle — a delete that 404s is an idempotent success, a real failure defers that op to the next cycle.

The flight ("Flighty Flights") calendar ID is resolved at runtime from the operator-supplied `byair_calendar_name` and cached, never hardcoded in tile code per `rules/flight-data-locality.md`; Reclaim travel blocks live on the **primary** calendar (content-classified). The exact `GOOGLECALENDAR_*` argument field names are isolated in one section for live-toolkit verification, the same treatment `composio_client.py` gives its action slugs. This slice reconciles the flights in `active-flights.json`; the tombstone sweep for switched-away flights lands next (the planner already emits teardown ops for cancelled / diverted flights still in the index, which this executes). 18 orchestration tests against a fake Composio client (resolution, create/adopt/teardown, delta no-op, 404 idempotency, Reclaim same-airport-gap delete, malformed-event skipping) plus 2 CLI-contract tests.

### Added — state schema v4: cached flight-calendar id in config (`jbaruch/nanoclaw-flight-assist#55`)

`config.json` gains two optional calendar-reconcile fields: `byair_calendar_name` (operator-supplied display name of the flight calendar) and `byair_calendar_id` (the id the reconcile caches after its first name match). Both are optional and absent-tolerant, so the v3→v4 owner-side migration only bumps `schema_version` — no shape change to config, active-flights, or per-flight records. See `state-schema.md`.

### Fixed — precheck preserves the calendar_events ledger (`jbaruch/nanoclaw-flight-assist#55`)

`precheck._build_flight_state` rebuilt the per-flight record from scratch on every poll and dropped `calendar_events`, which would have wiped the reconcile-owned ledger (and the teardown tombstone it doubles as) every ~2 minutes. It now carries the ledger forward verbatim from prior state, so the reconcile's writes survive subsequent polls.

## 0.1.34 — 2026-06-22

### Added — calendar event normalization + Reclaim travel classifier (`jbaruch/nanoclaw-flight-assist#55`)

The read-side adapter for calendar reconciliation (#55), built against the real Google Calendar event shapes. `calendar_normalize.py` flattens a Google event resource into the planner's `{event_id, calendar_id, summary, start, end, private_props, is_reclaim_travel}` shape, and classifies Reclaim-generated travel blocks.

`is_reclaim_travel` is **content-based, not calendar-based**: there is no dedicated Reclaim calendar — Reclaim writes its travel blocks onto the user's primary calendar interleaved with real meetings, so the only safe delete discriminator is the event's own content. Two factors, both required: the Reclaim authorship signature (`app.reclaim.ai`) in the description AND a travel marker in the summary (`🚌 Travel`). Reclaim's habit/focus/task blocks carry the signature but a different summary → not flagged; a user's own event titled "Travel" carries no signature → not flagged. The planner further bounds every delete to a same-airport layover gap, so a genuine meeting is never a candidate. `calendar_id` comes from the fetch context (authoritative), not the event body; `private_props` is `extendedProperties.private`.

### Fixed — whitespace-insensitive flight-code adopt match (`jbaruch/nanoclaw-flight-assist#55`)

Real Flighty flight-event summaries render the code with a space (`✈ BNA→YYZ • UA 8018`) while byAir's `code` field may carry it unspaced (`UA8018`), so the planner's `code in summary` adopt-match missed. `_match_byair_event` now strips whitespace from both sides before comparing, matching regardless of which side carries the space.

## 0.1.33 — 2026-06-22

### Added — flight disposition resolver (`jbaruch/nanoclaw-flight-assist#55`)

Next deterministic slice of calendar reconciliation (#55): `disposition.py` resolves each flight's reconciliation disposition (`active` / `cancelled` / `diverted` / `switched_away` / `completed`) that `plan_reconciliation` consumes to decide between normal reconcile, teardown, and leave-as-record. The computation needs the two inputs the pure planner deliberately stays out of — the wall clock and `active-flights.json` membership — so it lives in one isolated, tested module, the same carve-out as `boarding_lead.py` keeping volatile policy out of the planner.

Precedence: byAir `computed_status` cancelled/diverted wins over membership and time; `landed` or an effective-arrival instant at/before `now` is `completed`; a flight that has dropped out of active-flights while still in the future is `switched_away` (the per-flight wake loop can no longer see it — teardown runs off the retained ledger tombstone); everything else in active-flights and not yet arrived is `active`. Effective arrival prefers byAir's actual `last_snapshot.arr_time` over `scheduled_arr_time`, so a delayed in-air flight stays `active` until it actually lands. 16 tests cover the precedence matrix, the actual-vs-scheduled arrival boundary, null/missing snapshots, and the RFC-3339 offset handling.

## 0.1.32 — 2026-06-22

### Added — Composio calendar transport client (`jbaruch/nanoclaw-flight-assist#55`)

The I/O layer the pure planner (#55, 0.1.31) needs to execute its op list. `composio_client.py` is a thin stdlib-`urllib` REST client over Composio's v3 `tools/execute/{action}` endpoint, mirroring `byair_client.py` / `maps_client.py` (HTTP-mockable in CI, one client per process). It injects `x-api-key` auth + `COMPOSIO_USER_ID` scoping, names the `GOOGLECALENDAR_*` action slug, and passes a Composio-shaped `arguments` dict through — the planner-op → arguments mapping (and the version-specific per-action argument schemas) stays with the reconcile executor that lands next, where it is verified against the live toolkit.

The Composio envelope returns HTTP 200 even on a tool-level failure (`successful: false`), so the client raises `ComposioError` on that and surfaces the upstream provider status in `.status_code` — a delete that 404s (event already gone) is distinguishable from a real failure, letting the executor treat it as an idempotent no-op. HTTP-level failures (bad key, 5xx) propagate as `urllib.error.HTTPError`; a body-read timeout normalizes to `URLError` (mirrors byair, #28). 15 HTTP-mocked tests cover request shaping, the success/failure envelope, status-code surfacing, and transport-error normalization.

`check-env.py` now also reports `composio_key_present` / `composio_user_present` (SKILL.md Step 1 + tests updated to match), and `.env.example` documents `COMPOSIO_API_KEY` / `COMPOSIO_USER_ID` (plus the optional `COMPOSIO_BASE_URL` override). No wake-cycle wiring yet — the reconcile script that fetches events, runs the planner, and writes the ledger back lands in the follow-up.

## 0.1.31 — 2026-06-20

### Added — pure calendar reconciliation planner + boarding-lead resolver (`jbaruch/nanoclaw-flight-assist#55`)

The deterministic core of calendar-event reconciliation (#55), built as two pure, network-free modules so the whole decision surface unit-tests in CI per `coding-policy: script-delegation`. The Composio I/O layer that executes the plan lands in a follow-up.

`calendar_plan.py` — `plan_reconciliation(flights, events, config)` takes the per-flight `calendar_events` ledger plus a normalized snapshot of what is on the byAir and Reclaim calendars, and emits a declarative op list (`create`/`update`/`delete`/`adopt`/`forget`) that converges the calendar to the desired state. Delta-only: it no-ops when a live event already matches the `synced_signature`, so it is safe to run alongside byAir's own shifts (no stomping). Covers all four behaviors: boarding-block lifecycle, byAir flight-event adopt-by-tag-then-shift, the positional Reclaim same-airport-layover deletion rule, and teardown of managed events on a cancelled/diverted/switched flight. Event classification is by calendar ID (no summary regex), so user-created events are never touched and only Reclaim-calendar blocks in a same-airport gap are deleted.

`boarding_lead.py` — `resolve_boarding_lead_minutes(...)` encodes the (volatile) boarding-pace policy in one isolated, tested place; the planner consumes the resolved integer only. Policy: transoceanic crossing → 50, widebody → 50, narrowbody → 30, nothing classifiable → 30. Aircraft size is by aisle count (A320 family incl. A321, all 737, 757, regional/turboprop are narrowbody; twin-aisle is widebody), from byAir's top-level `model` with a fallback to `inbound.aircraft_model`. Transoceanic (TATL/TPAC) detection is a longitude-block + great-circle-distance heuristic over airport lat/lon — no country/continent table — and correctly excludes Europe↔Asia overland long-haul.

36 new tests (`test_calendar_plan.py`, `test_boarding_lead.py`) cover boarding create/no-op/shift/recreate, adopt/skip-tagged/tolerance/shift/forget, Reclaim delete-vs-keep across the positional cases, teardown, and the full lead-policy matrix on real airport coordinates. Also renames the `Flighty` references the v3 state-schema docs introduced to `byAir` per `rules/flight-data-locality.md` (byAir is the tile's single anonymized flight upstream — it both serves the data API and writes the flight events to the writable calendar); the boarding/flight calendar ID is operator config resolved at runtime, not hardcoded.

### Changed — renamed tile `jbaruch/nanoclaw-flight-assist` → `jbaruch/nanoclaw-travel`

The tile is broadening from flight-only notifications into a general travel assistant — ground-transit drive planning (borrowed from the `ligolnik/lombot` `drive_planner` design) lands as a sibling skill next. Repo and tessl registry identity rename to `jbaruch/nanoclaw-travel`; consumers update their `additionalTiles` entry to the new name. Historical CHANGELOG issue references keep the old `nanoclaw-flight-assist#NN` form — GitHub redirects them after the repo rename.

## 0.1.30 — 2026-06-19

### Added — per-flight `calendar_events` ledger + state schema v3 (`jbaruch/nanoclaw-flight-assist#55`)

Foundation for calendar-event reconciliation (#55): flight-assist is moving from a notification-only tile to one that writes Google Calendar events (a flight-assist-created boarding block, adopted byAir flight events, Reclaim travel-block cleanup). To update and delete those events in O(1) across the `*/2` precheck cadence — and to tear them down after a flight drops out of `active-flights.json`, where the per-flight wake loop can no longer see it — the per-flight state record needs a ledger of the event IDs flight-assist owns.

`STATE_SCHEMA_VERSION` bumps `2 → 3`. Per-flight `flight-<id>.json` gains an optional `calendar_events` map keyed by event kind (`boarding`, `flight`); each entry carries `event_id`, `calendar_id`, `managed` (`created`/`adopted`), and a `synced_signature` (`<start>/<end>`) the planner diffs against to no-op when the live event already matches byAir truth. `state.py` validates the field structurally (object) only — the per-entry shape is owned and deep-validated by the reconcile planner that lands in a follow-up, the same split as `last_snapshot` ↔ `byair_client`. `_migrate` now chains its version steps (a v1 record runs v1→v2→v3 in one owner-side read), adding `calendar_events: {}` to per-flight records on the v2→v3 step and bumping config/active-flights with no shape change. New `test_state.py` cases cover the v2→v3 per-flight add, the config/active-flights version-only bump, the chained v1→v3 path, round-trip with `calendar_events` present, and structural rejection of a non-object value. No behavior change yet — the precheck and SKILL surfaces are untouched; this is the state contract the reconciler builds on.

## 0.1.29 — 2026-06-19

### Fix — `boarding_started` no longer trusts byAir's premature `boarding` label (`jbaruch/nanoclaw-flight-assist#54`)

byAir flips `computed_status` to `boarding` up to ~1h before boarding actually starts, while its own `computed_status_detail` still reads "Boarding starts in N min" and `computed_phase_progress` is 0 — an internally contradictory payload (DL4662 fired a false "boarding now" alert twice, 2026-06-13 and 2026-06-16, while the flight was delayed 67 min and boarding had not begun). `detect_wake_events` no longer fires on the `computed_status` label alone: a new `_is_real_boarding` helper requires the `boarding` status AND a `computed_status_detail` that is not a future-tense "Boarding starts in …" countdown. The boarding transition is now computed against this real-boarding signal on both the prior and current snapshots, so a flight byAir prematurely marked `boarding` still fires once the detail flips to genuine boarding — even though the raw `computed_status` never changes across that flip. The upstream contradiction is byAir's (operator filed a support ticket 2026-06-16); this is the skill-side guard. Four new `test_wake_rules.py` cases cover premature-label suppression, the deferred real-boarding fire, a genuine non-future-detail boarding, and first-cycle premature suppression.

### Changed — per-skill `agentModel:` tier-down (`jbaruch/nanoclaw#613`)

Pin cadence-skill models via `agentModel:` frontmatter so they stop defaulting to Opus: **Sonnet** (`claude-sonnet-4-6`) for `flight-assist` — itinerary/flight reasoning matters there; **Haiku** (`claude-haiku-4-5-20251001`) for the data-sync skills `nightly-travel-sync` and `sync-tripit`. Part of the #613 Claude tier-down.

### Fix — `nightly-travel-sync` ran-marker carries the `<slot_key>` date the #581 watchdog expects (`jbaruch/nanoclaw-flight-assist#51`)

The skill's final-turn marker emitted `nightly-travel-sync ran: clean`/`: surfaced` with no date slot, so the #581 silent-success watchdog — which parses `task_run_logs.result` for `ran <YYYY-MM-DD>:` — classified a healthy run as `EMPTY (FRESH)` instead of `PASS`. The format only became observable after #45 fixed the underlying `sync-tripit.sh` failure that previously masked it. The marker now mirrors the sibling `nightly-cfp-sync` / `nightly-order-sync` template: `nightly-travel-sync ran <slot_key>: clean` (or `: surfaced`), where `<slot_key>` is today's UTC date in `YYYY-MM-DD` form. SKILL.md-only edit; no code change.

### Fix — ship `sync-tripit.sh`, the host-op wrapper missed in the #318 migration (`jbaruch/nanoclaw-flight-assist#45`)

The #299/#318 split moved `nightly-travel-sync`'s three Python travel-source scripts into this tile (PR #42) but dropped `sync-tripit.sh`, the wrapper the `mcp__nanoclaw__sync_tripit()` host op resolves as `<groupDir>/scripts/sync-tripit.sh`. With no skill shipping it, fresh container spawns land without the file and the host op fails with `sync-tripit.sh not found` — surfaced in `nightly-travel-sync` Step 1, already broken in `telegram_swarm` (`telegram_main` only still worked off a stale Apr-27 copy a fresh spawn would lose). This adds the wrapper under `skills/nightly-travel-sync/scripts/` — the bundle whose Step 1 invokes the op — `cd`-ing into the globally-installed `reclaim-tripit-timezones-sync` and running `node sync.mjs sync --output=json` under `set -euo pipefail`. The package is an orchestrator-image global (`Dockerfile.orchestrator`, jbaruch/nanoclaw) that a skill bundle can't declare itself, so the wrapper guards for it and exits with an actionable message naming the install site rather than a bare `cd` error when it's absent. Scripts ship with their skill dir, so no manifest edit. A smoke/contract test (`tests/test_sync_tripit_script.py`) locks the host-op contract: the script exists, is executable valid bash, runs under strict mode, invokes the sync entrypoint, and fails loudly when the package is missing.

### Fix — `wake_rules.py` detection gaps: pre-existing schedule slip + inbound-delay retraction (`jbaruch/nanoclaw-flight-assist#46`, `jbaruch/nanoclaw-flight-assist#48`)

Two symmetric blind spots in `detect_wake_events`, both leaving the operator with a stale read of a flight:

- **#46 — pre-existing schedule slip never fired `delay`.** Delay detection was purely a delta between consecutive `dep_time` polls, so a delay already baked into the *first* snapshot never surfaced (KL1017 AMS→LHR sat at `scheduled+31min` across every poll with no prior `dep_time` to delta against; `last_wake_at` stayed null). `detect_wake_events` now takes the flight's `scheduled_dep_time` (a top-level state field, not part of the `last_snapshot` shape) and, on the first cycle only, fires a `delay` (with `schedule_slip: True`) when `dep_time` slips ≥ threshold past schedule. First-cycle-only gating means the persistent slip surfaces once and the delta rule owns every later shift, so it can't re-fire each poll. `precheck.py` resolves `scheduled_dep_time` before the wake-rule call and passes it through.

- **#48 — no event when an inbound-delay prediction walked back.** `inbound_delay_predicted` fired on the way up but nothing fired on the way down, so after byAir escalated DL59's inbound to "connection missed, rebook now" and then retracted the prediction to `null` (both legs ultimately landed early), the last surface the operator saw for hours was "rebook now" — silence read as "still bad". A symmetric `inbound_delay_retracted` event now fires when a previously-surfaced prediction (≥ threshold) walks back below threshold or to null, carrying `prev_delay_minutes`/`new_delay_minutes` so the agent can compose an all-clear. Mutually exclusive with the prediction rule.

14 new `test_wake_rules.py` cases cover both: first-cycle slip at/above/below threshold, on-time, early, missing `scheduled_dep_time`, the persistent-slip no-re-fire guarantee; and retraction to null, below threshold, inbound-block-absent, partial-walk-back-still-above-threshold (no retraction), prior-below-threshold (nothing to retract), first-cycle, and prediction/retraction mutual exclusion.

### Test — restore the #41 lodging-pairing regression tests (`jbaruch/nanoclaw-flight-assist#41`)

The #41 fix in `refresh-travel-schedule.py` (keep a past `Check-in:` whose matching `Check-out:` is still live, paired by trip-ID + hotel) shipped via the #318 extraction, but its four regression tests were dropped in transit — the fix landed uncovered in 0.1.22. This restores `test_lodging_checkin_retained_while_stay_live`, `test_lodging_fully_past_stay_dropped`, `test_lodging_checkin_not_rescued_across_trips`, and `test_lodging_pairing_requires_trip_id`, which lock the pairing behaviour against regression. No production-code change.

### Added — operator-local-tz phrasing for flight-assist surfaces (`jbaruch/nanoclaw-admin#305`)

Companion to admin#305, which fixed maintenance surfaces (`heartbeat`, `morning-brief`) to phrase relative dates in the operator's timezone but left the flight-assist `day_before` surface — the one whose 2026-05-24 incident ("leg 1 today" at 21:36 the night before, container UTC already rolled to the next day) prompted the issue — to a separate fix. This is that fix.

New `rules/operator-local-tz-phrasing.md` (steering, `alwaysApply`) requires every relative-date word a flight-assist surface composes ("today" / "tomorrow" / "a travel day") to be labeled against the operator's local date. New `skills/flight-assist/scripts/read-current-tz.py` resolves `current_tz` from the host `tz_state` singleton at `/workspace/store/messages.db` (mounted RW in main/trusted containers). The overlay reads that store directly rather than admin's `heartbeat-precheck.json`, so it carries no `nanoclaw-admin` dependency; it fails open to `available: false` on any miss (missing DB/row, empty column, unsupported `schema_version`, unparseable zone) so a notification still fires with explicit-date phrasing. `home_tz` is deliberately not a fallback — relative-date phrasing needs where the operator is now.

Scope is narrow: only the today/tomorrow wording. Displayed flight clock times stay in the airport-local zone byAir provides (`flight-data-locality` / byAir's "show as-is, don't convert" contract) — the rule never converts a departure/arrival time. SKILL.md Step 3 routes the `day_before` and arrival/delay/time-to-leave surfaces through the rule. Unit tests cover the reader's resolve + every degrade path.

### Added — `nightly-travel-sync` bundle finishes the #299 reader-without-writer split (`jbaruch/nanoclaw-admin#318`)

#299 moved `check-travel-bookings` (the reader of `travel-db.json`) into this tile but left the **writers** behind in `nanoclaw-admin`'s `nightly-external-sync` bundle, so every chat loading the flight-assist overlay still required `nanoclaw-admin` just to refresh the data it consumes. This extracts the remaining travel-source scripts and the bundle steps that drive them into a flight-assist-owned skill.

New `skills/nightly-travel-sync/`:
- `SKILL.md` — daily bundle (TripIt → Reclaim sync, refresh `travel-schedule.json`, two-tier Gmail freshness probe, rebuild `travel-db.json`, run `check-travel-bookings`). Independently scheduled via `cadence:`+`script:` frontmatter — it materialises its own `scheduled_tasks` row and no longer depends on the admin bundle or admin's `resumable-cycle` machinery. A step failure surfaces a note and finishes; the daily cron + freshness probe recover the next run.
- `precheck.py` — gates the wake to a 3-day cadence anchored on `travel-db.json` mtime (the bundle's terminal artifact, the file downstream consumers read). No separate cursor file, so the gate adds no self-owned state. Fails open (wake) on internal error so a transient stat error can't freeze the pipeline.
- `scripts/refresh-travel-schedule.py`, `filter-tripit-bookings.py`, `check-travel-freshness.py` — moved from `nanoclaw-admin/skills/nightly-external-sync/scripts/`, reformatted for this tile's ruff config (double quotes, bugbear `B` enabled — the ICS-field/datetime helpers were hoisted out of the parse loop to satisfy `B023`). The admin bundle's `sync-tripit.sh` was a zero-reference orphan that shelled out to an npm package present only in the orchestrator container, not the agent container; it was dropped rather than carried as dead code (Step 1 uses `mcp__nanoclaw__sync_tripit`, the IPC-integrated path the admin bundle already used).
- The admin bundle's `references/two-tier-probe.md` was **not** carried over — as a loaded reference it was almost entirely rationale + restated filter behavior, which `coding-policy: context-writing-style` / `script-as-black-box` keep out of loaded artifacts. Its one executable directive ("never alert on `travel-schedule.json` mtime alone; escalate only on a `stale` status plus a matching TripIt forwarded-confirmation email") now lives inline in SKILL.md Step 3. Archived motivation: bare-mtime alerting was a false-positive engine — a stale `travel-schedule.json` usually just means no travel was booked recently (confirmed 2026-04-25, "Не, я просто давно не букал травел." — "I just haven't booked travel in a while"), which trained the operator to dismiss the channel. The classification detail the reference used to enumerate — TripIt Pro alerts, friend-shared trips, geofenced arrival marketing, and platform announcements are all excluded, only the forwarded-confirmation subject matches — lives solely in `filter-tripit-bookings.py` (`PREFIX`).

The `refresh-travel-schedule.py` extracted here carries the **#41 lodging fix** (keep a past `Check-in:` whose matching `Check-out:` is still live, paired by trip-ID + hotel) plus its four regression tests, superseding the in-flight admin PR #317. Step 3's Gmail fallback discovers `GMAIL_FETCH_EMAILS` inline via `COMPOSIO_SEARCH_TOOLS` rather than depending on an admin steering rule, keeping the bundle self-contained. Tests + conftest fixtures (`refresh_travel_schedule`, `filter_tripit_bookings`, `check_travel_freshness`, `nightly_travel_sync_precheck`) moved alongside the scripts. `travel-schedule.json` / `travel-db.json` stay at `/workspace/group/`, so admin's cross-tile readers (`check-orders`, `morning-brief`) are unaffected.

### Fix — size the precheck poll-loop headroom for the Maps call, not just byAir (`jbaruch/nanoclaw#562`)

Follow-up to #36's wall-clock budget. `execfile-error` kills kept recurring at ~34s (2026-05-27, 2026-05-29) — surfaced again while tracing the heartbeat wake-storm in `jbaruch/nanoclaw#562`, because each transient flight-assist crash pins heartbeat's 24h task-failure window open. #36 set `_CYCLE_POLL_HEADROOM_SECONDS = 10s`, reserved before the 30s hard-kill for "one in-flight poll" — but it only counted the byAir poll (8s) and ignored the Maps `travel_time` query that `_process_flight` runs on top of it. `_maybe_maps_client` instantiated `MapsClient.from_env()` with its 10s default, so a flight started just under the budget ran byAir (8s) + Maps (10s) ≈ 18s and overran the kill.

The Maps client now takes the same bounded per-call timeout as byAir (`_MAPS_CALL_TIMEOUT_SECONDS = 8.0`), and `_CYCLE_POLL_HEADROOM_SECONDS` is derived from `byair + maps + interpreter-teardown` (20s, leaving a 10s start-budget) so the headroom is correct by construction if either timeout changes. Regression coverage: `test_run_cycle_passes_bounded_per_call_timeout_to_maps_client` pins the kwarg; `test_poll_headroom_covers_byair_plus_maps_worst_case` asserts the headroom ≥ byAir + Maps.

### Changed — cap the precheck poll horizon at 24h (`jbaruch/nanoclaw-flight-assist#38`)

Root-cause follow-up to #36. The live index tracked 25 active flights with departures spread out to ~44 days, all polled on the 30-min `scheduled` cadence; their `last_polled_at` values cluster, so large batches (e.g. 17 flights) come due in a single cycle and the sequential byAir polls are what race the 30s execFile kill. `_due_for_poll` now skips any flight whose seeded `scheduled_dep_time` is more than `_POLL_HORIZON_HOURS = 24` away — it stays in `active-flights.json` (sync keeps the roster) but costs no byAir call until it crosses T-24h, at which point the first in-window poll fires `day_before`. The horizon clips nothing: T-24h is the earliest precheck event, and `connection_risk` already gates leg-1 on its own 24h lookahead and falls back to `scheduled_arr_time` for legs without a live snapshot, so horizon-skipped flights remain no-ops there. This shrinks the per-cycle poll batch at the source rather than only bounding it after the fact (#36's wall-clock budget remains the safety net). Regression coverage: `test_poll_horizon_skips_flight_departing_beyond_24h`, `test_poll_horizon_polls_flight_just_inside_24h`.

### Fix — bound `_run_cycle` to a wall-clock budget so slow multi-flight cycles don't trip the 30s kill (`jbaruch/nanoclaw-flight-assist#36`)

AyeAye flagged recurring `precheck script failed: execfile-error` on the `tessl__flight-assist` scheduled task (~5 fires over 4 days, each self-recovering next cycle), with every error row clustered at ~34–35s duration. Root cause: `_run_cycle` polls active flights sequentially, and #28 bounded each byAir call at 8s but not the cumulative total. With several active flights on slow upstreams the per-flight timeouts summed past the agent-runner's `SCRIPT_TIMEOUT_MS = 30s` execFile hard-kill (`container/agent-runner/src/index.ts`), killing the whole precheck — the observed ~34s being the 30s execFile timeout plus spawn/teardown.

`_run_cycle` now enforces an overall wall-clock budget (`_SCRIPT_KILL_BUDGET_SECONDS - _CYCLE_POLL_HEADROOM_SECONDS` — the 30s kill minus headroom for one in-flight poll plus interpreter startup/teardown). Before processing each flight it checks elapsed monotonic time; once the budget is reached it stops and defers the remaining flights to the next cycle, leaving their `last_polled_at` untouched so the cadence gate retries them — the same degraded-poll contract as the existing transient-transport branch. Deferred flights join the connection-risk exclusion set (`removed_upstream_ids | poll_failed_ids | deferred_ids`) because their snapshot wasn't verified this cycle. The budget clock is injected (`monotonic=time.monotonic`) so tests drive it deterministically without sleeping. Full per-flight concurrency (parallel byAir polls so total ≈ the slowest single call) would cut latency further but is a larger change, deferred as follow-up. Regression coverage: `test_wall_clock_budget_defers_remaining_slow_flights`, `test_connection_risk_excludes_budget_deferred_flights`.

### Fix — sync only the operator's own trips, not friends' (`jbaruch/nanoclaw-flight-assist#29`)

`sync_tripit._run_sync` called `byair.list_trips(status="active")` with no ownership argument, so the client default `ownership="all"` pulled friends' tracked trips into `active-flights.json`. The precheck then surfaced `[M]` wake events (delay, gate change, boarding) for flights the operator isn't on and can't act on — pure noise. The sync now requests `ownership="mine"`, so friends' flights never enter the index. The request-side filter is authoritative; the per-flight `ownership` field in the response is unreliable (defaults to `"mine"` when byAir omits it). Regression coverage: `test_sync_fetches_only_owned_trips`.

Deploy note: on the first sync after this ships, friends' flights already in `active-flights.json` reconcile as removed and would emit `tracked_flight_removed` (surfaced per SKILL.md). Prune those entries from the NAS state at deploy time to avoid a one-time "stopped tracking" burst. On-demand lookup of a friend's flight is tracked separately (expose byAir as an MCP tool to the agent).

### Fix — `build_lodging_ranges` no longer collapses repeat stays at one hotel (`jbaruch/nanoclaw-flight-assist#24`)

`check-travel-bookings.py:build_lodging_ranges` keyed check-in / check-out dates in dicts by hotel name alone, so a trip that bookended the same hotel (stay → other cities → same hotel again) overwrote the first stay and produced at most one range per hotel — under-reporting lodging coverage and surfacing false uncovered nights. Per-hotel events are now replayed in date order with a check-out closing the most recently opened stay (LIFO); same-hotel stays don't overlap, so the open stay is the one a check-out belongs to. This keeps a stray earlier check-out from matching a later check-in and an orphan earlier check-in from stealing a later stay's check-out (both would misreport coverage). Orphan check-outs form no range; unmatched check-ins keep the existing 1-day fallback. Unique-per-hotel trips (the common path) are unaffected. Regression coverage: `test_build_lodging_ranges_multiple_stays_same_hotel`, `test_build_lodging_ranges_same_hotel_extra_checkin_defaults_one_day`, `test_build_lodging_ranges_stray_earlier_checkout_not_consumed`, `test_build_lodging_ranges_orphan_earlier_checkin_not_stealing_later_stay`.

### Fix — don't flag same-day trips as missing hotel (`jbaruch/nanoclaw-admin#310`)

`check-travel-bookings.py`'s issue selector flagged any trip with transport and no lodging as "рейсы есть, отеля нет", including same-day round trips that need no overnight stay (Agentcon Miami: out + back on 2026-06-12, the return leg's arrival slipping to the next UTC day). The branch now treats a trip as needing no hotel only when the traveller is still in transit at the end of the trip window — the latest transport arrival within the trip reaches `trip_end`, as in a same-day round trip whose return slips past UTC midnight, or a red-eye that lands on the final day. When the latest arrival falls before `trip_end` the traveller has landed and is staying over. A missing hotel still surfaces in that case, including one-night single-leg trips and connecting outbounds, both of which `classify_trip`'s `has_future_transport` guard leaves with empty `uncovered_nights`. The signal is arrival-vs-`trip_end` rather than raw leg count. Two same-direction legs (a connecting outbound) are not a round trip, and leg count alone would misclassify them as one. Regression coverage: `test_classify_trip_same_day_round_trip_no_uncovered`, `test_main_same_day_trip_no_false_hotel_gap`, `test_main_one_night_single_leg_no_lodging_flagged`, `test_main_one_night_connecting_outbound_no_lodging_flagged`, and `test_main_multiday_single_transport_no_lodging_flagged`. The skill's output-contract doc was updated to match.

### Fix — bound `ByAirClient` per-call timeout in precheck to 8s (`jbaruch/nanoclaw-flight-assist#28`)

`precheck._run_cycle` now instantiates `ByAirClient.from_env(timeout=8.0)` instead of relying on the default 30s. A single slow byAir response previously raced the 30s `execFile` budget in `agent-runner` and surfaced as `precheck-error: execfile-error` — killing the whole cycle and producing a `task_run_logs` `status='error'` row that pinned `nanoclaw-admin` heartbeat into `system_health_issues` wake mode for 24h. With the per-call timeout below the outer budget, slow upstream calls fall through the existing transient-transport branch in `_run_cycle`, which skips the affected flight for one cycle (cadence gate retries it next tick) and lets other flights' polls complete.

Companion change in `ByAirClient._http_post`: `urlopen(..., timeout=X)` wraps connect-side socket timeouts as `urllib.error.URLError`, but a timeout during `response.read()` of the body propagates raw `TimeoutError` (since `socket.timeout` is aliased to `TimeoutError` in Python 3.10+). The body-read path is now wrapped to normalize `TimeoutError` into `URLError`, so `_run_cycle`'s transient-transport branch catches every transport timeout uniformly rather than letting body-read timeouts fall through to the outermost `precheck_exception` boundary and re-create the original cycle-kill symptom. Regression test: `test_body_read_timeout_surfaces_as_urlerror`.

### Fix — `_due_for_poll` forces a poll when `last_snapshot` is None (`jbaruch/nanoclaw-flight-assist#26`)

`_due_for_poll` now short-circuits to True when `last_snapshot is None`, so sync_tripit-seeded flights get polled on the next precheck tick instead of waiting up to a full cadence interval. Regression coverage: `test_seeded_state_with_no_snapshot_forces_poll`; two connection-risk tests updated to use a benign scheduled snapshot via the new `_scheduled_snapshot` helper.

### Added — `check-travel-bookings` migrated from `nanoclaw-admin` (`jbaruch/nanoclaw-admin#299`)

Per-chat travel concerns now consolidate under `nanoclaw-flight-assist`: flight notifications, time-to-leave, connection risk, arrival logistics, and now booking-gap detection. Coherent domain, single tile, single co-load for affected chats.

Migration is structural. `skills/check-travel-bookings/scripts/check-travel-bookings.py` and `skills/check-travel-bookings/scripts/build-travel-db.py` carry across with these edits, all from review feedback during PR #22:

- Non-behavioral cleanup: ruff-driven formatting (single → double quotes); B007/F841 `slug` / `item_count` dead-variable cleanup in `build-travel-db.py`; explicit `encoding="utf-8"` on file opens
- Hardening: `build-travel-db.py` now writes `travel-db.json` atomically (same-dir `.tmp` + `os.replace`) matching the `_atomic_write_json` pattern in `skills/flight-assist/state.py` — readers no longer see a half-written DB if the process is killed mid-write
- Diagnostic accuracy: `check-travel-bookings.py` adds `ensure_ascii=False` on the error-JSON path so operator-facing diagnostic messages keep their non-ASCII punctuation intact
- Stateful-artifacts contract — `travel-db.json` and `travel-booking-state.json` carry `schema_version: 1` per `coding-policy: stateful-artifacts`. New `state-schema.md` sibling documents owner / writers / readers / migration policy for both artifacts. The writer (`build-travel-db.py`) stamps `schema_version` on every output; the reader (`check-travel-bookings.py`) gates on it with explicit branches for legacy-implicit-v1, forward-incompatible (`> 1`), and non-int values. Snooze entries in `travel-booking-state.json` carry the same per-record field. Nine new tests pin the contract: DB at v1 / missing / forward / non-int; snooze entries at v1 / legacy / forward / non-dict-corrupt
- Test infra: `tests/conftest.py:_load` asserts `spec` and `spec.loader` are non-None so fixture-loading misconfigs fail at `_load` time with an actionable message instead of a deeper `AttributeError`

`skills/check-travel-bookings/SKILL.md` was restructured to follow `skill-authoring`'s execution-mode preamble + flat numbered step format (policy reviewer feedback); content is preserved, and Step 3 now instructs the agent to stamp `schema_version: 1` on snooze entries. The `gaps[]` example payload includes the `uncovered_nights` field the script actually emits.

Resolves the stateful-artifacts gap originally filed as #23 — that issue can close once this PR merges. The literal mount path `/home/node/.claude/skills/tessl__check-travel-bookings/scripts/<file>.py` used by `nightly-external-sync` Step 5 (`build-travel-db.py`) and `morning-brief` (`check-travel-bookings.py`) resolves to whichever tile owns the `check-travel-bookings` skill name — both consumers continue to work without code changes since the name doesn't change.

Tests follow: `tests/test_check_travel_bookings.py` and `tests/test_build_travel_db.py` migrated. New `tests/conftest.py` adds the two fixtures (`check_travel_bookings`, `build_travel_db`) ported from admin's conftest. Both scripts read/write `/workspace/group/travel-db.json` and `/workspace/group/travel-booking-state.json`; the writer chain (`refresh-travel-schedule.py` → `build-travel-db.py`) is unchanged from admin's perspective.

State plane note: existing `/workspace/group/travel-db.json` and `/workspace/group/travel-booking-state.json` carry across the migration as-is — they're group-scoped state files, not tile-shipped artifacts, so the deploy preserves operator-side snooze/resolve history.

### Review fixup (#21) — non-owner snapshot reader API + boundary handler at main()

OpenAI policy reviewer requested changes on two precondition violations in PR #21 (commit 5103c8f). Both addressed here:

1. **Module-level catch-all in `skills/sync-tripit/precheck.py` broadens the `error-handling` outer-boundary carve-out.** Removed the bootstrap try/except wrapping the cross-skill import. Path resolution + `sys.path` injection + the `state` import now live in a new `_load_flight_assist()` helper invoked from inside `main()`'s try block, so the sole catch-all sits at the outermost process boundary as the carve-out requires. The `_load_flight_assist` failure path is exercised by `test_main_bootstrap_failure_emits_safe_json`.

2. **Non-owner reader could invoke owner-side migrations.** `sync-tripit`'s precheck previously called `state.read_active_flights()` / `state.read_flight_state()`, both of which silently invoke `_migrate` and rewrite the file on `schema_version` mismatch — a violation of the single-owner contract in `coding-policy: stateful-artifacts`. Added a `migrate=True` kwarg to `_read_json_with_version` and exposed two dedicated non-owner reader entry points: `read_active_flights_snapshot()` and `read_flight_state_snapshot(flight_id)`. The snapshot readers treat any older `schema_version` as "no usable prior state" (return `[]` / `None`) and never write to disk; integrity failures (corrupt JSON, higher-than-current schema, missing required field at the current schema) still raise `StateError`. `precheck.py` now uses these snapshot readers. Owner-side `flight-assist` code paths are unchanged. `state-schema.md` documents the new reader contract.

Test coverage extended: gate tests now exercise the snapshot API; new `test_should_sync_does_not_migrate_old_active_flights` asserts the file's bytes + mtime are unchanged after a precheck run against a v1-schema state file; six new tests in `tests/test_state.py` cover the snapshot reader API (missing file, current payload, old-schema no-migrate, corruption, future-version, flight_id validation).

### Feat — adaptive scheduler for sync_tripit (new `sync-tripit` skill)

`sync_tripit.py` shipped at v0.1.0 with the docstring claim "Run cadence: daily at ~04:00 local" but never had a cadence-registry entry — the orchestrator never invoked it, so `active-flights.json` was never populated, and the existing 2-min flight-assist precheck loop fired into an empty state file every cycle. This is the orchestration half of the two-bug stack diagnosed live 2026-05-22 (byAir HTTP 400 was the transport half, addressed in PR #20).

The naïve fix would be `cadence: "0 4 * * *"` on flight-assist's existing frontmatter — but flight-assist already declares a `cadence:` for its 2-min precheck, and each SKILL.md gets one cadence-registry row. Beyond the structural constraint, daily-at-04:00 doesn't match the access pattern: day-of-travel changes (delays, gate moves, cancellations) need responsive polling, while between-travel-window periods don't justify any byAir traffic. Per the operator-stated requirement, the cadence should be ~5 minutes when there's a flight in the next 24 hours, idle otherwise.

New `skills/sync-tripit/` with `cadence: "*/5 * * * *"` + `script: "precheck.py"`. The precheck implements an adaptive gate: any tracked flight with `scheduled_dep_time` in the next 24h triggers a byAir round-trip; `active-flights.json` mtime older than 6h triggers a sync (catches newly-booked trips landing between travel windows); no state file yet triggers cold-start; otherwise emit `wake_agent: false` with no byAir call. When the gate passes, the precheck delegates to `flight-assist/sync_tripit.py` via subprocess and forwards its stdout — `sync_tripit.py` already emits the `{wake_agent, data}` wake-payload contract this script needs, so composition lives in one place.

Cross-skill import: `precheck.py` reads `state.py` and locates `sync_tripit.py` via the runtime mount path `/home/node/.claude/skills/tessl__flight-assist/` with a dev-clone-relative fallback (`../flight-assist`) for tests. Both skills ship from the same tile and are always co-deployed; if `flight-assist` is missing, the precheck raises `FileNotFoundError` at import time rather than silently failing.

Outer-boundary-process-contract handler in `main()` catches unexpected exceptions, emits safe-shape `{"wake_agent": false, "data": {"reason": "precheck_internal_error"}}`, exits 0 — the agent-runner reads non-zero exit OR invalid stdout JSON as `wake_agent: false`, which here would silently disable the entire flight-assist polling pipeline. Subprocess timeout (60s budget) surfaces as `sync_subprocess_timeout`; empty-stdout subprocess crashes surface as `sync_no_output`.

Adds `tile.json` entry for the new skill. Adds 13 mocked tests in `tests/test_sync_tripit_precheck.py`: gate correctness across cold-start / empty-recent / stale / imminent / out-of-window / past / multi-flight first-match / malformed-dep-time cases; subprocess delegation and forwarding; subprocess-timeout safe-shape conversion; outer-boundary exception handling. `tessl skill review` 87% (threshold 85). All pre-existing tests still pass.

### Fix — byAir MCP client `Accept` header missing `text/event-stream` (HTTP 400 on every call)

`byair_client.py` set `Accept: application/json` on both `_initialize` and `_tools_call` outbound headers. The byAir MCP streamable-HTTP endpoint rejects with `HTTP 400 — "Accept must contain both 'application/json' and 'text/event-stream'"` because the MCP streamable-HTTP spec requires clients to advertise support for both response shapes (servers may stream tool responses via SSE). The `_SessionExpired` retry path doesn't engage on `initialize` because `self._session_id is None` at that point, so the original 400 propagated and the client could not complete the handshake. Result: every byAir call from a fresh process failed at the handshake, and `sync_tripit.py` could not populate `active-flights.json`. Combined with the orchestration gap leaving `sync_tripit` unscheduled, the precheck loop fired every 2 min, read an empty state file, and emitted `wake_agent: false` on every cycle — the skill was "installed but deaf" on flight days.

Two-line fix: `Accept: "application/json"` → `Accept: "application/json, text/event-stream"` at both header sites. Verified live against the production byAir endpoint 2026-05-22 — `initialize` returned HTTP 200 with a valid `mcp-session-id`. Q-value forms (`application/json; q=1.0, text/event-stream; q=0.1`) are NOT accepted by the byAir server — it does substring matching after splitting on `,` and the parameter-suffixed entries don't match the bare-token check; the verified-working form is the plain comma-separated list.

Defensive `Content-Type` guard added in `_http_post`: since the client now advertises `text/event-stream`, a server could pick SSE for some response. We don't parse SSE here (the operations this client uses — `initialize`, `notifications/initialized`, non-streaming tool calls — all return JSON in practice). The guard raises a clear `ByAirError("unsupported_response_shape", ...)` if Content-Type comes back as `text/event-stream`, rather than letting `json.loads` fail with a cryptic decoder error on `event:` / `data:` SSE prefixes.

Adds two regression tests in `tests/test_byair_client.py`: one asserts the outbound `Accept` header includes both content types on initialize, notification, and tool-call requests (via mocked `urlopen` per `coding-policy: testing-standards`); the other asserts the Content-Type guard raises the actionable error on a mocked SSE response. 15/15 byair_client tests pass.

The orchestration gap (no cadence-registry entry, no scheduled-task row for `sync_tripit` itself) lands separately — fixing the transport doesn't help if nothing ever invokes the feeder.

### Fix — install wake task + origin-resolution ladder (`jbaruch/nanoclaw-flight-assist#17`, `#18`)

Two coupled bugs that left the skill installed-but-silent on a mobile traveller's flight days.

**#17** — `precheck.py` never fired. The scheduled-task row that runs the precheck every 2 minutes is provisioned via the host orchestrator's cadence-registry (host repo `src/cadence-registry.ts`), which reads `cadence:` + `script:` from each installed skill's SKILL.md frontmatter on container spawn. `flight-assist/SKILL.md` carried no such declaration, so the registry walked past it and never created the row. Verified live 2026-05-20: no `scheduled_tasks` row matched any flight-assist / byair prompt despite the skill being fully installed. Add `cadence: "*/2 * * * *"` + `script: "precheck.py"` to the frontmatter; the cadence-registry's rebuild on the next container respawn provisions the row. The existing per-flight cadence ladder inside `_interval_for` still gates byAir calls per-flight, so the 2-min wake floor doesn't translate into 2-min byAir traffic.

**#18** — `time_to_leave` resolved origin exclusively from `config.json:home_address`. A constant traveller (rarely at home on flight days) got either silent failure (no home base set) or structurally wrong notifications (home base set but user is 5000 km away). Add an origin-resolution ladder in `precheck._resolve_time_to_leave_origin`: (1) fresh `/workspace/state/flight-assist/current-location.json` snapshot (≤ 30 min old, formatted as `"lat,lng"` for Distance Matrix) → (2) `home_address` fallback → (3) `None` (skip the maps query when neither is available). The snapshot file is **host-orchestrator-owned** — flight-assist is a non-owner reader per `coding-policy: stateful-artifacts`, validates the documented shape, and returns `None` on any mismatch instead of raising. Without the orchestrator-side write the precheck behaves exactly as today (home_address-only); once the orchestrator's location-write companion lands the live ladder takes over. State-schema doc updated to describe the new file shape and reader contract.

### Skills — added

- **`skills/flight-assist/connection_risk.py`** — V1.1 cross-flight connection-risk detector (capability 4 of the V1 spec, previously deferred). Pure-function detector that groups on-disk per-flight state by `trip_id`, sorts each group by `scheduled_dep_time`, and walks consecutive (leg-1, leg-2) pairs where `arr_airport_id(leg-1) == dep_airport_id(leg-2)`. Emits `connection_at_risk` events when the projected transfer window (`scheduled_dep(leg-2) - projected_arr(leg-1)`, taking leg-1's live `arr_time` when populated and `scheduled_arr_time` as fallback) is below `min_transfer_minutes` (config-overridable, default 45). Suppression rules: leg-1 status in `{landed, cancelled, diverted}`, leg-1 scheduled departure > 24h away, `connection_at_risk_fired` already True on leg-2's marker. The event is keyed on leg-2's `flight_id` so the once-per-flight marker survives leg-1 landing. Closes #14.

- **`skills/flight-assist/precheck.py`** — post-loop pass `_check_connection_risks` runs after every cycle's per-flight processing, reads the now-up-to-date flight states, calls `detect_connection_risks`, and flips `connection_at_risk_fired` on each fired leg-2 record before emitting the event. `_initial_phase_markers` includes the new marker key.

- **`skills/flight-assist/SKILL.md`** — composition table gains a `connection_at_risk` row that renders the tight-connection notification. Description triggers extended with "connection at risk" / "tight connection alert" so the runtime matches the new intent.

- **`skills/flight-assist/references/event-payloads.md`** — new "Cross-flight" section documenting the `connection_at_risk` shape and suppression rules.

- **`skills/flight-assist/sync_tripit.py`** — `_initial_state` includes `connection_at_risk_fired: false` on the new-flight phase_markers dict.

### State schema — v1 → v2

- **`skills/flight-assist/state.py`** — `STATE_SCHEMA_VERSION` bumped to `2`. Owner-side migration (`_migrate`) handles the v1 → v2 upgrade: adds `connection_at_risk_fired: false` to per-flight `phase_markers`, rewrites at v2. Config and active-flights files have no shape change at v2; they receive a schema_version bump only via the same migration code path on first read. Per `coding-policy: stateful-artifacts` "Migration Policy", only the owner skill migrates — reader skills from other tiles continue to get `StateError` on mismatched version. Strict reader contract preserved: missing/wrong-type `schema_version`, schema_version higher than current, and corrupt JSON still raise `StateError` with actionable repair guidance.

- **`skills/flight-assist/state.py`** — `_PHASE_MARKER_KEYS` extended with `connection_at_risk_fired`; the read+write validators reject phase_markers dicts missing the new key or carrying it as a non-bool. `_CONFIG_OPTIONAL_FIELDS` extended with `min_transfer_minutes: int` (with explicit bool-rejection on int fields to match the rest of the validator family).

- **`skills/flight-assist/state-schema.md`** — documents v2 shape, the new `connection_at_risk_fired` marker (carried on leg-2 so it survives leg-1 landing), and the new optional `min_transfer_minutes` config field. The Migration Policy section names the v1 → v2 migration explicitly and reaffirms the owner-only migration discipline.

### Skills — added (V1)

- **`skills/flight-assist/sync_tripit.py`** — daily reconciliation of the active-flights index against byAir's `list_trips`. Reads upstream, diffs against the on-disk `active-flights.json`, writes initial state records for added flights, deletes state for removed flights, emits the same `{wake_agent: bool, data: {events: [...]}}` payload shape as `precheck.py` — sync adds use `reason: "tracked_flight_added"` and sync removes use `reason: "tracked_flight_removed"` so SKILL.md Step 3's composition table is the single consumer contract. Removed-flight events capture `code` + scheduled times BEFORE state deletion so the agent has the metadata it needs to render notifications. Same outer-boundary-process-contract carve-out as `precheck.py`. Exports `initialize_flight_from_byair()` for the precheck to call when it encounters a flight_id not yet on the index. stdlib-only.

- **`skills/flight-assist/SKILL.md`** — full V1 action-router SKILL.md. Three actions: `Diagnose env` (preserved from v0.1.0), `Set home base` (records `home_address` to config via `state.write_config`), and `Compose wake event notification` (per-event-type composition table covering all 10 documented wake reasons — `cancelled`, `diverted`, `gate_change`, `delay`, `inbound_delay_predicted`, `boarding_started`, `carousel_revealed`, `day_before`, `time_to_leave`, `arrival_logistics`, `removed_upstream`). Multi-event merging rule (one notification per flight per cycle, ordered by urgency). References `references/event-payloads.md` for the full event-shape contract. Skill review: 90% (Description 90, Content 85).

- **`skills/flight-assist/references/event-payloads.md`** — reference document for the precheck wake-event payload shapes. One section per `reason` with the JSON shape + when it fires + composition discipline.

- **`skills/flight-assist/precheck.py`** — the scheduler-invoked entry point that orchestrates byair_client + maps_client + state + wake_rules + phase_markers. Reads `active-flights.json`, cadence-gates each flight (per `state-schema.md`'s `last_polled_at` discipline), fetches new snapshots from byAir for due flights, runs delta detection (`wake_rules`) + time-based gates (`phase_markers`), persists updated state, and emits a single-line JSON payload on stdout: `{"wake_agent": <bool>, "data": {"events": [...]}}` per `coding-policy: script-delegation` "Precheck Gating". Uses the outer-boundary-process-contract carve-out: any unhandled exception is caught at the script boundary so the scheduler always sees safe-shape JSON + exit 0 (a bare programming bug would otherwise silently disable the wake contract). `_run_cycle()` takes `now_utc` as a parameter so tests pin the clock without monkey-patching `datetime`. Queries `maps_client.travel_time()` only for flights within the 6-hour time-to-leave window — preserves the Distance Matrix per-query budget.

- **`skills/flight-assist/phase_markers.py`** — time-based wake-gate functions for the precheck. Three once-per-flight events driven by wall-clock time alone (vs `wake_rules.py`'s delta detection): `day_before` (T-24h, capability 2's sanity check), `time_to_leave` (traffic-aware leave-by, capability 1), `arrival_logistics` (T-arr−15min, capability 6). Each function takes `phase_markers` (the per-flight state dict that tracks once-fired flags) plus a synthetic `now_utc` for deterministic testing. Returns `(should_fire, event_dict)`. `time_to_leave` consumes `travel_time_seconds` from `maps_client.travel_time().in_traffic_seconds`; defers when `None` (the caller decides when to query maps per the cadence-ladder budget). Pure functions, no I/O, no state mutation.

- **`skills/flight-assist/wake_rules.py`** — pure-function delta-event detector for the precheck. Takes `(prev_snapshot, new_snapshot)`, returns a list of wake events `[{"reason": "...", ...}, ...]`. Event types: `cancelled`, `diverted`, `gate_change` (with side + from + to), `delay` (with delay_minutes + new_dep_time, threshold ≥15 min), `inbound_delay_predicted` (threshold ≥20 min, dedupe within 5 min vs prior magnitude), `boarding_started`, `carousel_revealed` (with baggage claim). First-cycle behavior: `cancelled` / `diverted` fire from a None prev (the state itself is news), other rules require a prior snapshot. RFC3339 timestamps compared in UTC so DST/offset shifts don't false-positive. No I/O, no logging, no state mutation — per `coding-policy: script-delegation` (deterministic logic stays in scripts).

- **`skills/flight-assist/state.py` + `state-schema.md`** — per-flight state file read/write under `/workspace/state/flight-assist/` (configurable via `FLIGHT_ASSIST_STATE_DIR` env var for tests). Atomic writes (write-to-tmp + `os.replace`) so a kill mid-write doesn't leave a half-written file. Three file types: `config.json` (home_address from /setup), `active-flights.json` (index of tracked flight_ids), `flight-<flight_id>.json` (per-flight record with snapshot, phase_markers, last_polled_at). All carry `schema_version: 1`. `state-schema.md` documents the full per-record contract per `coding-policy: stateful-artifacts`. Owner skill: `flight-assist` — when a future schema bump ships, the owner skill adds migration branches that upgrade-and-rewrite. Read-side validation is strict: `schema_version` must equal `STATE_SCHEMA_VERSION` and be a plain `int` (no `bool`, no string); `flight_ids` must be a list of plain ints (no silent coercion). Mismatches raise `StateError` with actionable repair messages per `coding-policy: error-handling`. stdlib-only (`json` + `os` + `pathlib`).

- **`skills/flight-assist/maps_client.py`** — Google Maps Distance Matrix client for traffic-aware travel-time queries. Used by `phase_markers.py` (forthcoming) to compute the "leave by" deadline for the time-to-leave capability. stdlib-only (`urllib.request` + `urllib.parse` + `json`). Public API: `MapsClient.from_env()` + `travel_time(origin, destination) → TravelTime` (frozen dataclass with `duration_seconds`, `in_traffic_seconds`, `traffic_factor`, `distance_meters`, `origin_resolved`, `destination_resolved`). Uses `departure_time=now` + `traffic_model=best_guess` so every request includes a current-traffic estimate when the API returns one. `MapsError(status, message)` wraps non-OK top-level and per-element statuses (`NOT_FOUND`, `ZERO_RESULTS`, `OVER_QUERY_LIMIT`, `REQUEST_DENIED`, `MALFORMED_RESPONSE`); HTTP transport errors propagate as `urllib.error.HTTPError`.

- **`skills/flight-assist/byair_client.py`** — Python HTTP client wrapping the byAir streamable-HTTP MCP endpoint as a JSON-RPC API. Used by the (forthcoming) precheck script, not registered as a Claude MCP tool inside the agent container — the precheck filters the ~13KB raw byAir response down to a ~1KB operational slice before any state write, so the agent never sees the full payload. stdlib-only (`urllib.request` + `json`) per `coding-policy: dependency-management`. Public API: `ByAirClient.from_env()` + `get_flight()` / `list_trips()` / `get_flight_notifications()`. Wraps `isError: true` responses as `ByAirError(error_type, message)`; HTTP errors propagate as `urllib.error.HTTPError`. Sessions are managed lazily with one transparent re-init + retry on session-invalid 4xx; a second failure surfaces the underlying HTTPError so the caller sees the real transport error.

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-05-18). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out previously claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter. No `evals/` directory ships in this tile.

### Initial scaffold

- **`tile.json`** — declares `jbaruch/nanoclaw-flight-assist` 0.1.0, public, with one rule (`flight-data-locality`) and one skill (`flight-assist`)

- **`rules/flight-data-locality.md`** — byAir is the single source of truth for flight data; second flight-data upstreams are forbidden by default. The motivation behind the rule: byAir pre-computes phase logic (`computed_status`, `computed_phase_progress`, `computed_phase_risk`, `computed_phase_overdue`) and inbound-aircraft prediction (`inbound.predicted_delay`). Mixing a raw-status API would force a translation layer between two semantically-different models. The byAir Pro subscription covers every operational field this tile needs, so a second upstream would add a separate budget, a separate key, and a separate rate-limit posture for marginal data. When byAir reports "boarding" and a second API reports "scheduled", the reconciliation question has no clean answer — one upstream, one truth. Eval of byAir's MCP on 2026-05-17 confirmed all six target capabilities are addressable from byAir alone (with maps/traffic as a separate axis).

- **`skills/flight-assist/SKILL.md`** — minimal sequential-workflow skill with one step: run `check-env.py`, report missing credentials with actionable fix instructions. Will evolve into an action router as polling, state, and event composition land in subsequent PRs.

- **`skills/flight-assist/scripts/check-env.py`** — env-presence check for `BYAIR_MCP_URL` and `GOOGLE_MAPS_API_KEY`. Emits single-line JSON; exit 0 always (info-only, not a gate).

- **`.env.example`** — documents required environment variables per `coding-policy: no-secrets`, including the deep link to the GitHub Actions secrets configuration page so a new maintainer reaches the settings page in one click.

- **CI workflows** — `test.yml` runs ruff + pytest on every PR; `publish-tile.yml` uses `jbaruch/coding-policy/.github/actions/skill-review@<sha>` (the canonical changed-skills loop) before `tessl tile lint` and `tesslio/patch-version-publish` on `main`.

- **`pyproject.toml` + `requirements-dev.txt`** — pytest 8.3.4 + ruff 0.7.4, ruff scoped to `tests/` and `skills/` per `coding-policy: code-formatting` (every shipped Python file goes through lint + format check; new skill scripts under `skills/<name>/scripts/` inherit coverage automatically).

- **MIT license** — matches the public `nanoclaw-*` fleet.

- **`.tileignore`** — excludes repo-only files (CI, tests, build artifacts, dev-time tessl-install scaffolding) from the published Tessl tile per `coding-policy: context-artifacts`.
