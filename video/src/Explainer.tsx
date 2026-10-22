// 실험 E — 코드로 그리는 설명 화면 (2026-10-21).
// 대본 줄마다 beat 하나: 정해진 도식 종류(kind) 중 하나를 Claude 가 고르고 글자는 사실 목록에서만 (pipeline/explainer.py 가 검사).
// 위 제목·아래 자막·음성은 파이썬(04_render_video.make_video)이 얹는다 → 여기서는 가운데 도식만 (y 480~1260).
import React from "react";
import {
  AbsoluteFill,
  Easing,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

const C = {
  cream: "#F4EFE6",
  ink: "#1F3B2D", // 브랜드 짙은 녹색
  inkSoft: "#4F6B5C",
  sand: "#E3D5C1",
  skin1: "#F1D2BE",
  skin2: "#E2B49A",
  skin3: "#F6E6C9",
  accent: "#C8875E",
  red: "#D9776F",
};
const FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif";
const PANEL = { top: 470, height: 800, left: 70, width: 940 };

export type Beat = {
  start: number;
  end: number;
  kind: string;
  label?: string;
  note?: string;
  highlight?: string;
  particles?: string;
  count?: number;
  interval?: string;
  value?: string;
  unit?: string;
  belief?: string;
  fact?: string;
  items?: string[];
  icon?: string;
  from_label?: string;
  to_label?: string;
  source?: string;
};
export type BgClip = { file: string; seconds: number; from: number; zoom: number; rate?: number };
export type Line = { start: number; end: number; voice: string };
// lines 가 있으면 자막·브랜드·AI 표시를 여기서 같은 디자인으로 그린다 (2026-10-22 — 검은 자막 상자·노란 강조 제거).
export type ExplainerProps = { seconds: number; beats: Beat[]; background: BgClip[]; lines?: Line[]; handle?: string; disclosure?: string };

const useIn = (delay = 0, dur = 14) => {
  const f = useCurrentFrame();
  return interpolate(f - delay, [0, dur], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic) });
};
const usePop = (delay = 0) => {
  const f = useCurrentFrame();
  const { fps } = useVideoConfig();
  return spring({ frame: f - delay, fps, config: { damping: 14, stiffness: 120 } });
};

const Label: React.FC<{ text?: string; y: number; size?: number; color?: string; delay?: number }> = ({ text, y, size = 58, color = C.ink, delay = 4 }) => {
  const a = useIn(delay);
  if (!text) return null;
  return (
    <div style={{ position: "absolute", top: y, left: 0, width: PANEL.width, textAlign: "center", fontFamily: FONT, fontWeight: 700,
      fontSize: size, color, opacity: a, transform: `translateY(${(1 - a) * 18}px)`, padding: "0 30px", boxSizing: "border-box" }}>
      {text}
    </div>
  );
};

// ---- 제목 + 설명: 한 덩어리로 쌓는다 (제목이 두 줄이 돼도 설명과 겹치지 않게) ----
const Caption: React.FC<{ b: Beat; y: number }> = ({ b, y }) => {
  const a = useIn(4);
  return (
    <div style={{ position: "absolute", top: y, left: 0, width: PANEL.width, padding: "0 30px", boxSizing: "border-box", textAlign: "center",
      fontFamily: FONT, opacity: a, transform: `translateY(${(1 - a) * 18}px)` }}>
      {b.label ? <div style={{ fontSize: 58, fontWeight: 700, color: C.ink, lineHeight: 1.15 }}>{b.label}</div> : null}
      {b.note ? <div style={{ fontSize: 40, color: C.inkSoft, marginTop: 14, lineHeight: 1.25 }}>{b.note}</div> : null}
    </div>
  );
};

