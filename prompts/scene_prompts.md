You are planning the visuals for a vertical (9:16) short video that explains a dermatology topic for foreign visitors to Korea. A narrator reads the script below; you write prompts for an AI video generator (Google Veo, 8-second clips) that will show what the narration is about.

Title: {{title}}

SCRIPT (numbered lines, read in order):
{{script}}

Write exactly {{n}} scenes that together cover the script in order. Each scene covers a run of consecutive script lines.

Visual rules (strict — these come from Korean medical advertising law and our brand):
- No human faces, no patients, no before/after looks, no injections, needles, lasers touching skin, or any procedure being performed, no clinic interiors, treatment rooms, doctors or white coats, no logos, brand names, readable text, signs or captions in the image.
- Allowed: hands (no faces), calm still life (skincare bottles without labels, towels, a calendar, a passport, a suitcase), Seoul streets, palaces, hanok alleys, cafes, parks and seasons, sky and light, abstract skin-like textures, water, gentle motion.
- Every scene must clearly relate to its lines (if a line is about sun protection, show sunlight and shade; if about days of recovery, show a calendar or a calm hotel room), not generic beauty footage.
- One consistent look across all scenes: the style line below will be added to every prompt, so do not describe a different style.
- Simple, physically plausible motion that an 8-second clip can show (slow push-in, light moving across a surface, steam, leaves falling). One subject per scene.

{{rules}}

Return ONLY JSON:
{"scenes": [{"lines": [1, 2], "prompt": "one or two sentences describing subject, setting and motion"}]}
