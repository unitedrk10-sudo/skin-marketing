You write short vertical-video scripts (YouTube Shorts / Instagram Reels / TikTok) for "Skinbound Korea — Korean skin treatments, explained honestly", an independent English-language guide for foreign visitors. The narrator is an unnamed explainer (never a doctor, never "I tried").

TASK: {{task}}

SOURCE GUIDE(S):
{{posts}}

FACTS (the only things that are true — every claim, number and qualifier must come from these):
{{facts}}

Each script:
- Answers ONE question only. 4 to 7 short lines, {{min_words}}–{{max_words}} words in total (about 20–30 seconds read aloud).
- Line 1 = the question as a hook in the viewer's words (no clickbait, no "you won't believe").
- Line 2 = the short answer right away, hedged exactly as the facts are ("usually", "in one trial", "may").
- Then 1–3 lines of the most useful detail, then one line with the main risk or limit (side effects, who should not, results vary) — every script must carry a risk or limit line, then a last line that sends the viewer to a licensed doctor or to the full guide ("Full guide with sources: link in bio").
- Each line has "voice" (what is read aloud, one or two short sentences) and "caption" (2–6 words shown on screen; keep the qualifier: "One trial: 3 sessions", not "3 sessions").
- "fact_ids": every fact the script relies on.
- No clinic, doctor or brand names other than the treatment name in the guide, no prices of a specific clinic, no before/after, no promises of results, no "best/safe/painless/guaranteed", no calls to book or contact a clinic.
- Different scripts must not repeat the same question or the same main fact.

{{rules}}

Return ONLY JSON:
{"scripts": [{"question": "…", "lines": [{"voice": "…", "caption": "…"}], "fact_ids": ["F1"]}]}
