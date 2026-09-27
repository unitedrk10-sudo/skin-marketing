Write a {{duration}}-second vertical short-form video script (TikTok / Instagram Reels / YouTube Shorts) in English.

Topic: {{title}}
Angle: {{angle}}
Suggested hook: {{hook}}
{{revision_note}}

You may ONLY use the facts below. Do not add any fact, number or claim that is not in this list. Every line that states a fact must list the fact ids it relies on.

FACTS:
{{facts}}

Style:
- First 2-3 seconds: a question-style hook with visible motion (describe the visual).
- A neutral AI explainer voice (TTS). The narrator never speaks as a patient.
- Big on-screen captions; short sentences; about 2.3 spoken words per second.
- Mention that results and side effects vary and to consult a licensed doctor.
- Last line: a soft call to follow/save for more info (no booking, no DM-for-price).

{{rules}}

Return ONLY JSON, no prose:
{
  "title": "video title, max 70 chars, main keyword first",
  "hook": "the spoken hook",
  "lines": [
    {"voice": "spoken line", "caption": "on-screen caption", "visual": "b-roll / motion description", "fact_ids": ["F1"]}
  ],
  "on_screen_disclosure": "AI-generated content",
  "hashtags": ["#kbeauty", "..."],
  "estimated_seconds": 45
}
