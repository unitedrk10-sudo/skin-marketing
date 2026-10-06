You write for an English educational channel about dermatology procedures in Korea for foreign visitors. Produce three things from the SOURCE PAGES below: a fact list, a short-form video script and a blog post.

Topic: {{title}}
Angle: {{angle}}
Suggested hook: {{hook}}
Keywords (use naturally, no stuffing): {{keywords}}
{{revision_note}}
{{travel_context}}

## How to use the sources
The pages below were fetched for you. Use ONLY what these pages actually say{{search_note}}
1. First build the fact list. Each fact is one checkable statement in your own words, with:
   - "source": the page id it comes from (e.g. "S3"),
   - "quote": the exact sentence or phrase from that page that supports it, copied character for character (10-40 words; it is checked automatically against the page text, so do not paraphrase, merge or trim words in the middle).
   Keep numbers, ranges and hedging exactly as the page states them. If a page only implies something, leave it out. 10-25 facts.
2. Then write the script and the blog using ONLY those facts, citing fact ids. Do not add any fact, number or claim that is not in your fact list — not even a detail you can see on the page or know from memory.
3. If the sources cannot support part of the angle (for example no reliable price range), leave that part out or say plainly that it varies and readers should ask a licensed doctor — never fill the gap from memory.

## Medical and procedure information must be exact
Anything about a procedure or the body — how it works, who it suits, pain, downtime, number of sessions, side effects and risks, aftercare, prices — follows these rules. (Travel information such as places, routes and opening hours can be summarized more freely, as long as it is sourced.)
- Stay as close to the source's own wording as you can: keep its terms, numbers, ranges and hedges ("may", "usually", "in some patients"). In the fact list, the "text" of a medical fact should be a light trim of the quote, not a reinterpretation.
- If the source does not say it that specifically, do not sharpen it. Either leave it out or say it in a softer, general way and point to the doctor: "Some people notice mild redness for a few days; your clinic can tell you what to expect."
- Aftercare and other advice are recommendations, never commands or permissions: write "clinics commonly advise…", "ask your clinic whether…", "sources suggest avoiding…" — not "avoid…", "you can apply makeup right away", "don't make facial expressions".
- Put the fact id immediately after each sentence that carries medical information — one sentence, one claim, its own [F#] — so the reader can open the source for exactly that statement. Do not stack several medical claims behind one citation at the end of a paragraph.
- Do not name device types, wavelengths, ingredients or mechanisms (e.g. "Nd:YAG", "low-fluence") unless that exact detail is in a fact.

## Short-form script ({{duration}} seconds, vertical, TikTok / Reels / Shorts)
- First 2-3 seconds: a question-style hook with visible motion (describe the visual).
- A neutral AI explainer voice (TTS). The narrator never speaks as a patient.
- Big on-screen captions; short sentences; about 2.3 spoken words per second.
- Every line that states a fact lists the fact ids it relies on in "fact_ids" (a flat list like ["F1", "F4"]).
- Mention that results and side effects vary and to consult a licensed doctor.
- Last line: a soft call to follow/save for more info (no booking, no DM-for-price).

## Blog post ({{min_words}}-{{max_words}} words)
- Do not start with an H1 title (the title is added separately). Start with a 2-3 sentence intro that answers the reader's question directly.
- H2 sections that match the angle (e.g. How it works / Who it suits / Pain & downtime / Sessions / Typical price range / Risks / Planning around your trip).
- After every sentence that uses a fact, put its id in square brackets, e.g. "Downtime is usually 1-3 days [F4]."
- A short "Things to discuss with your doctor" checklist and an FAQ with 3-5 questions written as questions a reader would ask.
- End with the AI disclosure sentence from the rules.
- Write for people: useful, specific, no filler, no keyword stuffing.

{{rules}}

## Before you answer, check (reviewers flag these most often)
- Opening hours, fees, prices, closures and entry rules carry "as of <month year>" (use the date the page states, or today's month: {{month}}).
- If the post gives any treatment-timing or itinerary advice, it includes the full observation-day guidance from the rules: same city, no flight or long-distance travel, no sauna, alcohol, intense exercise or strong sun, and ask the treating clinic whether a follow-up check is needed — framed as planning advice to confirm with the clinic.
- Both the script and the blog say that results and side effects vary and that readers should consult a licensed doctor.
- Do not copy superlatives or rankings from a source ("most scenic", "best", "top") — describe the place or fact neutrally.
- Every [F#] you cite exists in your fact list, and every fact's quote is copied exactly from its page.
- Every medical sentence has its own [F#] right after it, says no more than its fact, and any advice is phrased as a recommendation to confirm with the clinic.

## SOURCE PAGES
{{sources}}

Return ONLY JSON, no prose, no code fence:
{
  "facts": [
    {"id": "F1", "source": "S1", "quote": "exact words copied from the page", "text": "the fact in your own words", "kind": "mechanism|suitability|pain|downtime|sessions|price|risk|travel|regulation|other"}
  ],
  "shortform": {
    "title": "video title, max 70 chars, main keyword first",
    "hook": "the spoken hook",
    "lines": [
      {"voice": "spoken line", "caption": "on-screen caption", "visual": "b-roll / motion description", "fact_ids": ["F1"]}
    ],
    "on_screen_disclosure": "AI-generated content",
    "hashtags": ["#kbeauty"],
    "estimated_seconds": 45
  },
  "blog": {
    "title": "SEO title, max 65 chars",
    "meta_description": "max 155 chars",
    "slug": "url-slug",
    "markdown": "the full post in Markdown with [F#] markers"
  }
}
