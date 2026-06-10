# SDR Outreach

Research target accounts, qualify ICP fit, and draft personalized outreach. Outbound email must be drafted first and approved before sending.

## ICP Lead Qualification

Use `icp-qualification.py` to score a list of leads against the Ideal Customer Profile before drafting outreach. This avoids wasting effort on poor-fit accounts.

Invoke via `skill.run_script`:

```json
{
  "tool": "skill.run_script",
  "args": {
    "skill_id": "sdr-outreach",
    "script_name": "icp-qualification.py",
    "params": {
      "leads": [
        {
          "name": "Jane Smith",
          "company": "Acme SaaS",
          "title": "VP of Engineering",
          "company_size": 200,
          "industry": "Software"
        }
      ]
    }
  }
}
```

The script returns:

```json
{
  "qualified_leads": [...],
  "disqualified_leads": [...],
  "summary": "Scored 1 lead. 1 qualified (score >= 60), 0 disqualified. Top lead: Jane Smith at Acme SaaS (score 95)."
}
```

Each lead in the output includes `icp_score` (0–100) and a `score_breakdown` dict explaining title, company size, and industry sub-scores. Only draft outreach for leads in `qualified_leads`.

## Outreach Rules

- Always qualify leads with `icp-qualification.py` before drafting email.
- Draft emails via `gmail.draft` — never call `gmail.send` directly.
- Sending requires an approval record; the agent must wait for human approval.
