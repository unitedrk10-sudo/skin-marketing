Write three short social posts about our blog guide, each in an X version and a Threads version. They go out on different days, so each must stand on its own.

Guide title: {{title}}
Hook used in the video: {{hook}}
Travel guide (treatment × trip planning): {{travel}}

You may ONLY use the facts below. Every number (price, days, sessions, percentages) must come from these facts exactly. If a fact is not here, leave it out.

FACTS:
{{facts}}

The three posts:
1. "link" — introduces the guide (the link is added automatically after your text).
   - Line 1: the reader's situation or question (trip context if it is a travel guide). Never "New post!" or "Check out".
   - Then ONE useful takeaway, worded as close to the fact as possible.
   - Threads: then 2-3 short lines starting with "→" saying what the guide covers (what the reader will learn).
   - X: then 2-3 short lines starting with "·" saying what the guide covers.
   - X: at most {{link_x}} characters. Threads: at most {{link_threads}} characters.
2. "fact" — ONE fact from the list, no link. Plain, specific, useful on its own (e.g. "Laser + sightseeing tip: …"). End with what to confirm with a doctor if it is about a procedure.
   - X: at most {{fact_x}} characters. Threads: at most {{fact_threads}} characters.
3. "angle" — if this is a travel guide: a practical planning tip (timing, sun, walking, recovery days around the trip). Otherwise: a common misconception vs. what the sources say ("A common belief is … What sources say: …") — the "belief" must be a general one, never attributed to a person, clinic or brand.
   - X: at most {{angle_x}} characters. Threads: at most {{angle_threads}} characters.

For every post:
- An explainer's voice (never "I tried"), plain English, hedged wording ("commonly", "results vary"), no clinic or doctor names, no prices from a specific clinic, no "best/#1/guaranteed/safe/painless".
- Never ask readers for their clinic experiences, reviews or before/after photos. A closing question is fine only about planning ("What's harder to plan — …?").
- No links and no hashtags (the source name / link line is added automatically).
- "fact_ids": the facts the post relies on (fact and angle must list at least one).

{{rules}}

Return ONLY JSON:
{"link": {"x": "…", "threads": "…", "fact_ids": ["F1"]},
 "fact": {"x": "…", "threads": "…", "fact_ids": ["F3"]},
 "angle": {"x": "…", "threads": "…", "fact_ids": ["F2"]}}
