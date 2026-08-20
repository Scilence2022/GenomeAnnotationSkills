# Recurring automation

## Required schedule inputs

Before creating a recurring job, obtain:

- absolute genome file path;
- number of gene annotation features per run;
- daily time and IANA timezone;
- selection policy and any exclusions;
- research focus, prompt, language, and maximum results;
- repository directories and MCP endpoints;
- state directory and operational owner;
- notification or review handoff destination.

Also obtain the reporting inputs, because a daily batch is expected to account for what it consumed:

- price list path for the research and agent models;
- agent usage sidecar path;
- report output directory and retention policy.

The recurring job must create ChangeSets only. Human review remains a separate activity.

## Scheduling model

Use the agent product's native recurring automation mechanism when available. Otherwise use a supervised scheduler such as launchd, systemd timer, or cron. Keep secrets in the scheduler's protected environment, not in the command line.

`scripts/install_schedule.py` generates the wrapper and the scheduler unit. It prints them and writes nothing unless `--install` is passed:

```bash
python3 /absolute/path/curate-genome-annotations/scripts/install_schedule.py \
  --genome /absolute/path/genome.gbk \
  --daily-count 10 \
  --at 01:00 \
  --state-dir /durable/private/path/genome-annotation-state \
  --report-dir /durable/private/path/reports \
  --env-file /protected/path/genome-annotation.env \
  --pricing-file /protected/path/pricing.json \
  --agent-usage-file /durable/private/path/agent-usage.jsonl
```

launchd fires at the machine's local time and ignores a requested timezone; use `--scheduler cron` (which emits `CRON_TZ`) or `--scheduler systemd` (which emits `Timezone=`) when the daily time must follow a specific zone.

The generated wrapper is equivalent to:

```bash
python3 .../start_services.py --check-only

python3 .../run_annotation_workflow.py \
  --genome /absolute/path/genome.gbk \
  --daily-count 10 \
  --selection-policy low-quality \
  --maximum-quality-score 70 \
  --research-refresh-days 365 \
  --user-prompt "Refine gene annotations using organism-specific evidence and precise citations" \
  --state-dir /durable/private/path/genome-annotation-state \
  --run-id "$RUN_ID" \
  --pricing-file /protected/path/pricing.json \
  --agent-usage-file /durable/private/path/agent-usage.jsonl \
  --output "$SUMMARY" --metrics-output "$METRICS"

python3 .../generate_run_report.py "$SUMMARY" \
  --markdown-output "$REPORT_MD" --json-output "$REPORT_JSON"
```

The report step runs even when the batch exits non-zero, so a partial day is still accounted for; the wrapper then exits with the batch's status. Prefer process supervision for CodeXomics and DGR rather than launching duplicate copies from every scheduled job.

## Overlap and retries

- The runner acquires a non-blocking, per-genome lock. A second overlapping job exits without submitting duplicate research.
- CodeXomics keeps the authoritative research coverage in the genome sidecar. Candidate filtering excludes active and durably completed targets even when another agent or state directory performed the work.
- `start_annotation_research` applies a second `skip-covered` guard so concurrent candidate lists cannot create duplicate work.
- State is written atomically after every target transition.
- Keep a stable state directory across runs.
- Retry transient endpoint failures with scheduler-level backoff.
- Failed and completed-but-unarchived workflows remain retryable, but do not retry scientific validation failures indefinitely; surface repeated failures for human inspection.
- Do not raise concurrency until DGR task storage, provider quotas, and CodeXomics window routing have been load-tested.

## Daily reporting

`generate_run_report.py` turns the run summary into the daily report. Read [reporting.md](reporting.md) for where each statistic comes from. The report covers:

- selected/submitted/completed/failed/skipped counts;
- target, DGR task ID, report attachment, and ChangeSet ID per gene;
- total tokens split by role (research model versus agent model) and by model id, plus cached and cache-replayed tokens;
- actual cost per model from the operator's price list, or an explicit unavailable reason;
- runtime per gene and for the batch, with DGR research time separated from wall clock;
- references surveyed, retained, and newly added; full texts adopted;
- newly incorporated information: qualifier fields updated and citation-bound facts included in Notes;
- remaining pending tasks and endpoint or provider failures;
- confirmation that no automatic approval/application occurred.

Add `--fail-on-gaps` when the monitored job should alert on missing statistics rather than publishing a report with unavailable cells.

Alert when the service is unavailable, a run produces no ChangeSet for multiple consecutive targets, DGR tasks complete implausibly without evidence, the task ledger is locked/corrupt, the same feature remains selected repeatedly, or the replayed-token share rises — the last means the batch is re-covering already-researched targets.

## Changing policy

Changing the count, prompt, result limit, selection filter, random seed, or genome changes the scientific run policy. Record the change and dry-run the next selection. Do not reset coverage state merely to make the new policy start at the first coordinate; use a new state directory or an explicit reset procedure approved by the operator.
