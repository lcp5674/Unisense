---
title: Metric Compliance Review Workflow

summary: |
  Automated compliance review process for newly created metrics, with human-in-the-loop checkpoint for final approval.

kind: workflow

scope: compliance

# Loop specification

## Loop pattern
Recurring compliance review of metrics as they enter production.

**Why this loop exists:**
- Metrics must pass compliance checks before being usable
- Different compliance requirements based on metric sensitivity and usage
- Regulatory requirements need documented audit trails

**Where this loop appears:**
- Metric creation → submission for compliance review
- Periodic re-auditing of existing metrics
- Incident-triggered compliance reassessments

## Workflow steps (triggered per metric)

### Step 1: Metric Submission (automated)
When a metric is created or updated:
- Validate metadata completeness
- Assign to appropriate compliance reviewer based on metric category
- Queue for automated compliance checking

### Step 2: Automated Compliance Check (automated)
Run initial compliance validation:
- PII detection and classification
- Schema compatibility check
- Lineage completeness verification
- Documentation completeness audit

### Step 3: Human Review Checkpoint (manual)

**Trigger:** When automated checks complete

**What human reviewer sees:**
- Automated check results summary
- Metric metadata and version history
- Any compliance issues detected
- Links to audit evidence (PII scans, lineage graphs, etc.)

**Decision options:**
- Approve for production use
- Request modifications
- Escalate to senior compliance reviewer
- Archive/reject

**Brief format:**
"Metric '{metric_name}' v{version} has {passed_checks} passed/{failed_checks} failed automated checks. Key issues: {issue_summary}. [View full audit report](link). Decision required by {deadline}."

### Step 4: Final Actions (automated)
Based on human decision:
- If approved: publish metric and create compliance certificate
- If modifications requested: send revision package to metric owner
- If escalated: create senior review ticket
- If archived: update metric status and notify stakeholders

## Details

### Trigger specification
**Event-triggered:** New metric creation, metric status change to 'draft', or compliance re-assessment trigger

### No schedule trigger needed
This workflow runs on-demand when compliance actions are required, not on a fixed schedule.

### No AI mandate
Human reviewers make the final decisions. The automation handles routine checks and routing.

### Push right principle
- Automated validation runs all checks before human review
- Reviewer gets consolidated brief with all evidence links
- Reviewer doesn't need to run separate validation commands

### Dependencies
Requires integration with:
- Metric creation system
- PII detection service
- Lineage tracking system
- Documentation repository
- Compliance reporting system

---