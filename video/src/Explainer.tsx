// 실험 E — 코드로 그리는 설명 화면 (2026-10-21).
// 대본 줄마다 beat 하나: 정해진 도식 종류(kind) 중 하나를 Claude 가 고르고 글자는 사실 목록에서만 (pipeline/explainer.py 가 검사).
// 위 제목·아래 자막·음성은 파이썬(04_render_video.make_video)이 얹는다 → 여기서는 가운데 도식만 (y 480~1260).
import React from "react";
import { Helix, SkinSection, stageOf } from "./Illustrations";
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
  stages?: string[];  // 3단계 이름 (피부 단면 효과) — 화면 위 단계 표시·블로그 그림 3칸
};
export type BgClip = { file: string; seconds: number; from: number; zoom: number; rate?: number };
export type Line = { start: number; end: number; voice: string };
// lines 가 있으면 자막·브랜드·AI 표시를 여기서 같은 디자인으로 그린다 (2026-10-22 — 검은 자막 상자·노란 강조 제거).
export type ExplainerProps = { seconds: number; beats: Beat[]; background: BgClip[]; lines?: Line[]; handle?: string; disclosure?: string;
  overlay?: string };  // overlay "caption" = 배경 영상 위에 제목·설명만 (도식 카드 없이)

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

// ---- 피부 단면 (고급 도식, Illustrations.tsx): 강조 층 + 입자 스며듦 diffuse / 돌기 가라앉음 settle / 콜라겐 섬유 생성 collagen ----
const SkinLayers: React.FC<{ b: Beat }> = ({ b }) => {
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const a = useIn(0, 12);
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", top: 10, left: 10, opacity: a }}>
        <SkinSection width={760} height={520} highlight={b.highlight} particles={b.particles} t={Math.min(1, f / Math.max(1, durationInFrames - 8))} frame={f} />
      </div>
      {b.stages && b.stages.length === 3 && b.particles ? (() => {
        const s = stageOf(Math.min(1, f / Math.max(1, durationInFrames - 8)));
        return (
          <div style={{ position: "absolute", top: 18, left: 26, display: "flex", gap: 10, alignItems: "center" }}>
            {[0, 1, 2].map((i) => (
              <div key={i} style={{ width: 46, height: 46, borderRadius: 23, fontFamily: FONT, fontWeight: 800, fontSize: 26,
                display: "flex", alignItems: "center", justifyContent: "center",
                background: i === s ? C.ink : "rgba(244,239,230,0.9)", color: i === s ? C.cream : C.inkSoft }}>{i + 1}</div>
            ))}
            <div style={{ background: C.ink, color: C.cream, borderRadius: 22, padding: "8px 20px", fontFamily: FONT, fontWeight: 700, fontSize: 30 }}>
              {b.stages[s]}
            </div>
          </div>
        );
      })() : null}
      <Caption b={b} y={560} />
    </AbsoluteFill>
  );
};

