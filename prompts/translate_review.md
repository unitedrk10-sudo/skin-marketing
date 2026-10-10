Translate the English texts in the JSON below into natural Korean for a Korean reviewer who checks a draft before it is published. This translation is ONLY for the reviewer — it is never published.

Rules:
- Translate faithfully, sentence by sentence. Do not summarize, shorten, soften, strengthen or add anything — the reviewer must see exactly what the English says, including hedges ("commonly", "may", "results vary") and numbers.
- Keep every marker like [F3] or [F3][F7] exactly where it is.
- Keep Markdown structure (headings "##", lists, bold, links) as is. Keep URLs unchanged.
- Product, procedure and place names: write the common Korean name and keep the English in parentheses the first time (e.g. "리쥬란(Rejuran)", "경복궁(Gyeongbokgung)").
- Return the same keys, and lists with exactly the same number of items in the same order.

INPUT:
{{payload}}

Return ONLY JSON with the same shape:
{"findings": ["…"], "medical": ["…"], "practice": ["…"], "script": ["…"], "blog": "…"}
