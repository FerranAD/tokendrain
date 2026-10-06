---
hide:
  - toc
---

# Use the tokens you're paying for.

You're already paying for a Codex subscription. The weekly usage limit resets whether you use it or not. Tokendrain runs Codex on your projects to consume the usage you'd otherwise leave behind.

Give it approved tasks, choose how far to drain your usage limits, and let it run. Work stops when the tasks finish, the agent gets blocked, or your limits are reached.

[Install on NixOS](getting-started.md){ .md-button .md-button--primary }
[NixOS options](nixos-options.md){ .md-button }

![The tokendrain dashboard with usage, Runs, projects, and schedules](assets/dashboard.png)

## How it works

- Create projects and approve tasks in the Kanban board. Codex resumes In progress work, then takes Todo tasks. Its new ideas stay in Backlog until you approve them.
- Each project runs in an isolated Firecracker VM. Source files, installed tools, caches, and progress persist between runs.
- Choose usage thresholds or run until the provider's limit is exhausted. Start now, save a cron schedule, or [launch automatically before a reset](automations.md).
- Connect GitHub if you want changes published. You can also browse or download the workspace directly.

## Documentation

| What you need | Read |
| --- | --- |
| Install and start a run | [Installation and first Run](getting-started.md) |
| Configure the NixOS service | [All module options](nixos-options.md) |
| Connect Codex | [Account setup](openai-auth.md) |
| Choose how much usage to consume | [Usage limits and stopping](usage-limits.md) |
| Run before the weekly reset | [Automations](automations.md) · [ntfy notifications](notifications.md) |
| Manage projects and approved work | [Projects and tasks](workflow.md) |
| Give a project GitHub access | [GitHub App setup](github-app.md) |
| Manage VM resources and storage | [MicroVM operations](microvms.md) |
| Back up or restore | [Security and backups](security.md) |
| Work on Tokendrain | [Development guide](development.md) · [Architecture](architecture.md) |