// ---- 피부 단면: 표피·진피·피하, 강조 층 + 입자(스며듦 diffuse / 가라앉음 settle) ----
const SkinLayers: React.FC<{ b: Beat }> = ({ b }) => {
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const a = useIn(0, 12);
  const W = 840, X = 50, Y = 20;
  const layers = [
    { id: "epidermis", name: "Epidermis", h: 120, c: C.skin1 },
    { id: "dermis", name: "Dermis", h: 280, c: C.skin2 },
    { id: "subcutis", name: "Fat layer", h: 150, c: C.skin3 },
  ];
  let y = Y;
  const t = Math.min(1, f / Math.max(1, durationInFrames - 10));
  const dots = Array.from({ length: 18 }, (_, i) => i);
  const dermisTop = Y + 120;
  return (
    <svg width={PANEL.width} height={PANEL.height} style={{ position: "absolute", opacity: a }}>
      {layers.map((l) => {
        const top = y;
        y += l.h;
        const on = b.highlight === l.id;
        const pulse = on ? 0.5 + 0.5 * Math.sin(f / 8) : 0;
        return (
          <g key={l.id}>
            <rect x={X} y={top} width={W} height={l.h} fill={l.c} stroke={on ? C.ink : "none"} strokeWidth={on ? 4 + pulse * 3 : 0} rx={l.id === "epidermis" ? 26 : 0} />
            <text x={X + W - 24} y={top + 44} textAnchor="end" fontFamily={FONT} fontSize={38} fontWeight={on ? 800 : 500} fill={on ? C.ink : C.inkSoft}>{l.name}</text>
          </g>
        );
      })}
      {/* 피부결 선 */}
      <path d={`M ${X} ${Y + 8} ${Array.from({ length: 12 }, (_, i) => `Q ${X + 34 + i * 68} ${Y - 10 + (i % 2) * 18} ${X + 68 + i * 68} ${Y + 8}`).join(" ")}`} fill="none" stroke={C.accent} strokeWidth={3} opacity={0.6} />
      {b.particles && dots.map((i) => {
        const px = X + 60 + ((i * 97) % (W - 120));
        const startY = b.particles === "settle" ? Y - 30 - (i % 3) * 14 : Y - 60 - (i % 4) * 20;
        const endY = b.particles === "settle" ? Y + 4 : dermisTop + 40 + ((i * 53) % 190);
        const k = interpolate(t, [i / 60, Math.min(1, 0.55 + i / 60)], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.inOut(Easing.quad) });
        return <circle key={i} cx={px + Math.sin((f + i * 9) / 14) * 6} cy={startY + (endY - startY) * k} r={b.particles === "settle" ? 13 * (1 - 0.4 * k) : 11} fill={C.ink} opacity={0.75} />;
      })}
      <foreignObject x={0} y={Y + 535} width={PANEL.width} height={170}>
        <div style={{ fontFamily: FONT, fontSize: 56, fontWeight: 700, color: C.ink, textAlign: "center" }}>{b.label}</div>
        {b.note ? <div style={{ fontFamily: FONT, fontSize: 38, color: C.inkSoft, textAlign: "center", marginTop: 8, padding: "0 30px" }}>{b.note}</div> : null}
      </foreignObject>
    </svg>
  );
};

