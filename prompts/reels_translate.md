Make English subtitles for a Korean creator's Instagram reel. A human editor will put them on a remix of the reel.

{{rules}}

INPUT (JSON):
- "duration": video length in seconds
- "speech": what the creator says, from local speech recognition (may contain recognition errors), with start/end seconds
- "screen": Korean text the creator put on the video, with the second where it first appears

Name hints (procedure and product names that may appear): {{hints}}

{{payload}}

Make one list of subtitle cues in time order:
- One cue per speech segment, same start/end. You may merge two very short neighbouring segments or split a long one at a sentence break; keep the times inside the original segments.
- On-screen text: if it repeats what is being said, skip it. If it adds something (a title, a label, a caption on a silent part), make a cue for it with "source": "screen", starting at its time and lasting until the next cue or about 2.5 seconds. If there is no speech at all, the on-screen text is the subtitle track.
- Cues must not overlap. Keep each English cue short enough to read (about 12 words); split longer ones at natural breaks.
- "ko" is the Korean original of that cue (corrected only as rule 5 allows), "en" its faithful English.

Return ONLY JSON:
{"cues": [{"start": 0.0, "end": 2.4, "source": "speech", "ko": "…", "en": "…"}], "notes": ["…"]}
