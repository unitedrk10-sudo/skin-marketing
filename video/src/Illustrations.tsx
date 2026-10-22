// 고급 도식 (2026-10-22 — "알맹이로 그리지 말고 고급스럽게 구체적으로").
// 피부 단면: 각질층·표피(기저세포)·진피(콜라겐 다발·섬유아세포·모세혈관)·피하지방(지방세포), 그라데이션·깊이.
// 이중 나선: 앞뒤 깊이에 따라 굵기·색이 바뀌는 골격 + 염기쌍, 천천히 회전하며 왼쪽부터 조립.
// 효과(입자 스며듦·돌기 가라앉음·콜라겐 섬유 생성)는 승인된 블로그 문장이 말하는 만큼만 — 무엇을 그릴지는 explainer_plan 이 정한다.
import React from "react";
import { Easing, interpolate } from "remotion";

const P = {
  corneum: ["#F7E4D6", "#F1D6C3"],
  epi: ["#F3CDB5", "#E9B699"],
  dermis: ["#E6AC92", "#D48F75"],
  fat: ["#FBEBD2", "#F0D3A6"],
  fiber: "#F8DCCB",
  fiberNew: "#FFF4EA",
  fibroblast: "#B9644F",
  vessel: "#C4544D",
  ink: "#1F3B2D",
  inkSoft: "#4F6B5C",
  accent: "#C8875E",
  green: "#2F6B52",
};

