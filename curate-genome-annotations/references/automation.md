# Recurring automation

## Required schedule inputs

Before creating a recurring job, obtain:

- absolute genome file path;
- number of CDS features per run;
- daily time and IANA timezone;
- selection policy and any exclusions;
- research focus, prompt, language, and maximum results;
- repository directories and MCP endpoints;
- state directory and operational owner;
- notification or review handoff destination.

The recurring job must create ChangeSets only. Human review remains a separate activity.

## Scheduling model

Use the agent product's native recurring automation mechanism when available. Otherwise use a supervised scheduler such as launchd, systemd timer, or cron. Keep secrets in the scheduler's protected environment, not in the command line.

The scheduled command should be equivalent to:

```bash
python3 /absolute/path/curate-genome-annotations/scripts/run_annotation_workflow.py \
  --genome /absolute/path/genome.gbk \
  --daily-count 10 \
  --user-prompt "Refine CDS annotations using organism-specific evidence and precise citations" \
  --state-dir /durable/private/path/genome-annotation-state \
  --output /durable/private/path/latest-run.json
```

Run `start_services.py --check-only` first in a wrapper or service health check. Prefer process supervision for CodeXomics and DGR rather than launching duplicate copies from every scheduled job.

## Overlap and retries

- The runner acquires a non-blocking, per-genome lock. A second overlapping job exits without submitting duplicate research.
- State is written atomically after every target transition.
- Keep a stable state directory across runs.
- Retry transient endpoint failures with scheduler-level backoff.
- Do not automatically retry scientific validation failures indefinitely; surface them for human inspection.
- Do not raise concurrency until DGR task storage, provider quotas, and CodeXomics window routing have been load-tested.

## Daily reporting

Capture the runner's JSON output and report:

- selected/submitted/completed/failed/skipped counts;
- target, DGR task ID, report attachment, and ChangeSet ID per gene;
- remaining pending tasks;
- endpoint or provider failures;
- confirmation that no automatic approval/application occurred.

Alert when the service is unavailable, a run produces no ChangeSet for multiple consecutive targets, DGR tasks complete implausibly without evidence, the task ledger is locked/corrupt, or the same CDS remains selected repeatedly.

## Changing policy

Changing the count, prompt, result limit, selection filter, or genome changes the scientific run policy. Record the change and dry-run the next selection. Do not reset coverage state merely to make the new policy start at the first coordinate; use a new state directory or an explicit reset procedure approved by the operator.
