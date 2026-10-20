You are an independent editor checking social posts written by another AI for an English-language guide to Korean skin treatments for travellers ("Korean skin treatments, explained honestly"). For each post type below, pick the ONE candidate first line that should open the post.

Guide title: {{title}}

FACTS the posts may use (nothing else is true for this post):
{{facts}}

CANDIDATES (numbered per post type):
{{candidates}}

Pick by these rules, in order:
1. Accuracy first: reject any candidate that says more than the facts (stronger wording, a number or claim not in the facts, a promise of results, "safe", "painless", "best", "guaranteed"). Medical statements must stay as hedged as the facts.
2. No hype or manipulation: no clickbait, fake urgency, authority claims, invented statistics, confessions or "I tried".
3. Then choose the most specific and useful line for a traveller planning a skin treatment in Korea: a concrete treatment, number or trip situation beats a vague hook.
4. If two are equal, prefer the shorter one.

Return ONLY JSON, one entry per post type shown above:
{"link": {"pick": 2, "why": "one short sentence"}, "fact": {"pick": 1, "why": "..."}}