// 매 프레임 같은 모양이 나오도록 고정 난수
const rand = (seed: number) => {
  const x = Math.sin(seed * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};

const wavePts = (x0: number, x1: number, y: number, amp: number, step: number, phase = 0) => {
  const pts: [number, number][] = [];
  for (let x = x0; x <= x1; x += step) pts.push([x, y + Math.sin(x / 38 + phase) * amp + Math.sin(x / 13 + phase * 2) * amp * 0.25]);
  return pts;
};
const wave = (x0: number, x1: number, y: number, amp: number, step: number, phase = 0) =>
  wavePts(x0, x1, y, amp, step, phase).map(([x, yy], i) => `${i ? "L" : "M"} ${x} ${yy}`).join(" ");

export type SkinProps = { width: number; height: number; highlight?: string; particles?: string; t: number; frame: number };

export const SkinSection: React.FC<SkinProps> = ({ width: W, height: H, highlight, particles, t, frame: f }) => {
  const top = 46, epiTop = 66, dermTop = 170, fatTop = H * 0.76;
  const ease = (a: number, b: number) => interpolate(t, [a, b], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.inOut(Easing.cubic) });
  const glow = 0.55 + 0.45 * Math.sin(f / 9);
  const band = (id: string) => (id === "epidermis" ? [epiTop, dermTop] : id === "dermis" ? [dermTop, fatTop] : [fatTop, H]);

  // 콜라겐 다발 (진피를 가로지르는 물결 섬유)
  const bundles = Array.from({ length: 11 }, (_, i) => {
    const y = dermTop + 22 + i * ((fatTop - dermTop - 40) / 10);
    return <path key={`b${i}`} d={wave(0, W, y, 6 + rand(i) * 6, 6, i * 1.7)} stroke={P.fiber} strokeWidth={5 + rand(i + 9) * 6}
      strokeLinecap="round" fill="none" opacity={0.42 + rand(i + 3) * 0.25} />;
  });
  // 섬유아세포
  const fibro = Array.from({ length: 7 }, (_, i) => {
    const x = 70 + rand(i + 40) * (W - 160), y = dermTop + 40 + rand(i + 50) * (fatTop - dermTop - 80);
    const r = rand(i + 60) * 40 - 20;
    return (
      <g key={`f${i}`} transform={`translate(${x} ${y}) rotate(${r})`}>
        <ellipse rx={30} ry={9} fill={P.fibroblast} opacity={0.55} />
        <ellipse rx={9} ry={6} fill="#8E3F31" opacity={0.75} />
      </g>
    );
  });
  // 모세혈관 고리
  const vessels = [0.18, 0.5, 0.82].map((p, i) => {
    const x = W * p, y0 = fatTop - 6, y1 = epiTop + 120;
    return <path key={`v${i}`} d={`M ${x - 22} ${y0} C ${x - 30} ${y1 + 60}, ${x - 12} ${y1}, ${x} ${y1} S ${x + 30} ${y1 + 60}, ${x + 22} ${y0}`}
      stroke={P.vessel} strokeWidth={7} fill="none" opacity={0.55} strokeLinecap="round" />;
  });
  // 지방세포
  const fat: React.ReactNode[] = [];
  for (let row = 0; row < 4; row++)
    for (let col = 0; col < 16; col++) {
      const r = 26 + rand(row * 31 + col) * 8;
      const x = col * 58 + (row % 2) * 29 + 10, y = fatTop + 22 + row * 44;
      if (y - r > H) continue;
      fat.push(<circle key={`a${row}-${col}`} cx={x} cy={y} r={r} fill="url(#fatCell)" stroke="#E2BE8C" strokeWidth={2} />);
    }
  // 기저세포 줄
  const basal = Array.from({ length: 30 }, (_, i) => (
    <ellipse key={`c${i}`} cx={14 + i * 30} cy={dermTop - 14} rx={13} ry={10} fill="#DFA286" stroke="#CF8E73" strokeWidth={1.5} opacity={0.8} />
  ));

  // 입자 (윤기 있는 구슬)
  const spheres = Array.from({ length: 16 }, (_, i) => {
    const x = 60 + rand(i + 70) * (W - 120);
    const target = dermTop + 30 + rand(i + 80) * (fatTop - dermTop - 70);
    const k = ease(i / 50, 0.5 + i / 50);
    const y = particles === "collagen" ? target : top - 70 + (target - top + 70) * k;
    const shrink = particles === "collagen" ? 1 - 0.35 * ease(0.2, 1) : 1;
    return <circle key={`p${i}`} cx={x} cy={y} r={11 * shrink} fill="url(#pearl)" opacity={particles === "settle" ? 0 : 0.95} />;
  });
  // 새 콜라겐 섬유 (입자 주변에서 그려진다)
  const newFibers = particles === "collagen" ? Array.from({ length: 16 }, (_, i) => {
    const x = 60 + rand(i + 70) * (W - 120), y = dermTop + 30 + rand(i + 80) * (fatTop - dermTop - 70);
    const len = 120, k = ease(0.15 + i / 60, 0.75 + i / 60);
    return <path key={`n${i}`} d={`M ${x - 60} ${y + 10} C ${x - 30} ${y - 18}, ${x + 30} ${y + 22}, ${x + 60} ${y - 6}`}
      stroke={P.fiberNew} strokeWidth={4} fill="none" strokeLinecap="round" strokeDasharray={len} strokeDashoffset={len * (1 - k)} opacity={0.9} />;
  }) : null;
  // 표면 돌기 (가라앉는다)
  const bumps = particles === "settle" ? Array.from({ length: 7 }, (_, i) => {
    const x = 80 + i * ((W - 160) / 6), h = 26 * (1 - ease(0.1, 0.9)) + 2;
    return <path key={`u${i}`} d={`M ${x - 34} ${top + 2} Q ${x} ${top - h * 2} ${x + 34} ${top + 2} Z`} fill="url(#epiGrad)" stroke="#E2A98E" strokeWidth={2} />;
  }) : null;

  const [hy0, hy1] = highlight ? band(highlight) : [0, 0];
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ overflow: "visible" }}>
      <defs>
        <linearGradient id="epiGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={P.epi[0]} /><stop offset="1" stopColor={P.epi[1]} /></linearGradient>
        <linearGradient id="dermGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={P.dermis[0]} /><stop offset="1" stopColor={P.dermis[1]} /></linearGradient>
        <linearGradient id="corGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={P.corneum[0]} /><stop offset="1" stopColor={P.corneum[1]} /></linearGradient>
        <radialGradient id="fatCell" cx="0.4" cy="0.35" r="0.7"><stop offset="0" stopColor={P.fat[0]} /><stop offset="1" stopColor={P.fat[1]} /></radialGradient>
        <radialGradient id="pearl" cx="0.35" cy="0.3" r="0.75"><stop offset="0" stopColor="#FFFFFF" /><stop offset="0.35" stopColor="#CFE6DA" /><stop offset="1" stopColor={P.green} /></radialGradient>
        <filter id="soft" x="-5%" y="-5%" width="110%" height="110%"><feDropShadow dx="0" dy="10" stdDeviation="14" floodColor="#7A4B3A" floodOpacity="0.22" /></filter>
        <clipPath id="block"><rect x="0" y={top - 60} width={W} height={H - top + 60} rx="28" /></clipPath>
      </defs>
      <g filter="url(#soft)" clipPath="url(#block)">
        <rect x={0} y={fatTop} width={W} height={H - fatTop} fill="#F5DDB6" />
        {fat}
        <rect x={0} y={dermTop} width={W} height={fatTop - dermTop} fill="url(#dermGrad)" />
        {vessels}
        {bundles}
        {fibro}
        {newFibers}
        <path d={`${wave(0, W, epiTop, 3, 8)} ${wavePts(0, W, dermTop, 7, 8).reverse().map(([x, y]) => `L ${x} ${y}`).join(" ")} Z`} fill="url(#epiGrad)" />
        {basal}
        <path d={`${wave(0, W, top, 2.5, 8)} L ${W} ${epiTop} L 0 ${epiTop} Z`} fill="url(#corGrad)" />
        {Array.from({ length: 22 }, (_, i) => <path key={`s${i}`} d={`M ${i * 42 + 6} ${top + 8} q 14 -5 28 0`} stroke="#E8C7B2" strokeWidth={2} fill="none" />)}
        {bumps}
        {spheres}
        {highlight ? <rect x={3} y={hy0} width={W - 6} height={hy1 - hy0} fill="none" stroke={P.ink} strokeWidth={4 + glow * 3} rx={10} opacity={0.85} /> : null}
      </g>
      {[["Epidermis", (epiTop + dermTop) / 2, "epidermis"], ["Dermis", (dermTop + fatTop) / 2, "dermis"], ["Fat layer", (fatTop + H) / 2, "subcutis"]].map(([n, y, id]) => (
        <g key={String(n)}>
          <line x1={W - 8} y1={Number(y)} x2={W + 24} y2={Number(y)} stroke={P.inkSoft} strokeWidth={2} />
          <text x={W + 30} y={Number(y) + 11} fontFamily="'Segoe UI', Arial, sans-serif" fontSize={30} fontWeight={highlight === id ? 800 : 500}
            fill={highlight === id ? P.ink : P.inkSoft}>{n}</text>
        </g>
      ))}
    </svg>
  );
};

