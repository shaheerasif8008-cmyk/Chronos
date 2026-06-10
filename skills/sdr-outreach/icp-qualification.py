"""
ICP Lead Qualification Script
------------------------------
Scores leads 0-100 based on Ideal Customer Profile (ICP) fit.

Expected params (passed as `context` by skill.run_script):
  leads: list of dicts with keys:
    - name:         str  — lead full name
    - company:      str  — company name
    - title:        str  — job title
    - company_size: int  — number of employees (0 if unknown)
    - industry:     str  — industry label

Returns JSON:
  {
    "qualified_leads":    [...],   # score >= 60
    "disqualified_leads": [...],   # score < 60
    "summary": "..."
  }

Each lead dict in the output includes an added "icp_score" (int 0-100)
and "score_breakdown" dict with sub-scores.
"""

import json
import sys

# ---------------------------------------------------------------------------
# ICP configuration — adjust per client before deploying.
# ---------------------------------------------------------------------------

# Titles that strongly indicate a buying decision-maker (+35 pts)
IDEAL_TITLES = {
    "ceo", "cto", "coo", "cpo", "vp", "vice president",
    "head of", "director", "chief", "founder", "co-founder",
    "owner", "president", "general manager",
}

# Titles that indicate a strong influencer but not final decision (+20 pts)
INFLUENCER_TITLES = {
    "manager", "lead", "senior", "principal", "architect", "engineer",
    "specialist", "analyst", "consultant",
}

# Industries that are a strong ICP fit (+30 pts)
IDEAL_INDUSTRIES = {
    "software", "saas", "technology", "tech", "fintech", "healthtech",
    "edtech", "ecommerce", "e-commerce", "media", "marketing", "advertising",
    "financial services", "insurance", "banking", "retail", "logistics",
}

# Industries that are a partial fit (+15 pts)
ADJACENT_INDUSTRIES = {
    "consulting", "professional services", "real estate", "manufacturing",
    "healthcare", "pharmaceuticals", "energy", "telecom", "telecommunications",
}

# Ideal company size range (number of employees)
IDEAL_SIZE_MIN = 50
IDEAL_SIZE_MAX = 5000

QUALIFICATION_THRESHOLD = 60


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_title(title: str) -> tuple[int, str]:
    title_lower = title.lower()
    for keyword in IDEAL_TITLES:
        if keyword in title_lower:
            return 35, f"decision-maker title ({keyword})"
    for keyword in INFLUENCER_TITLES:
        if keyword in title_lower:
            return 20, f"influencer title ({keyword})"
    return 5, "title not matched to ICP"


def _score_company_size(size: int) -> tuple[int, str]:
    if size <= 0:
        return 10, "company size unknown"
    if IDEAL_SIZE_MIN <= size <= IDEAL_SIZE_MAX:
        return 30, f"company size {size} in ideal range ({IDEAL_SIZE_MIN}–{IDEAL_SIZE_MAX})"
    if size < IDEAL_SIZE_MIN:
        return 10, f"company size {size} below ideal range (too small)"
    return 15, f"company size {size} above ideal range (enterprise — longer sales cycle)"


def _score_industry(industry: str) -> tuple[int, str]:
    ind_lower = industry.lower()
    for keyword in IDEAL_INDUSTRIES:
        if keyword in ind_lower:
            return 30, f"ideal industry match ({keyword})"
    for keyword in ADJACENT_INDUSTRIES:
        if keyword in ind_lower:
            return 15, f"adjacent industry match ({keyword})"
    return 5, "industry not matched to ICP"


def _score_lead(lead: dict) -> dict:
    title_score, title_reason = _score_title(lead.get("title") or "")
    size_score, size_reason = _score_company_size(int(lead.get("company_size") or 0))
    industry_score, industry_reason = _score_industry(lead.get("industry") or "")

    total = min(100, title_score + size_score + industry_score)

    return {
        **lead,
        "icp_score": total,
        "score_breakdown": {
            "title": {"score": title_score, "reason": title_reason},
            "company_size": {"score": size_score, "reason": size_reason},
            "industry": {"score": industry_score, "reason": industry_reason},
        },
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(params: dict) -> dict:
    leads = params.get("leads") or []
    if not isinstance(leads, list):
        return {"error": "params.leads must be a list", "qualified_leads": [], "disqualified_leads": [], "summary": ""}

    scored = [_score_lead(lead) for lead in leads]
    qualified = [l for l in scored if l["icp_score"] >= QUALIFICATION_THRESHOLD]
    disqualified = [l for l in scored if l["icp_score"] < QUALIFICATION_THRESHOLD]

    qualified.sort(key=lambda l: l["icp_score"], reverse=True)
    disqualified.sort(key=lambda l: l["icp_score"], reverse=True)

    summary = (
        f"Scored {len(scored)} leads. "
        f"{len(qualified)} qualified (score >= {QUALIFICATION_THRESHOLD}), "
        f"{len(disqualified)} disqualified."
    )
    if qualified:
        top = qualified[0]
        summary += f" Top lead: {top.get('name', 'unknown')} at {top.get('company', 'unknown')} (score {top['icp_score']})."

    return {
        "qualified_leads": qualified,
        "disqualified_leads": disqualified,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Sandbox execution: the data connector passes `context` as JSON on stdin
# or the script can be imported and main() called directly.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    params = json.loads(raw) if raw else {}
    result = main(params)
    print(json.dumps(result))