// ---- 이중 나선 (고급 도식): 회전하며 왼쪽부터 조립 (PN·DNA 조각 같은 성분 설명) ----
const Molecule: React.FC<{ b: Beat }> = ({ b }) => {
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  return (
    <AbsoluteFill>
      <div style={{ position: "absolute", top: 40, left: 30 }}>
        <Helix width={880} height={460} t={Math.min(1, f / Math.max(1, durationInFrames - 8))} frame={f} />
      </div>
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

// ---- 배경 영상이 주인공일 때: 아래쪽에 제목·설명·출처만 (영상 위 글자는 어두운 그라데이션 위에) ----
const CaptionOverlay: React.FC<{ b: Beat }> = ({ b }) => {
  const a = useIn(3);
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const out = interpolate(f, [durationInFrames - 6, durationInFrames], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const label = b.kind === "timeline" && b.count ? `${b.label}` : b.label;
  const note = b.kind === "myth_fact" ? b.fact : b.note || (b.items || []).join(" · ");
  const head = b.kind === "myth_fact" ? b.belief : label;
  return (
    <div style={{ position: "absolute", top: 930, left: 70, width: 940, opacity: a * out, transform: `translateY(${(1 - a) * 24}px)`,
      fontFamily: FONT, textAlign: "center" }}>
      {b.kind === "myth_fact" ? <div style={{ fontSize: 30, fontWeight: 700, color: "#F2B38B", letterSpacing: 2 }}>COMMON BELIEF</div> : null}
      {head ? <div style={{ fontSize: 64, fontWeight: 800, color: "#FFF8EE", lineHeight: 1.12, textShadow: "0 2px 18px rgba(0,0,0,0.55)" }}>{head}</div> : null}
      {note ? <div style={{ fontSize: 42, fontWeight: 600, color: "#F4EFE6", marginTop: 16, lineHeight: 1.25, textShadow: "0 2px 14px rgba(0,0,0,0.6)" }}>
        {b.kind === "myth_fact" ? <span style={{ color: "#F2B38B" }}>What sources say: </span> : null}{note}</div> : null}
    </div>
  );
};

const DRAWN = new Set(["timeline", "big_number", "checklist", "myth_fact"]);  // pipeline/explainer.py DRAWN_KINDS 와 같게

const BeatView: React.FC<{ b: Beat; card: boolean }> = ({ b, card }) => {
  const View = KINDS[b.kind] || IconScene;
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const out = interpolate(f, [durationInFrames - 6, durationInFrames], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return (
    <div style={{ position: "absolute", top: PANEL.top, left: PANEL.left, width: PANEL.width, height: PANEL.height, opacity: out,
      background: card ? "rgba(244,239,230,0.93)" : "transparent", borderRadius: 44, overflow: "hidden" }}>
      <View b={b} />
    </div>
  );
};

// ---- 브랜드 머리줄: 로고 + 주소 (항상 같은 자리) ----
const Header: React.FC<{ handle: string; dark: boolean }> = ({ handle }) => (
  <div style={{ position: "absolute", top: 150, left: 0, width: 1080, display: "flex", justifyContent: "center" }}>
    <div style={{ display: "flex", alignItems: "center", gap: 16, background: "rgba(244,239,230,0.92)", borderRadius: 40, padding: "10px 26px 10px 12px" }}>
      <div style={{ width: 60, height: 60, borderRadius: 16, background: C.ink, color: C.cream, fontFamily: FONT, fontWeight: 800, fontSize: 44,
        display: "flex", alignItems: "center", justifyContent: "center" }}>S</div>
      <div style={{ fontFamily: FONT, fontWeight: 700, fontSize: 36, color: C.ink, letterSpacing: 0.5 }}>{handle}</div>
    </div>
  </div>
);

// ---- 아래 한 줄: 왼쪽 AI 표시, 오른쪽 지금 줄의 출처 (2026-10-22 — 출처는 구석 아래 줄로) ----
const BottomLine: React.FC<{ beats: Beat[]; disclosure?: string }> = ({ beats, disclosure }) => {
  const f = useCurrentFrame();
  const now = f / 30;
  const b = beats.find((x) => now >= x.start && now < x.end);
  const pill = { background: "rgba(31,59,45,0.72)", color: "#F4EFE6", borderRadius: 18, padding: "6px 16px", fontFamily: FONT, fontSize: 24 } as const;
  return (
    <div style={{ position: "absolute", top: 1490, left: 60, width: 960, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 20 }}>
      {disclosure ? <div style={pill}>{disclosure}</div> : <div />}
      {b && b.source ? <div style={{ ...pill, maxWidth: 560, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>{b.source}</div> : null}
    </div>
  );
};

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

export const Explainer: React.FC<ExplainerProps> = ({ seconds, beats, background, lines, handle, disclosure, overlay }) => {
  const fps = 30;
  const hasBg = background && background.length > 0;
  let at = 0;
  return (
    <AbsoluteFill style={{ background: C.cream }}>
      {hasBg && background.map((c, i) => {
        const from = Math.round(at * fps);
        at += c.seconds;
        if (!c.file) return null;  // 섞기: 이 구간은 우리 도식 (크림 배경)
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
      {beats.map((b, i) => {
        const onVideo = hasBg && (overlay === "caption" || (overlay === "mix" && !DRAWN.has(b.kind)));
        return (
          <Sequence key={i} from={Math.round(b.start * fps)} durationInFrames={Math.max(1, Math.round((b.end - b.start) * fps))}>
            {onVideo ? (
              <>
                <AbsoluteFill style={{ background: "linear-gradient(180deg, rgba(0,0,0,0.25) 0%, rgba(0,0,0,0) 20%, rgba(0,0,0,0) 44%, rgba(0,0,0,0.5) 62%, rgba(0,0,0,0.6) 100%)" }} />
                <CaptionOverlay b={b} />
              </>
            ) : (
              <BeatView b={b} card={!!hasBg && overlay !== "mix"} />
            )}
          </Sequence>
        );
      })}
      {lines && lines.length > 0 ? (
        <>
          {handle ? <Header handle={handle} dark={!!hasBg} /> : null}
          <Subtitle lines={lines} />
          <BottomLine beats={beats} disclosure={disclosure} />
        </>
      ) : null}
      <div style={{ display: "none" }}>{seconds}</div>
    </AbsoluteFill>
  );
};


// ---- 블로그 그림 (정지 이미지, pipeline/figures.py): 도식 하나 + 작은 출처·브랜드 ----
export type FigureProps = { beat: Beat };
export const Figure: React.FC<FigureProps> = ({ beat }) => {
  const View = KINDS[beat.kind] || IconScene;
  if (beat.kind === "skin_layers" && beat.particles && beat.stages && beat.stages.length === 3) {
    // 3칸 그림: 단계별 상태를 나란히 + 화살표 (블로그에서 바뀐 점이 한눈에 보이게)
    const ts = [0.2, 0.55, 0.95];
    return (
      <AbsoluteFill style={{ background: C.cream }}>
        <div style={{ position: "absolute", top: 34, width: 1800, textAlign: "center", fontFamily: FONT }}>
          <div style={{ fontSize: 52, fontWeight: 800, color: C.ink }}>{beat.label}</div>
          {beat.note ? <div style={{ fontSize: 32, color: C.inkSoft, marginTop: 8 }}>{beat.note}</div> : null}
        </div>
        {ts.map((tt, i) => (
          <div key={i} style={{ position: "absolute", top: 210, left: 60 + i * 590, width: 500 }}>
            <SkinSection width={500} height={420} highlight={beat.highlight} particles={beat.particles} t={tt} frame={0} sideLabels={false} />
            <div style={{ marginTop: 58, display: "flex", gap: 14, alignItems: "center", justifyContent: "center", fontFamily: FONT }}>
              <div style={{ width: 48, height: 48, borderRadius: 24, background: C.ink, color: C.cream, fontWeight: 800, fontSize: 28,
                display: "flex", alignItems: "center", justifyContent: "center" }}>{i + 1}</div>
              <div style={{ fontSize: 32, fontWeight: 700, color: C.ink }}>{beat.stages![i]}</div>
            </div>
          </div>
        ))}
        {[0, 1].map((i) => (
          <svg key={i} width={80} height={60} style={{ position: "absolute", top: 390, left: 560 + i * 590 }}>
            <path d="M 8 30 L 60 30" stroke={C.accent} strokeWidth={8} strokeLinecap="round" />
            <path d="M 50 14 L 72 30 L 50 46" stroke={C.accent} strokeWidth={8} fill="none" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ))}
        <div style={{ position: "absolute", bottom: 26, left: 40, right: 40, display: "flex", justifyContent: "space-between",
          fontFamily: FONT, fontSize: 24, color: C.inkSoft }}>
          <span>Skinbound Korea · illustration (schematic, not to scale · results vary)</span>
          {beat.particles === "heat" || beat.particles === "collagen" ? (
            <span style={{ display: "flex", gap: 22, alignItems: "center" }}>
              <span style={{ display: "flex", gap: 8, alignItems: "center" }}><i style={{ width: 34, height: 10, borderRadius: 5, background: "#F8DCCB", display: "inline-block", border: "1px solid #E3B9A3" }} />Collagen</span>
              <span style={{ display: "flex", gap: 8, alignItems: "center" }}><i style={{ width: 34, height: 10, borderRadius: 5, background: "#FFFBF3", display: "inline-block", boxShadow: "0 0 6px #fff", border: "1px solid #E9D7C5" }} />New collagen</span>
            </span>
          ) : null}
          <span>{beat.source || ""}</span>
        </div>
      </AbsoluteFill>
    );
  }
  return (
    <AbsoluteFill style={{ background: C.cream }}>
      <div style={{ position: "absolute", top: 40, left: 130, width: PANEL.width, height: PANEL.height }}>
        <View b={beat} />
      </div>
      <div style={{ position: "absolute", bottom: 26, left: 40, right: 40, display: "flex", justifyContent: "space-between",
        fontFamily: FONT, fontSize: 24, color: C.inkSoft }}>
        <span>Skinbound Korea · illustration</span>
        <span>{beat.source || ""}</span>
      </div>
    </AbsoluteFill>
  );
};
