You add 1 or 2 explanatory diagrams to a blog guide for "Skinbound Korea — Korean skin treatments, explained honestly". Each diagram is a clean medical-illustration style figure placed right after one section heading of the guide, like the mechanism figures in a good science article.

GUIDE (already fact-checked and approved by a human editor):
{{guide}}

SECTION HEADINGS (use one of these exactly as "after_heading"):
{{headings}}

FACTS (with sources):
{{facts}}

Pick the section(s) where a diagram helps most — first choice: how the treatment works (mechanism); second: what results studies report, how long effects or side effects last, or how sessions are spaced. Skip travel-only sections. Return no figure if nothing fits.

For each figure choose ONE kind and fill its fields:
- "skin_layers": skin cross-section. highlight ("epidermis"|"dermis"|"subcutis"), particles ("diffuse" = particles sink into the highlighted layer, "settle" = small surface bumps settle down, "heat" = the layer warms and existing collagen tightens, then new fibres form — for energy devices (radiofrequency, laser, ultrasound); never draw particles for them, "collagen" = particles in the dermis with new collagen fibres forming around them — only when the guide says the treatment stimulates collagen, "" = none), label, note.
  When particles is not "", also give "stages": exactly 3 short step names (at most 28 characters each) that say what changes at each step, taken from the approved text — e.g. heat: ["Heat reaches the dermis", "Collagen contracts", "New collagen forms over months"]; collagen: ["Particles placed in dermis", "Collagen starts forming", "Collagen builds gradually"]; settle: ["Small bumps right after", "Bumps begin to settle", "Usually settled"]. Keep the guide's qualifiers if a step is uncertain ("May…").
- "molecule": a rotating double-helix / nucleotide chain (PN, PDRN, DNA-derived ingredients). label, note.
- "timeline": numbered sessions with the interval. count, interval, label, note.
- "fade_patch": redness or swelling that settles over time. from_label, to_label, label, note.

Rules:
- Draw and write only what the guide says, no stronger; keep its qualifiers ("may", "one study", "usually", "thought to").
- label at most 32 characters, note at most 48. Every number must appear in the facts.
- "alt": one plain sentence describing the figure for screen readers (no claims beyond the guide).
- "fact_ids": the facts the figure relies on.

{{rules}}

Return ONLY JSON:
{"figures": [{"after_heading": "…", "kind": "skin_layers", "highlight": "dermis", "particles": "collagen", "label": "…", "note": "…", "alt": "…", "fact_ids": ["F1"]}]}
