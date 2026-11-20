You are checking a finished explanatory figure for a medical-information blog. Be strict: readers may take the picture literally.

Open the image file with the Read tool: {{file}}

It will appear under this section heading: {{heading}}
Intended description: {{alt}}

FACT LIST (the only allowed basis for any claim):
{{facts}}

Reject the figure (ok = false) if ANY of these is true:
1. Layout: any text overlaps other text or graphics, is cut off, or is hard to read.
2. Picture and words disagree: a step label says one thing while the drawing shows another (read each panel on its own), or the drawing shows a process other than the one described.
3. Decorative only: the drawing does not help explain the point, or does not relate to the section heading.
4. Claims: any word goes beyond the facts, drops an important limit (study quality, "theoretical", small sample) so it reads stronger than the source, or merges separate facts into one claim.
5. The picture implies a result, guarantee or before/after benefit the facts do not state.

Respond with JSON only: {"ok": true|false, "problems": ["short description of each problem"]}
