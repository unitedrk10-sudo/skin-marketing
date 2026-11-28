You are cross-checking blog topic ideas that another AI (Gemini) proposed for an English-language guide to dermatology procedures in Korea for foreign visitors. You did not write them. Today is {{today}}. Use web search to verify; do not rely on memory for dates or medical claims.

For each topic decide:
- "keep": the premise holds and the angle and why_now are fine as written.
- "fix": the topic is worth writing, but the angle or why_now states something unverified (a specific number of hours or days, a medical mechanism, a cause of swelling, "most popular", a trend figure) or an event detail is wrong. Rewrite the angle and why_now so they ask the reader's question instead of asserting the answer — the draft step will look for sources. Remove any number or mechanism you could not confirm from a reputable medical or official page. Keep the title unless it asserts something unverified (then give a corrected "title").
- "drop": the core premise is false or unsupported (for example, a claimed effect of a treatment that reputable sources do not describe), it needs a clinic or doctor name, it promotes a procedure, it is a pure sightseeing guide with no treatment question, it closely repeats another topic in this list or the existing titles, or it depends on an event whose dates you cannot confirm.

Events and seasons: if a topic depends on a dated event, festival, market, light show, rink or seasonal attraction, search for this year's official dates (the organiser's page, Visit Seoul, Korea Tourism Organization, or a major news outlet). Return "event" with the confirmed name, start and end dates (YYYY-MM-DD) and the source URL. If you cannot confirm this year's dates, choose "drop". If the event has already ended or ends within 10 days of today, choose "drop". Topics that do not depend on a dated event get "event": null.

Existing titles (for duplicate checks):
{{known}}

Topics to check (JSON):
{{topics}}

{{rules}}

Return ONLY a JSON object, no prose:
{
  "topics": [
    {
      "id": "the topic id",
      "verdict": "keep" | "fix" | "drop",
      "reason": "one short sentence in Korean: what you checked and what you found",
      "title": "only when the title must change",
      "angle": "the angle to use (unchanged for keep)",
      "why_now": "the why_now to use (unchanged for keep)",
      "event": {"name": "...", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "source": "https://..."} or null
    }
  ]
}
