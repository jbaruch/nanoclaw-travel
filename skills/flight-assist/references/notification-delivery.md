# Confirm delivered flight notices

Use each current wake event's `notification_id`. Do not infer delivery from a phase marker, a prior wake, calendar reconciliation, or final turn text. Send each queued notification through `mcp__nanoclaw__send_message`. Omit `sender` and `chat_jid`.

After the tool confirms a send, save only that message's included IDs in `/tmp/flight-assist-delivered.json`, using the input contract in `rules/flight-notification-delivery.md`.

Invoke the co-shipped helper using `rules/flight-notification-delivery.md`. In NanoClaw:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/confirm-notifications.py \
  < /tmp/flight-assist-delivered.json
```

Read the helper outcome through the named rule's output contract. Do not send a stale event again; the next precheck supplies the current observation. Retry only the acknowledgement when the send already succeeded. Do not resend its message to fix a store error.

A failed or uncertain send leaves the notice pending for a later verified retry. Do not claim exactly-once delivery: a crash between a successful Telegram send and this acknowledgement can cause a later duplicate. If one notification merges several events for a flight, acknowledge all and only those included IDs after that single successful send.
