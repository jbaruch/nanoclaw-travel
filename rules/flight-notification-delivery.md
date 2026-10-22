---
alwaysApply: false
applyTo: "skills/flight-assist/** — when invoking the co-shipped notification acknowledgement helper"
description: Native flight-assist helper lookup and delivery acknowledgement protocol
---

# Flight Notification Delivery Contract

## Lookup

- Scope: `jbaruch/nanoclaw-travel` flight-assist receipt acknowledgements.
- Co-shipped target: `skills/flight-assist/scripts/confirm-notifications.py`.
- Co-shipped dependencies: `skills/flight-assist/notification_delivery.py` and `skills/flight-assist/state.py`.
- In NanoClaw, invoke the target through the renderer-owned mount `/home/node/.claude/skills/tessl__flight-assist/scripts/confirm-notifications.py` with Python 3.11 or newer.
- In a repository checkout, invoke `skills/flight-assist/scripts/confirm-notifications.py`.
- In another Tessl consumer, invoke `.tessl/plugins/jbaruch/nanoclaw-travel/skills/flight-assist/scripts/confirm-notifications.py`.
- Keep the helper and its dependencies in the same installed skill bundle.
- Reinstall the plugin when a co-shipped file is missing.
- Never substitute an inline implementation.

## Input and output

- Standard input: one JSON object containing only `notification_ids`, an array of exact IDs from successfully delivered wake events.
- Exit 0: one JSON object containing `confirmed` and `stale` ID arrays.
- Exit 2: empty stdout and an actionable stderr diagnostic.
- The helper sends no messages.
- Invoke the helper only after `send_message` confirms success.
- A failed acknowledgement permits retrying the acknowledgement.
- Never resend an accepted message to repair an acknowledgement.
- Treat source-level events without IDs through their existing owner contract.

## Verification

- `tests/test_notification_delivery.py` exercises all three declared lookups from an unrelated working directory, receipt output, malformed input, store refusal and retry behavior.
- Grant no runtime authority through this contract.
- Preserve every required validation, review and publication check after lookup.
