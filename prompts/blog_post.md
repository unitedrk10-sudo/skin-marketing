Write an English blog post ({{min_words}}-{{max_words}} words) for foreign readers considering a dermatology procedure in Korea.

Topic: {{title}}
Angle: {{angle}}
Keywords (use naturally, no stuffing): {{keywords}}
{{revision_note}}

You may ONLY use the facts below. Do not add any fact, number or claim that is not in this list. After every sentence that uses a fact, put its id in square brackets, e.g. "Downtime is usually 1-3 days [F4]."

FACTS:
{{facts}}

Structure:
- H1 title, then a 2-3 sentence intro that answers the reader's question directly.
- H2 sections that match the angle (e.g. How it works / Who it suits / Pain & downtime / Sessions / Typical price range / Risks / Planning around your trip).
- A short "Things to discuss with your doctor" checklist.
- An FAQ section with 3-5 questions.
- End with the AI disclosure sentence from the rules.
- Write for people: useful, specific, no filler, no keyword stuffing.

{{rules}}

Return ONLY JSON, no prose:
{
  "title": "SEO title, max 65 chars",
  "meta_description": "max 155 chars",
  "slug": "url-slug",
  "markdown": "the full post in Markdown with [F#] markers"
}