// ---- 분자 사슬: 조각이 하나씩 이어진다 (PN·DNA 조각 같은 성분 설명) ----
const Molecule: React.FC<{ b: Beat }> = ({ b }) => {
  const f = useCurrentFrame();
  const n = 9;
  return (
    <AbsoluteFill>
      <svg width={PANEL.width} height={560}>
        {Array.from({ length: n }, (_, i) => {
          const p = interpolate(f - i * 4, [0, 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          const x = 110 + i * 90;
          const y1 = 230 + Math.sin(i * 0.9 + f / 20) * 70;
          const y2 = 330 - Math.sin(i * 0.9 + f / 20) * 70;
          return (
            <g key={i} opacity={p} transform={`translate(0 ${(1 - p) * 40})`}>
              <line x1={x} y1={y1} x2={x} y2={y2} stroke={C.sand} strokeWidth={8} />
              <circle cx={x} cy={y1} r={32} fill={C.ink} />
              <circle cx={x} cy={y2} r={32} fill={C.accent} />
            </g>
          );
        })}
      </svg>
      <Caption b={b} y={540} />
    </AbsoluteFill>
  );
};

// ---- 회차 타임라인: count 회, 사이 간격 글자 ----
const Timeline: React.FC<{ b: Beat }> = ({ b }) => {
  const n = Math.max(1, Math.min(6, b.count || 3));
  const gap = 760 / Math.max(1, n - 1);
  const f = useCurrentFrame();
  const line = interpolate(f, [0, 10 + n * 10], [0, 1], { extrapolateRight: "clamp" });
  return (
    <AbsoluteFill>
      <svg width={PANEL.width} height={500}>
        <line x1={90} y1={250} x2={90 + 760 * line} y2={250} stroke={C.sand} strokeWidth={10} strokeLinecap="round" />
        {Array.from({ length: n }, (_, i) => {
          const s = spring({ frame: f - 6 - i * 12, fps: 30, config: { damping: 12 } });
          const x = n === 1 ? 470 : 90 + i * gap;
          return (
            <g key={i} transform={`translate(${x} 250) scale(${s})`}>
              <circle r={74} fill={C.ink} />
              <text y={24} textAnchor="middle" fontFamily={FONT} fontSize={66} fontWeight={800} fill={C.cream}>{i + 1}</text>
            </g>
          );
        })}
        {b.interval && n > 1 && Array.from({ length: n - 1 }, (_, i) => {
          const o = interpolate(f - 14 - i * 12, [0, 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          return <text key={i} x={90 + i * gap + gap / 2} y={390} textAnchor="middle" fontFamily={FONT} fontSize={40} fontWeight={600} fill={C.inkSoft} opacity={o}>{b.interval}</text>;
        })}
      </svg>
      <Caption b={b} y={520} />
    </AbsoluteFill>
  );
};

// ---- 큰 숫자 ----
const BigNumber: React.FC<{ b: Beat }> = ({ b }) => {
  const s = usePop(0);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", top: 120, width: PANEL.width, textAlign: "center", fontFamily: FONT, color: C.ink, transform: `scale(${s})` }}>
        <span style={{ fontSize: 230, fontWeight: 800 }}>{b.value}</span>
        <span style={{ fontSize: 70, fontWeight: 700, marginLeft: 16 }}>{b.unit}</span>
      </div>
      <Caption b={b} y={470} />
    </AbsoluteFill>
  );
};

// ---- 붉은기 빠짐: 피부색 조각의 붉은 기가 시간에 따라 옅어진다 ----
const FadePatch: React.FC<{ b: Beat }> = ({ b }) => {
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const k = interpolate(f, [8, Math.max(9, durationInFrames - 8)], [0.75, 0.05], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <AbsoluteFill>
      <svg width={PANEL.width} height={480}>
        <defs>
          <radialGradient id="flush">
            <stop offset="0%" stopColor={C.red} stopOpacity={k} />
            <stop offset="100%" stopColor={C.red} stopOpacity={0} />
          </radialGradient>
        </defs>
        <rect x={170} y={60} width={600} height={360} rx={180} fill={C.skin1} />
        <ellipse cx={470} cy={240} rx={250} ry={150} fill="url(#flush)" />
        <rect x={170} y={440} width={600 * (1 - (k - 0.05) / 0.7)} height={12} rx={6} fill={C.ink} />
      </svg>
      <div style={{ position: "absolute", top: 470, left: 170, width: 600, display: "flex", justifyContent: "space-between", fontFamily: FONT, fontSize: 36, color: C.inkSoft }}>
        <span>{b.from_label}</span>
        <span>{b.to_label}</span>
      </div>
      <Caption b={b} y={540} />
    </AbsoluteFill>
  );
};

// ---- 흔한 믿음 vs 출처 ----
const MythFact: React.FC<{ b: Beat }> = ({ b }) => {
  const a = useIn(0);
  const c = useIn(16);
  const card = (title: string, text: string | undefined, o: number, dark: boolean, top: number) => (
    <div style={{ position: "absolute", top, left: 40, width: 860, opacity: o, transform: `translateX(${(1 - o) * 40}px)`, background: dark ? C.ink : C.sand,
      color: dark ? C.cream : C.ink, borderRadius: 28, padding: "30px 36px", boxSizing: "border-box", fontFamily: FONT }}>
      <div style={{ fontSize: 32, fontWeight: 700, opacity: 0.8, textTransform: "uppercase", letterSpacing: 2 }}>{title}</div>
      <div style={{ fontSize: 54, fontWeight: 700, marginTop: 12, lineHeight: 1.22 }}>{text}</div>
    </div>
  );
  return (
    <AbsoluteFill>
      {card("Common belief", b.belief, a, false, 60)}
      {card("What sources say", b.fact, c, true, 400)}
    </AbsoluteFill>
  );
};

// ---- 체크리스트 (최대 3) ----
const Checklist: React.FC<{ b: Beat }> = ({ b }) => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill>
      <Label text={b.label} y={60} />
      {(b.items || []).slice(0, 3).map((it, i) => {
        const o = interpolate(f - 8 - i * 12, [0, 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
        return (
          <div key={i} style={{ position: "absolute", top: 200 + i * 150, left: 60, width: 820, display: "flex", alignItems: "center", gap: 28, opacity: o, fontFamily: FONT }}>
            <div style={{ width: 84, height: 84, borderRadius: 22, background: C.ink, color: C.cream, fontSize: 52, fontWeight: 800, display: "flex", alignItems: "center", justifyContent: "center" }}>✓</div>
            <div style={{ fontSize: 54, fontWeight: 600, color: C.ink, lineHeight: 1.2 }}>{it}</div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

// ---- 아이콘 장면 (여행·계획·의사 확인) ----
const ICONS: Record<string, (s: number) => React.ReactNode> = {
  suitcase: () => (<g><rect x={-150} y={-90} width={300} height={220} rx={30} fill={C.ink} /><rect x={-60} y={-140} width={120} height={60} rx={20} fill="none" stroke={C.ink} strokeWidth={18} /><line x1={-70} y1={-90} x2={-70} y2={130} stroke={C.cream} strokeWidth={10} /><line x1={70} y1={-90} x2={70} y2={130} stroke={C.cream} strokeWidth={10} /></g>),
  calendar: () => (<g><rect x={-150} y={-120} width={300} height={260} rx={26} fill={C.cream} stroke={C.ink} strokeWidth={14} /><rect x={-150} y={-120} width={300} height={70} rx={20} fill={C.ink} />{[0, 1, 2].map((r) => [0, 1, 2, 3].map((c) => <circle key={`${r}${c}`} cx={-105 + c * 70} cy={-10 + r * 55} r={13} fill={C.sand} />))}</g>),
  sun: (s) => (<g transform={`rotate(${s * 30})`}><circle r={90} fill={C.accent} />{Array.from({ length: 10 }, (_, i) => <rect key={i} x={-8} y={-170} width={16} height={50} rx={8} fill={C.accent} transform={`rotate(${i * 36})`} />)}</g>),
  bed: () => (<g><rect x={-180} y={0} width={360} height={90} rx={20} fill={C.ink} /><rect x={-180} y={-70} width={110} height={70} rx={24} fill={C.sand} /><rect x={-60} y={-40} width={240} height={40} rx={18} fill={C.sand} /><rect x={-180} y={90} width={24} height={50} fill={C.ink} /><rect x={156} y={90} width={24} height={50} fill={C.ink} /></g>),
  doctor_note: () => (<g><rect x={-130} y={-160} width={260} height={320} rx={22} fill={C.cream} stroke={C.ink} strokeWidth={14} />{[0, 1, 2, 3].map((i) => <rect key={i} x={-90} y={-100 + i * 60} width={i === 3 ? 110 : 180} height={16} rx={8} fill={C.sand} />)}<circle cx={80} cy={110} r={44} fill={C.ink} /><path d="M 60 110 L 76 126 L 102 96" stroke={C.cream} strokeWidth={10} fill="none" /></g>),
  logo: () => (<g><rect x={-140} y={-140} width={280} height={280} rx={56} fill={C.ink} /><text y={70} textAnchor="middle" fontFamily={FONT} fontSize={210} fontWeight={800} fill={C.cream}>S</text></g>),
  drop: () => (<path d="M 0 -150 C 70 -50 110 10 110 60 A 110 110 0 0 1 -110 60 C -110 10 -70 -50 0 -150 Z" fill={C.ink} />),
};
const IconScene: React.FC<{ b: Beat }> = ({ b }) => {
  const s = usePop(0);
  const f = useCurrentFrame();
  const draw = ICONS[b.icon || ""] || ICONS.drop;
  return (
    <AbsoluteFill>
      <svg width={PANEL.width} height={480}>
        <g transform={`translate(470 240) scale(${s * 1.25})`}>{draw(f / 30)}</g>
      </svg>
      <Caption b={b} y={500} />
    </AbsoluteFill>
  );
};

const KINDS: Record<string, React.FC<{ b: Beat }>> = {
  skin_layers: SkinLayers,
  molecule: Molecule,
  timeline: Timeline,
  big_number: BigNumber,
  fade_patch: FadePatch,
  myth_fact: MythFact,
  checklist: Checklist,
  icon_scene: IconScene,
};

const BeatView: React.FC<{ b: Beat; card: boolean }> = ({ b, card }) => {
  const View = KINDS[b.kind] || IconScene;
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const out = interpolate(f, [durationInFrames - 6, durationInFrames], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <div style={{ position: "absolute", top: PANEL.top, left: PANEL.left, width: PANEL.width, height: PANEL.height, opacity: out,
      background: card ? "rgba(244,239,230,0.93)" : "transparent", borderRadius: 44, overflow: "hidden" }}>
      <View b={b} />
      {b.source ? (
        <div style={{ position: "absolute", bottom: 6, width: PANEL.width, textAlign: "center", fontFamily: FONT, fontSize: 28, color: C.inkSoft }}>
          {b.source}
        </div>
      ) : null}
    </div>
  );
};

// ---- 브랜드 머리줄: 로고 + 주소 (항상 같은 자리) ----
const Header: React.FC<{ handle: string; dark: boolean }> = ({ handle, dark }) => (
  <div style={{ position: "absolute", top: 150, left: 0, width: 1080, display: "flex", justifyContent: "center", alignItems: "center", gap: 18 }}>
    <div style={{ width: 64, height: 64, borderRadius: 16, background: C.ink, color: C.cream, fontFamily: FONT, fontWeight: 800, fontSize: 46,
      display: "flex", alignItems: "center", justifyContent: "center" }}>S</div>
    <div style={{ fontFamily: FONT, fontWeight: 700, fontSize: 38, color: dark ? C.cream : C.ink, letterSpacing: 0.5 }}>{handle}</div>
  </div>
);

// ---- 말하는 문장 자막: 4단어씩, 지금 읽는 단어만 브랜드 색 (단어 시간 = 글자 수 비율) ----
const WORDS = 4;
const Subtitle: React.FC<{ lines: Line[] }> = ({ lines }) => {
  const f = useCurrentFrame();
  const now = f / 30;
  const line = lines.find((l) => now >= l.start && now < l.end);
  if (!line) return null;
  const words = line.voice.split(/\s+/).filter(Boolean);
  const total = words.reduce((a, w) => a + w.length + 1, 0);
  let clock = line.start, idx = words.length - 1;
  for (let i = 0; i < words.length; i++) {
    const d = ((line.end - line.start) * (words[i].length + 1)) / total;
    if (now < clock + d) { idx = i; break; }
    clock += d;
  }
  const chunk = Math.floor(idx / WORDS) * WORDS;
  return (
    <div style={{ position: "absolute", top: 1285, left: 90, width: 900, display: "flex", justifyContent: "center" }}>
      <div style={{ background: "rgba(31,59,45,0.92)", borderRadius: 28, padding: "18px 34px", fontFamily: FONT, fontWeight: 700, fontSize: 52,
        lineHeight: 1.25, color: C.cream, textAlign: "center" }}>
        {words.slice(chunk, chunk + WORDS).map((w, i) => (
          <span key={i} style={{ color: chunk + i === idx ? "#F2B38B" : C.cream }}>{w}{" "}</span>
        ))}
      </div>
    </div>
  );
};

export const Explainer: React.FC<ExplainerProps> = ({ seconds, beats, background, lines, handle, disclosure }) => {
  const fps = 30;
  const hasBg = background && background.length > 0;
  let at = 0;
  return (
    <AbsoluteFill style={{ background: C.cream }}>
      {hasBg && background.map((c, i) => {
        const from = Math.round(at * fps);
        at += c.seconds;
        return (
          <Sequence key={`bg${i}`} from={from} durationInFrames={Math.max(1, Math.round(c.seconds * fps))}>
            <AbsoluteFill style={{ transform: `scale(${c.zoom})` }}>
              <OffthreadVideo src={staticFile(c.file)} startFrom={Math.round(c.from * fps)} playbackRate={c.rate || 1} muted style={{ width: "100%", height: "100%", objectFit: "cover" }} />
            </AbsoluteFill>
          </Sequence>
        );
      })}
      {!hasBg && (
        <AbsoluteFill>
          <svg width={1080} height={1920}>
            <circle cx={980} cy={260} r={260} fill={C.sand} opacity={0.45} />
            <circle cx={90} cy={1700} r={320} fill={C.sand} opacity={0.35} />
          </svg>
        </AbsoluteFill>
      )}
      {beats.map((b, i) => (
        <Sequence key={i} from={Math.round(b.start * fps)} durationInFrames={Math.max(1, Math.round((b.end - b.start) * fps))}>
          <BeatView b={b} card={!!hasBg} />
        </Sequence>
      ))}
      {lines && lines.length > 0 ? (
        <>
          {handle ? <Header handle={handle} dark={!!hasBg} /> : null}
          <Subtitle lines={lines} />
          {disclosure ? (
            <div style={{ position: "absolute", top: 1500, width: 1080, textAlign: "center", fontFamily: FONT, fontSize: 28, color: hasBg ? C.cream : C.inkSoft }}>
              {disclosure}
            </div>
          ) : null}
        </>
      ) : null}
      <div style={{ display: "none" }}>{seconds}</div>
    </AbsoluteFill>
  );
};
