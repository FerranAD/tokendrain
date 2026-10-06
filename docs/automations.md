# Usage automations

Use **Automations** to spend remaining usage before it resets. Create a rule, choose a saved run configuration, and select how it launches.

For example: **when weekly reset is within 12 hours and at least 20% remains, work these projects until the usage is exhausted**.

## Configure a rule

- Choose the usage-window duration reported by your provider: weekly is `10080` minutes. Optionally specify a limit ID to target one usage limit.
- Set the reset-within hours and minimum remaining percentage. Both conditions must match. For example, 20% used means 80% remaining.
- Choose **Launch automatically**, or **Notify and wait for approval** using your saved [ntfy destination](notifications.md).
- Select projects, models, reasoning, parallel execution, and stop conditions using the ordinary run controls.

Defaults are weekly, reset within 12 hours, at least 1% remaining, and approval mode. Runs default to reaching the triggering usage limit at 100% usage, or stopping when the provider prevents work or approved tasks finish. You can set a lower threshold or a runtime limit.

## Timing and limits

The daemon evaluates rules once at startup, then every **15 minutes**. Detection can take up to 15 minutes. It refreshes usage when needed without starting model inference; observations older than five minutes, absent reset timestamps, and expired windows cannot trigger.

Each rule launches at most one run per matching provider limit, window duration, and reset timestamp, including across daemon restarts. A new provider reset allows a new occurrence. Automatic launch conflicts, such as a project already being used by another run, retry on later checks while the rule still matches. Approval launch conflicts require another authorization attempt.

Every automation run has a fixed deadline at the triggering reset. The execution monitor checks that deadline independently of the 15-minute trigger interval. At reset, it interrupts active work, including a graceful wrap-up, without starting another model turn. Queued project executions cannot start after reset. This keeps the run from intentionally consuming usage from the new window; provider reporting and interruption are not instantaneous.

Runs work only approved tasks and may finish before exhausting usage. Completion, blocking, provider limits, and existing no-progress protections still apply.

## Approvals and history

The notification opens a review page. Sign in if required, then select **Authorize run** to refresh usage, recheck the condition, and launch. The link itself grants no permission. Pending requests expire at reset. **Dismiss** suppresses that occurrence, and editing, pausing, or deleting a rule cancels its unlaunched occurrences for that window. Changing a rule does not launch a replacement for the same window.

The Automations page shows pending approvals, rules, delivery/admission errors, and recent occurrence history with links to runs. Runs also link back to the occurrence that launched them. Deleting a rule preserves its history and existing runs.
