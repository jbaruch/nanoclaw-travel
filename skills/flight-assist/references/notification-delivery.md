# Confirm delivered flight notices

Use each current wake event's `notification_id`. Do not infer delivery from a phase marker, a prior wake, calendar reconciliation, or final turn text. Send each queued notification through `mcp__nanoclaw__send_message`. Omit `sender` and `chat_jid`.

After the tool confirms a send, save only that message's included IDs as JSON data in `/tmp/flight-assist-delivered.json`:

```json
{"notification_ids":["<id from the delivered wake event>"]}
```

Invoke the co-shipped helper using `rules/flight-notification-delivery.md`. In NanoClaw:

```bash
python3 /home/node/.claude/skills/tessl__flight-assist/scripts/confirm-notifications.py \
  < /tmp/flight-assist-delivered.json
```

Exit 0 prints `{confirmed: [...], stale: [...]}`. Confirmed IDs are consumed; repeated acknowledgements are safe. Stale IDs cannot consume a replacement notice. Do not send a stale event again; the next precheck supplies the current observation. Exit 2 prints no stdout and an actionable stderr diagnostic. Retry only the acknowledgement when the send already succeeded. Do not resend its message to fix a store error.

A failed or uncertain send leaves the notice pending for a later verified retry. Do not claim exactly-once delivery: a crash between a successful Telegram send and this acknowledgement can cause a later duplicate. If one notification merges several events for a flight, acknowledge all and only those included IDs after that single successful send.
