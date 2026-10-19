You are planning the visuals for a vertical (9:16) short video that explains a dermatology topic for foreign visitors to Korea. A narrator reads the script below; you write prompts for an AI video generator (Google Veo, 8-second clips) that will show what the narration is about.

Title: {{title}}

SCRIPT (numbered lines, read in order):
{{script}}

Write exactly {{n}} scenes that together cover the script in order. Each scene covers a run of consecutive script lines.

Visual rules (strict — these come from Korean medical advertising law and our brand):
- No human faces, no patients, no before/after looks, no injections, needles, lasers touching skin, or any procedure being performed, no clinic interiors, treatment rooms, doctors or white coats, no logos, brand names, readable text, signs or captions in the image.
- Allowed: hands (no faces), calm still life (skincare bottles without labels, towels, a calendar, a passport, a suitcase), Seoul streets, palaces, hanok alleys, cafes, parks and seasons, sky and light, abstract skin-like textures, water, gentle motion.
- Every scene must clearly relate to its lines (if a line is about sun protection, show sunlight and shade; if about days of recovery, show a calendar or a calm hotel room), not generic beauty footage.
- Content match test (2026-10-20 — viewers scored our first try 1/5 for match): a viewer who sees the clip with the sound off should be able to guess what its lines are about. Pick the most concrete noun or action in the lines and show THAT, literally where allowed, otherwise as a direct visual stand-in for the same idea:
  - skin texture / small bumps → extreme macro of skin-like texture on the back of a hand or forearm with tiny raised dots slowly settling
  - an ingredient (e.g. PN from salmon DNA) → the source itself (salmon in cold clear water, a glowing DNA-like strand made of water droplets)
  - "how it works / repair" → slow macro of something visibly repairing or filling in (cracked dry clay smoothing out, water filling fine cracks)
  - redness fading → a soft pink tint slowly fading out of white fabric or water
  - counts and intervals (3 sessions, two weeks apart) → show the count with objects, e.g. three identical small glass vials placed one by one with pauses; never rely on written numbers
  - rest / downtime → a calm bed or a sofa by a window, a cup being set down
  - "ask a doctor" / consultation → two hands with a notebook and pen across a plain table (no clinic, no white coat)
  - travel / planning → suitcase, passport, a Seoul street or palace path that matches the travel point of the line
- Never write numbers, dates or words that should appear in the image (the generator garbles text — a calendar came out with "36" and "39"). If you show a calendar, it must be blank or out of focus.
- If a scene covers several lines about different things, choose the scene's subject from the line with the most specific content, and make sure no line in the scene contradicts the image.
- One consistent look across all scenes: the style line below will be added to every prompt, so do not describe a different style.
- Simple, physically plausible motion that an 8-second clip can show (slow push-in, light moving across a surface, steam, leaves falling). One subject per scene.

{{rules}}

Return ONLY JSON:
{"scenes": [{"lines": [1, 2], "prompt": "one or two sentences describing subject, setting and motion"}]}