export type HelixProps = { width: number; height: number; t: number; frame: number };

export const Helix: React.FC<HelixProps> = ({ width: W, height: H, t, frame: f }) => {
  const cy = H / 2, A = H * 0.3, k = (Math.PI * 2) / 300, phase = f * 0.045;
  const built = interpolate(t, [0, 0.6], [0.15, 1], { extrapolateRight: "clamp", easing: Easing.out(Easing.cubic) }) * W;
  const seg: { z: number; el: React.ReactNode }[] = [];
  for (let x = 0; x < W; x += 6) {
    for (const s of [0, Math.PI]) {
      const a = k * x + phase + s, a2 = k * (x + 6) + phase + s;
      const z = Math.cos(a);
      seg.push({ z, el: <line key={`s${x}-${s}`} x1={x} y1={cy + A * Math.sin(a)} x2={x + 6} y2={cy + A * Math.sin(a2)}
        stroke={s === 0 ? (z > 0 ? "#2F6B52" : "#7FA592") : (z > 0 ? "#B8714A" : "#DDB094")} strokeWidth={10 + 8 * (z + 1) / 2} strokeLinecap="round" /> });
    }
  }
  for (let x = 12; x < W; x += 30) {
    const a = k * x + phase, y1 = cy + A * Math.sin(a), y2 = cy - A * Math.sin(a), mid = (y1 + y2) / 2;
    const z = Math.cos(a) * 0.2;
    const pair = Math.floor(x / 30) % 2 === 0;
    seg.push({ z, el: (
      <g key={`p${x}`} opacity={0.9}>
        <line x1={x} y1={y1} x2={x} y2={mid} stroke={pair ? "#E9C46A" : "#7BB8A4"} strokeWidth={8} strokeLinecap="round" />
        <line x1={x} y1={mid} x2={x} y2={y2} stroke={pair ? "#7BB8A4" : "#E9C46A"} strokeWidth={8} strokeLinecap="round" />
      </g>
    ) });
  }
  seg.sort((a, b) => a.z - b.z);
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`}>
      <defs>
        <clipPath id="built"><rect x={0} y={0} width={built} height={H} /></clipPath>
        <filter id="hglow" x="-10%" y="-30%" width="120%" height="160%"><feGaussianBlur stdDeviation="10" /></filter>
        <radialGradient id="nuc" cx="0.35" cy="0.3" r="0.7"><stop offset="0" stopColor="#FFFFFF" /><stop offset="1" stopColor="#B8714A" /></radialGradient>
      </defs>
      <g clipPath="url(#built)" opacity={0.35} filter="url(#hglow)">{seg.map((s) => s.el)}</g>
      <g clipPath="url(#built)">{seg.map((s) => s.el)}</g>
      {built < W - 4 ? Array.from({ length: 5 }, (_, i) => {
        const p = (f / 40 + i / 5) % 1;
        return <circle key={`n${i}`} cx={built + 40 + (1 - p) * 140} cy={cy + Math.sin(i * 2.1) * A * 0.9} r={9} fill="url(#nuc)" opacity={p} />;
      }) : null}
    </svg>
  );
};
