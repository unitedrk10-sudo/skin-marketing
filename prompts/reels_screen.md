You are helping a human editor translate a short Korean Instagram video (a reel made by a creator). Below are still frames from it, saved as image files in your working folder. Each file name contains the time in seconds (for example "f03_06.0s.jpg" = 6.0 seconds). Open and look at every frame with the Read tool.

FRAMES:
{{files}}

Task 1 — on-screen text. Copy the Korean text that the creator put on the video (captions, titles, labels, stickers) exactly as written, one entry per distinct text, with the time of the first frame where it appears. Skip Instagram's own interface (buttons, account name, music label). If the same text stays on several frames, list it once. Do not translate.

Task 2 — what is in the picture. Report only what you clearly see, never guesses:
- "needle": an injection, needle, cannula, blood, or a fresh wound or bumps in close-up
- "before_after": a before/after comparison (side-by-side, split screen, or "before"/"after" labels)
- "clinic": a clinic or hospital sign, name, logo or price board, or a doctor/staff in uniform speaking to the camera or performing a procedure
- "other_faces": a clearly visible face of a person other than the main person of the video
- "overclaim": on-screen text that promises results ("10 years younger in one session", "100%", "no side effects", "best")

Return ONLY JSON:
{"texts": [{"t": 2.0, "ko": "…"}], "flags": [{"kind": "needle", "t": 6.0, "what": "short description in Korean"}]}
Use empty lists when there is nothing.
