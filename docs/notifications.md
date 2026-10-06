# ntfy notifications

Tokendrain can send usage reminders to [ntfy.sh](https://ntfy.sh) or your own ntfy server. Configure them under **Settings → Notifications**, then subscribe to the same server and topic in your ntfy app.

## Set up a destination

1. Enter the server URL, such as `https://ntfy.sh` or `https://ntfy.example.com`. Reverse-proxy prefix paths are supported. Use HTTPS when sending credentials across a network.
2. Choose a private or hard-to-guess topic. Public topics are readable by anyone who knows the name. Topic names use letters, digits, underscores, and hyphens.
3. If publishing requires authentication, enter an ntfy access token. Tokendrain stores it encrypted with the host master key, never returns it through the API, and does not supply it to project VMs. Leaving the field blank preserves an existing token; use **Remove saved access token** to clear it.
4. Save, then select **Send test notification**. Testing uses the saved destination and works even when automatic reminders are disabled.

Publishing uses ntfy's [JSON publishing API](https://docs.ntfy.sh/publish/#publish-as-json) and optional [Bearer access tokens](https://docs.ntfy.sh/publish/#access-tokens). Redirects are not followed. Each request has a ten-second timeout. Delivery errors appear in Settings without exposing upstream response bodies or credentials.

## Configure reminders

Add one or more rules and enable usage reminders. For example:

| Setting | Value |
| --- | --- |
| Usage window | `10080` minutes (weekly) |
| Reset within | `12` hours |
| Minimum remaining allowance | `80` percent |
| Limit ID | Empty to match any limit with that duration |

Both conditions must match: the reset is within the chosen time and at least the configured percentage remains. Remaining allowance means `max(0, 100 − used_percent)`: 20% used means 80% remaining. A weekly rule only matches a provider window whose reported duration is 10080 minutes; short windows are not inferred from primary/secondary labels. Use another duration for other windows, and optionally restrict a rule to a limit ID shown in usage observations.

The daemon checks every minute, including when projects are idle, and refreshes account metadata without starting inference when live usage is unavailable. Observations older than five minutes, missing reset times, and already expired windows cannot trigger a reminder. An unavailable Codex connection or ntfy server is retried on subsequent checks. Disable individual rules or the whole integration to pause reminders.

Successful delivery is recorded per destination, rule settings, limit, and reset timestamp, so subsequent checks and daemon restarts do not resend it. A new reset window can send again. Changing a rule or its destination can also send a new reminder. A crash after ntfy accepts a message but before the local delivery record is committed can result in a duplicate; delivery is not guaranteed to be exactly once.

The notification opens your configured `services.tokendrain.web.publicUrl`. It only reminds you: it does not launch or authorize runs. Provider usage and resets are observations, not guarantees of remaining usable quota.

## Run approvals

Under **Automations**, choose **Notify and wait for approval** to attach a usage condition to a saved run. Automation checks happen at daemon startup and every **15 minutes**. When the condition matches, Tokendrain saves a pending request and sends a notification to the destination configured here, even if reminder-only notifications are disabled.

Open the notification to review the projects, models, stop conditions, and current usage, then select **Authorize run**. Opening the link does not launch anything. Authorization refreshes usage and requires the enabled rule to still match. Requests expire at reset; editing, pausing, or deleting the rule cancels pending requests. Dismissing a request prevents another request for that same rule and reset window.

Failed notification delivery retries on the next automation check. The request remains available on the Automations page. Successful notification delivery is recorded across restarts, although a crash between delivery and recording can produce a duplicate notification. Authorization and run creation are transactionally deduplicated.
