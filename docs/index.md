---
hide:
  - navigation
  - toc
---

<div class="td-hero" markdown>

<p class="td-eyebrow">SELF-HOSTED · NIXOS · FIRECRACKER</p>

# Give your leftover allowance a job.

Autonomous Codex work, on your terms. Approve the tasks, choose the limits, and keep a workspace that is ready for the next Run.

[Get started](getting-started.md){ .md-button .md-button--primary }
[How it works](workflow.md){ .md-button }

<div class="td-wordmark">
  <img class="td-logo-light" src="assets/tokendrain-logo-horizontal.png" alt="tokendrain" width="440">
  <img class="td-logo-dark" src="assets/tokendrain-logo-horizontal-dark.png" alt="tokendrain" width="440">
</div>

</div>

<div class="td-screenshot" markdown>

![The tokendrain dashboard with usage, Runs, projects, and schedules](assets/dashboard.png)

</div>

<div class="td-grid" markdown>

<div class="td-card" markdown>

### Your tasks. Your call.

Agents work In progress and Todo. Discoveries stay in Backlog until you approve them.

[Plan the work →](workflow.md)

</div>

<div class="td-card" markdown>

### A persistent development machine.

Each execution boots a Firecracker VM. The entire project machine persists, while Tokendrain control software is current at every Run and managed credentials are temporary.

[Understand persistence →](microvms.md)

</div>

<div class="td-card" markdown>

### Know when to stop.

Set usage thresholds and runtime limits. Choose a bounded wrap-up or hard interruption when a threshold is observed.

[Choose your limits →](usage-limits.md)

</div>

</div>

## Find your next step

| You want to… | Start here |
| --- | --- |
| Install tokendrain on a NixOS host | [Installation and first Run](getting-started.md) |
| Connect your Codex account | [Authentication and usage](openai-auth.md) |
| Give a project GitHub access | [GitHub App setup](github-app.md) |
| Back up or restore project state | [Security and backups](security.md) · [MicroVM operations](microvms.md) |
| Work on tokendrain itself | [Development guide](development.md) · [Architecture](architecture.md) |

!!! note "Before the first Run"
    The supported host is **NixOS with KVM**. Agents have root inside their VMs and run commands without approval prompts. Read the [security model](security.md) before supplying credentials.
