# Projects, tasks & Runs

A project is the durable unit of work: its goal, Kanban board, workspace, development environment, and feedback stay available between executions. A Run selects one or more projects and sets their models, reasoning effort, and stopping conditions.

## Approve the work

![Kanban with approved tasks and agent discoveries](assets/kanban.png)

| Column | Meaning |
| --- | --- |
| Backlog | Ideas and agent discoveries waiting for your review. Only you can approve them. |
| Todo | Work you have approved for the agent to do. |
| In progress | Approved work to resume before starting Todo tasks. |
| Done | Completed tasks. Review the report and any repository contributions. |

The host rejects attempts by agents to move Backlog into approved work. If there are no In progress or Todo tasks left, the agent finishes. Add clear task descriptions and give repository and secret permissions only as needed for the work.

## Prepare a Run

1. Create a project with a description and Todo tasks.
2. Optionally connect a [GitHub repository](github-app.md) and add described secrets.
3. Select **Prepare Run**, choose projects, and set models and reasoning effort.
4. Choose [usage or runtime stopping conditions](usage-limits.md) and Graceful or Hard behavior.
5. Start immediately, or save a timezone-aware cron schedule.

The dashboard shows observed account usage, active Runs, projects, and schedules. During a Run, follow activity and checkpoints. Feedback and task changes guide subsequent work; next-run feedback is consumed only when an execution returns a turn result.

## Inspect and continue

When a project is idle, **Workspace** lets you browse files, preview source, sanitized Markdown and images, and download files or ZIP archives. **VM storage** shows total capacity and supports storage growth while the project is idle.

The project VM is persistent. Workspace files, installed tools, OS configuration, homes, and caches survive between Runs. Tokendrain attaches its current control software each time. Cancelling or reaching a budget stops execution; it does not turn an incomplete project into a completed one. Execution outcomes and the last valid checkpoint are recorded separately.

Use feedback to change direction before the next Run. For host resource limits and recovery procedures, see [MicroVM operations](microvms.md).
