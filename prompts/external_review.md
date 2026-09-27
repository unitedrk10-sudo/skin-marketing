# Claude Code 검수 요청 (packet {{packet_id}})

You are the independent reviewer (a different vendor from the Gemini model that wrote these drafts). The operator is legally responsible under Korean law, so be precise and conservative. Do not rewrite drafts; report findings.

For EACH draft in `drafts`:

## A. Source check (`sources`)
For every entry in the draft's `sources`, read `page_excerpt` (text Hermes fetched from that URL) and judge each fact listed in `fact_ids` (fact text is in `facts`):
- "supported": the page clearly states it (paraphrase OK, numbers must match)
- "weak": related but only partly supports it, or numbers differ slightly
- "unsupported": the page does not say it, or says something different
Judge only from the page text, never from outside knowledge. Quote the supporting sentence in `evidence`.

## B. Cross review (`findings`)
Check the script and blog against the rules below and this checklist:
- Exaggerated or definitive claims about effects
- Factual errors or claims that look implausible for the cited fact
- Anything that reads as a patient testimonial or first-person experience
- Comparative / superlative wording, including implied comparisons between countries' clinics
- Missing mention of side effects, individual variation, or consulting a doctor
- Anything that identifies or promotes a specific clinic or doctor
- Missing AI disclosure
Severity: "minor" = wording to soften or clarify; "major" = factual error, legal-risk phrase, or missing mandatory element.
`rule_findings` lists what the code-based check already caught; do not repeat those.

{{rules}}

## Output
Write ONE JSON file to `review-queue/done/{{packet_id}}.result.json`, copying `draft_id` and `draft_hash` exactly:
```json
{
  "packet_id": "{{packet_id}}",
  "reviewer": "claude-code",
  "drafts": [
    {
      "draft_id": "...",
      "draft_hash": "...",
      "sources": [{"id": "F1", "verdict": "supported|weak|unsupported", "evidence": "quote from page"}],
      "findings": [{"severity": "minor|major", "where": "script|blog", "quote": "exact text", "issue": "what is wrong", "suggestion": "rewrite"}],
      "overall": "one sentence"
    }
  ]
}
```
Then delete `review-queue/pending/{{packet_id}}.json` in the same commit.
