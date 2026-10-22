You are designing an animated explainer (vertical 9:16 short) for an English-language guide to Korean skin treatments for travellers ("Korean skin treatments, explained honestly"). A narrator reads the script below; the line's caption is already shown at the top and the spoken words at the bottom. You design ONLY the animated diagram in the middle of the screen for each line — like the mechanism animations in good science shorts.

Title: {{title}}

SCRIPT (numbered lines):
{{script}}

FACTS (the only things that are true for this video — every word and number you put on screen must come from these or from the line itself):
{{facts}}

Choose ONE diagram per script line from these kinds and fill its fields:
- "skin_layers": skin cross-section (epidermis / dermis / fat layer). Fields: highlight ("epidermis"|"dermis"|"subcutis"), particles ("diffuse" = small particles sink into the highlighted layer, "settle" = small bumps on the surface settle down, or "" for none), label, note.
- "molecule": a chain of fragments assembling (an ingredient such as PN / DNA fragments, hyaluronic acid). Fields: label, note.
- "timeline": numbered sessions with the interval between them. Fields: count (integer), interval (e.g. "2 weeks apart"), label, note.
- "big_number": one key number. Fields: value, unit, label, note.
- "fade_patch": a skin patch whose redness slowly fades (redness/swelling that settles). Fields: from_label, to_label, label, note.
- "myth_fact": two cards. Fields: belief (a general belief, never attributed to a person or clinic), fact.
- "checklist": up to 3 short items. Fields: label, items.
- "icon_scene": one icon. Fields: icon ("suitcase"|"calendar"|"sun"|"bed"|"doctor_note"|"drop"|"logo"), label, note.

Rules:
- The label of each diagram is the on-screen headline for that line (there is no other title on screen), so it must say the point of the line in plain words.
- Line 1 (the hook): if it asks a question, answer it right away — label = the question in a few words, note = a short preview of the answer taken from the facts, hedged as the facts are (e.g. "Usually settles soon — varies by person"). Viewers must know in the first 3 seconds what they will learn.
- Keep every qualifier the fact has ("in one trial", "usually", "may", "for most people") in the label or note — never turn a single study or general guidance into a universal fact (write "One trial: 3 sessions, 2 weeks apart", not "3 sessions, 2 weeks apart").
- The last line (follow / more guides) uses icon_scene with icon "logo".
- Show the mechanism or the practical point of the line, as a schematic diagram. Prefer skin_layers / molecule / timeline / fade_patch when the line is about how something works, how long, or how often.
- On-screen text must be no stronger than the facts: keep hedges ("may", "usually", "in one trial"), never promise results, never show before/after, never say safe, painless, best, guaranteed. If a fact comes from one study, say so ("one trial").
- Every number on screen must appear in the facts exactly. Do not invent numbers, percentages or durations.
- Short text: label at most 32 characters, note at most 48, each card or item at most 60.
- No clinic, doctor or brand names other than the treatment name used in the script. No prices.
- Vary the kinds; do not use the same kind more than three times in a row.
- "fact_ids": the facts each diagram relies on (empty only for icon_scene on travel or "ask your doctor" lines).

{{rules}}

Return ONLY JSON:
{"beats": [{"line": 1, "kind": "skin_layers", "highlight": "dermis", "particles": "diffuse", "label": "…", "note": "…", "fact_ids": ["F3"]}]}
