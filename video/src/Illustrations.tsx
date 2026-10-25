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
  fiberNew: "#FFFBF3",
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

export type SkinProps = { width: number; height: number; highlight?: string; particles?: string; t: number; frame: number;
  sideLabels?: boolean; collagenStyle?: string };  // mesh_springs(기본, 2026-10-25) | lines | mesh | bundles | springs

// 3단계로 또렷하게 (2026-10-22 "바뀐 점이 직관적으로 보여야"): 0~0.33 1단계 · 0.33~0.66 2단계 · 0.66~1 3단계, 단계 사이만 짧게 넘어간다
export const stageOf = (t: number) => (t < 0.33 ? 0 : t < 0.66 ? 1 : 2);
const stepT = (t: number, a: number, b: number) => interpolate(t, [a, b], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.inOut(Easing.cubic) });

export const SkinSection: React.FC<SkinProps> = ({ width: W, height: H, highlight, particles, t, frame: f, sideLabels = true, collagenStyle = "mesh_springs" }) => {
  // 2026-10-23 다시 그림 — "너무 지저분하다, 고급스럽게": 화장품 광고 피부 단면처럼 요소를 줄이고(섬유아세포·혈관·낙서 섬유 제거),
  // 고른 리듬(벽돌 각질층·세포 줄·평행 콜라겐 리본), 부드러운 파스텔, 얇은 지시선. 효과는 3단계(stageOf) 그대로.
  const top = 58, epiTop = 80, dermTop = H * 0.36, fatTop = H * 0.78;
  const fill = particles === "heat" || particles === "collagen";
  const k = (xs: number[], ys: number[]) => interpolate(t, xs, ys, { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  // 표면: 1단계 접힘·내려앉음 → 3단계 평평 (콜라겐이 차오름)
  const depth = fill ? k([0, 0.33, 0.45, 0.66, 0.8, 1], [46, 46, 30, 30, 2, 2]) : 0;
  const sag = fill ? k([0, 0.33, 0.45, 0.66, 0.8, 1], [16, 16, 9, 9, -4, -4]) : 0;
  const creaseXs = [W * 0.27, W * 0.73];
  const folds = (x: number) => creaseXs.reduce((s, cx) => s + depth * Math.exp(-((x - cx) ** 2) / (2 * 26 * 26)), 0);
  const surf = (x: number, base: number, kk: number) => base + (sag + folds(x)) * kk;
  const path = (base: number, kk: number) => {
    let d = "";
    for (let x = 0; x <= W; x += 5) d += `${x ? " L" : "M"} ${x} ${surf(x, base, kk)}`;
    return d;
  };
  const back = (base: number, kk: number) => {
    let d = "";
    for (let x = W; x >= 0; x -= 5) d += ` L ${x} ${surf(x, base, kk)}`;
    return d;
  };
  const serif = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif";

  // 각질층 벽돌 (표면을 따라 두 줄)
  const bricks: React.ReactNode[] = [];
  for (let row = 0; row < 2; row++)
    for (let x = row * 17; x < W; x += 34) {
      const y = surf(x + 15, top, 1) + 3 + row * 9;
      bricks.push(<rect key={`k${row}-${x}`} x={x + 2} y={y} width={30} height={7} rx={3.5} fill="#FBE9DD" opacity={0.95} />);
    }
  // 표피 세포 두 줄 + 기저 세포 한 줄
  const cells: React.ReactNode[] = [];
  for (let row = 0; row < 3; row++)
    for (let x = (row % 2) * 16 + 12; x < W; x += 32) {
      const base = epiTop + 14 + row * ((dermTop - epiTop - 30) / 2);
      const y = surf(x, base, row === 2 ? 0.25 : 0.85 - row * 0.2);
      cells.push(<ellipse key={`e${row}-${x}`} cx={x} cy={y} rx={13} ry={row === 2 ? 9 : 10} fill={row === 2 ? "#E9B49C" : "#F6D3C0"}
        stroke={row === 2 ? "#DFA187" : "#EEC2AC"} strokeWidth={1.2} opacity={0.9} />);
    }
  // 콜라겐 (2026-10-23 "새 콜라겐이 지저분하다" → 1안 + 3안 약하게): 모두 같은 방향·같은 굵기·같은 물결의 평행선.
  // 12칸 중 홀수 칸 6줄 = 원래 콜라겐, 짝수 칸 6줄 = 새 콜라겐(기존 줄 사이에 끼워짐, 밝은 크림색, 왼쪽에서 오른쪽으로 그려짐).
  // 6줄 → (PDLLA 2단계 9줄) → 12줄. 진피는 3단계에 아래로 조금 두꺼워진다(피하지방 쪽으로, 표면은 그대로).
  const plump = fill ? k([0, 0.66, 0.82, 1], [0, 0, 14, 14]) : 0;
  const fatY = fatTop + plump;
  const dH = fatY - dermTop;
  const SLOTS = 12;
  const slotY = (j: number) => dermTop + 20 + j * ((dH - 40) / (SLOTS - 1));
  const amp = particles === "heat" ? k([0, 0.33, 0.45, 1], [9, 9, 2.5, 2.5]) : 4;  // 열: 느슨한 물결 → 수축해 펴짐
  const lineD = (y: number) => wave(0, W, y, amp, 8, 0.6);  // 같은 위상 → 서로 엇갈리지 않는다
  const ribbons: React.ReactNode[] = [];
  const fresh: React.ReactNode[] = [];
  for (let j = 0; j < SLOTS; j++) {
    const y = slotY(j);
    if (j % 2 === 1 || !fill) {  // 원래 콜라겐 (채우기 전엔 조금 옅게, 다 차면 같이 또렷해짐)
      if (!fill && j % 2 === 0) continue;
      const op = fill ? k([0, 0.66, 0.82, 1], [0.38, 0.38, 0.75, 0.75]) : 0.65;
      ribbons.push(<path key={`r${j}`} d={lineD(y)} stroke="#F6D2C0" strokeWidth={6} strokeLinecap="round" fill="none" opacity={op} />);
    } else {  // 새 콜라겐
      const order = j / 2;  // 0..5
      const early = particles === "collagen" && order % 2 === 0;  // PDLLA: 3줄은 2단계, 나머지 3줄은 3단계
      const start = particles === "heat" ? 0.66 : early ? 0.36 : 0.66;
      const grow = stepT(t, start + order * 0.012, start + 0.13 + order * 0.012);
      const len = W * 1.15;
      fresh.push(<path key={`n${j}`} d={lineD(y)} stroke="#FFFCF7" strokeWidth={7} strokeLinecap="round" fill="none"
        strokeDasharray={len} strokeDashoffset={len * (1 - grow)} opacity={0.97} />);
    }
  }
  // ---- 콜라겐 다른 표현 (비교안) — 늘어나는 양 g: 0 → (PDLLA 2단계 0.5) → 1 ----
  const g = particles === "heat" ? k([0, 0.66, 0.8, 1], [0, 0, 1, 1]) : particles === "collagen" ? k([0, 0.33, 0.45, 0.66, 0.8, 1], [0, 0, 0.5, 0.5, 1, 1]) : 0;
  const oldOp = fill ? k([0, 0.66, 0.82, 1], [0.4, 0.4, 0.75, 0.75]) : 0.65;
  const top0 = dermTop + 8, bot0 = fatY - 8;
  if (collagenStyle !== "lines") {
    ribbons.length = 0;
    fresh.length = 0;
  }
  if (collagenStyle === "mesh") {
    // ① 엮인 그물: 두 방향(±기울기) 물결 다발. 원래 4개(옅게) + 새 8개(밝게, 왼→오 그려짐)
    const bundle = (i: number, dir: number, y0: number, isNew: boolean, show: number) => {
      const slope = dir * 0.16;
      let d = "";
      for (let x = -60; x <= W + 60; x += 8) d += `${x === -60 ? "M" : " L"} ${x} ${y0 + slope * (x - W / 2) + Math.sin(x / 46 + i) * 5}`;
      const len = W * 1.4;
      return <path key={`m${isNew ? "n" : "o"}${i}`} d={d} stroke={isNew ? "#FFFCF7" : "#F6D2C0"} strokeWidth={isNew ? 7 : 6} strokeLinecap="round"
        fill="none" opacity={isNew ? 0.97 : oldOp} strokeDasharray={isNew ? len : undefined} strokeDashoffset={isNew ? len * (1 - show) : undefined} />;
    };
    const span = bot0 - top0;
    [0.25, 0.75].forEach((p, i) => { ribbons.push(bundle(i, 1, top0 + span * p, false, 1)); ribbons.push(bundle(i + 2, -1, top0 + span * p, false, 1)); });
    [0.12, 0.42, 0.58, 0.88].forEach((p, i) => {
      [1, -1].forEach((dir, j) => {
        const n = i * 2 + j;  // 0..7
        const show = stepT(g * 8 - n, 0, 1);
        if (fill) fresh.push(bundle(10 + n, dir, top0 + span * p, true, show));
      });
    });
  } else if (collagenStyle === "bundles") {
    // ② 잘린 다발 단면: 작은 원 묶음. 원래 6묶음(작고 옅게) + 새 10묶음(밝게, 커짐)
    const cluster = (cx: number, cy: number, scale: number, isNew: boolean, key: string) => {
      const pts = [[0, 0], [-9, -6], [9, -6], [-10, 6], [10, 6], [0, -11], [0, 11]];
      return (
        <g key={key} transform={`translate(${cx} ${cy}) scale(${scale})`} opacity={isNew ? 0.98 : Math.max(0.75, oldOp)}>
          <ellipse rx={20} ry={18} fill={isNew ? "#FFF4EA" : "#E9A68C"} stroke={isNew ? "#FFFFFF" : "#DC9478"} strokeWidth={1.5} />
          {pts.map(([x, y], i) => <circle key={i} cx={x} cy={y} r={4.2} fill={isNew ? "#FFFFFF" : "#F4CBB8"} />)}
        </g>
      );
    };
    const rows = 3, cols = Math.max(4, Math.round(W / 120));
    let n = 0;
    for (let r = 0; r < rows; r++)
      for (let c = 0; c < cols; c++) {
        const cx = (c + 0.5 + (r % 2) * 0.5) * (W / (cols + 0.5)), cy = top0 + 18 + r * ((bot0 - top0 - 36) / (rows - 1));
        if ((r + c) % 2 === 0) ribbons.push(cluster(cx, cy, 0.85, false, `bo${r}-${c}`));
        else if (fill) {
          const show = stepT(g * 10 - n, 0, 1);
          n++;
          if (show > 0) fresh.push(cluster(cx, cy, 0.4 + 0.75 * show, true, `bn${r}-${c}`));
        }
      }
  } else if (collagenStyle === "mesh_springs") {
    // 2026-10-25 사람 제안: 원래 콜라겐 = 성긴 그물(얇은 다발 4개), 새 콜라겐 = 그물을 받쳐 보강하는 입체 코일 스프링
    const span = bot0 - top0;
    const strand = (i: number, dir: number, y0: number) => {
      let d = "";
      for (let x = -60; x <= W + 60; x += 8) d += `${x === -60 ? "M" : " L"} ${x} ${y0 + dir * 0.14 * (x - W / 2) + Math.sin(x / 50 + i) * 4}`;
      return <path key={`ms${i}`} d={d} stroke="#F4CCB9" strokeWidth={5} strokeLinecap="round" fill="none" opacity={Math.max(0.7, oldOp)} />;
    };
    [[0.3, 1], [0.3, -1], [0.72, 1], [0.72, -1]].forEach(([p, dir], i) => ribbons.push(strand(i, dir, top0 + span * p)));
    if (fill) {
      const coil = (cx: number, show: number, key: string) => {
        // 입체 코일: 고리마다 뒤쪽 반원(가늘고 진하게) + 앞쪽 반원(굵고 밝게), 아래에서 위로 자란다
        const loops = 6, rx = 16, ry = 5;
        const yBot = bot0 - 2, yTop = top0 + 4, h = (yBot - yTop) * show;
        const parts: React.ReactNode[] = [];
        for (let l = 0; l < loops; l++) {
          const cy = yBot - (h * (l + 0.5)) / loops;
          if (show <= 0) break;
          parts.push(<path key={`b${l}`} d={`M ${cx - rx} ${cy} A ${rx} ${ry} 0 0 1 ${cx + rx} ${cy}`} stroke="#E8B7A1" strokeWidth={3} fill="none" />);
          parts.push(<path key={`f${l}`} d={`M ${cx - rx} ${cy} A ${rx} ${ry} 0 0 0 ${cx + rx} ${cy - h / loops}`} stroke="#FFFCF7" strokeWidth={5.5}
            fill="none" strokeLinecap="round" />);
        }
        return (
          <g key={key} opacity={show > 0 ? 1 : 0}>
            <ellipse cx={cx} cy={yBot + 3} rx={rx + 4} ry={4} fill="#C98A72" opacity={0.18 * show} />
            {parts}
            <circle cx={cx} cy={yBot - h} r={4} fill="#FFFCF7" opacity={show} />
          </g>
        );
      };
      const xs = [0.16, 0.38, 0.6, 0.82].map((p) => W * p);
      const order = [1, 3, 0, 2];  // PDLLA 2단계: 먼저 두 개
      xs.forEach((x, i) => {
        const show = stepT(g * 4 - order.indexOf(i), 0, 1);
        fresh.push(coil(x, show, `co${i}`));
      });
    }
  } else if (collagenStyle === "springs") {
    // ③ 스프링 받침(비유): 원래 5개는 눌려 촘촘·옅게 → 탄탄해짐, 새 스프링이 사이에 생김
    const spring = (x: number, isNew: boolean, firm: number, key: string, show = 1) => {
      const coils = 9 - 3 * firm, y0 = bot0, y1 = top0 + 6;
      let d = `M ${x} ${y0}`;
      const steps = 80;
      for (let s = 1; s <= steps; s++) {
        const yy = y0 + (y1 - y0) * (s / steps) * show;
        d += ` L ${x + Math.sin((s / steps) * coils * Math.PI * 2) * 11} ${yy}`;
      }
      return <path key={key} d={d} stroke={isNew ? "#FFFCF7" : "#F2C2AC"} strokeWidth={isNew ? 5 : 4 + firm} fill="none"
        strokeLinecap="round" strokeLinejoin="round" opacity={isNew ? 0.97 : Math.max(0.8, oldOp)} />;
    };
    const N = 5, firm = fill ? g : 1;
    for (let i = 0; i < N; i++) ribbons.push(spring(W * (0.1 + i * 0.2), false, firm, `so${i}`));
    if (fill) for (let i = 0; i < N - 1; i++) {
      const show = stepT(g * 4 - i, 0, 1);
      if (show > 0) fresh.push(spring(W * (0.2 + i * 0.2), true, 1, `sn${i}`, show));
    }
  }
  // 지방층: 부드러운 원
  const fat: React.ReactNode[] = [];
  for (let row = 0; row < 3; row++)
    for (let x = (row % 2) * 34 + 30; x < W + 30; x += 68) {
      const y = fatY + 34 + row * 54;
      if (y - 30 > H) continue;
      fat.push(<circle key={`a${row}-${x}`} cx={x} cy={y} r={31} fill="url(#fatCell)" stroke="#EED3A6" strokeWidth={1.5} />);
    }
  // 입자 (주사형): 작고 정돈된 진주 — 스며듦은 표면 위 → 표피 → 진피, PDLLA 는 진피에 놓였다가 3단계에 작아짐
  const pearls = particles === "diffuse" || particles === "collagen" ? Array.from({ length: 12 }, (_, i) => {
    const x = W * (0.08 + (i % 6) * 0.168) + (i >= 6 ? W * 0.08 : 0);
    const target = slotY(i >= 6 ? 8 : 4);  // 새 콜라겐 줄 자리 — 그 주변에서 콜라겐이 생긴다
    const y = particles === "collagen" ? target : k([0, 0.28, 0.4, 0.6, 0.72, 1], [top - 40, top - 40, (epiTop + dermTop) / 2, (epiTop + dermTop) / 2, target, target]);
    const r = particles === "collagen" ? 10 * (1 - 0.55 * stepT(t, 0.62, 0.8)) : 10;
    return <circle key={`p${i}`} cx={x} cy={y} r={r} fill="url(#pearl)" />;
  }) : null;
  // 돌기 (가라앉음)
  const bumps = particles === "settle" ? creaseXs.concat([W * 0.5]).map((x, i) => {
    const h = k([0, 0.28, 0.42, 0.6, 0.74, 1], [30, 30, 15, 15, 1, 1]);
    return <path key={`u${i}`} d={`M ${x - 40} ${top + 1} Q ${x} ${top - h * 2} ${x + 40} ${top + 1} Z`} fill="#F4CFBA" stroke="#E8B49C" strokeWidth={1.5} />;
  }) : null;
  const heatOn = particles === "heat" ? k([0, 0.08, 0.6, 0.72, 1], [0, 1, 1, 0.1, 0.1]) : 0;
  const hl = (id: string) => highlight === id;
  const tag = (x: number, y: number, tx: number, ty: number, text: string, dark = false) => (
    <g>
      <line x1={x} y1={y} x2={tx} y2={ty + 6} stroke="#3C5A4B" strokeWidth={1.6} />
      <circle cx={x} cy={y} r={4} fill="#3C5A4B" />
      <text x={tx + 6} y={ty} fontFamily={serif} fontSize={sideLabels ? 22 : 18} fontWeight={700} letterSpacing={1.2}
        fill={dark ? "#1F3B2D" : "#3C5A4B"} stroke="#FFFFFF" strokeWidth={5} paintOrder="stroke">{text.toUpperCase()}</text>
    </g>
  );
  const ribbonY = slotY(3);
  const freshY = slotY(6);
  const newShown = particles === "heat" ? t > 0.72 : particles === "collagen" ? t > 0.42 : false;

  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ overflow: "visible" }}>
      <defs>
        <linearGradient id="epiG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#FBE3D5" /><stop offset="1" stopColor="#F3C9B4" /></linearGradient>
        <linearGradient id="dermG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#EFB9A3" /><stop offset="1" stopColor="#E3A28A" /></linearGradient>
        <linearGradient id="newCol" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stopColor="#FFF8EE" /><stop offset="0.5" stopColor="#FFFFFF" /><stop offset="1" stopColor="#FFF3E2" /></linearGradient>
        <radialGradient id="fatCell" cx="0.4" cy="0.35" r="0.75"><stop offset="0" stopColor="#FFF6E2" /><stop offset="1" stopColor="#F6DFB5" /></radialGradient>
        <radialGradient id="pearl" cx="0.35" cy="0.3" r="0.75"><stop offset="0" stopColor="#FFFFFF" /><stop offset="0.45" stopColor="#D9EEE4" /><stop offset="1" stopColor="#5E9C82" /></radialGradient>
        <radialGradient id="warm" cx="0.5" cy="0.45" r="0.7"><stop offset="0" stopColor="#FF9D6E" stopOpacity="0.75" /><stop offset="1" stopColor="#FF9D6E" stopOpacity="0" /></radialGradient>
        <filter id="softGlow" x="-5%" y="-60%" width="110%" height="220%"><feGaussianBlur stdDeviation="3" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
        <filter id="lift" x="-6%" y="-6%" width="112%" height="120%"><feDropShadow dx="0" dy="14" stdDeviation="18" floodColor="#9A6650" floodOpacity="0.18" /></filter>
        <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#FFFFFF" stopOpacity="0.55" /><stop offset="1" stopColor="#FFFFFF" stopOpacity="0" /></linearGradient>
        <clipPath id="blk"><rect x="0" y={top - 70} width={W} height={H - top + 70} rx="26" /></clipPath>
      </defs>
      <g filter="url(#lift)" clipPath="url(#blk)">
        <rect x={0} y={fatY} width={W} height={H - fatY} fill="#FBEACB" />
        {fat}
        <rect x={0} y={dermTop - 6} width={W} height={fatY - dermTop + 6} fill="url(#dermG)" />
        {heatOn > 0 ? <rect x={0} y={dermTop - 6} width={W} height={dH + 6} fill="url(#warm)" opacity={heatOn} /> : null}
        {ribbons}
        {fresh}
        <path d={`${path(epiTop, 0.9)}${back(dermTop, 0.25)} Z`} fill="url(#epiG)" />
        {cells}
        <path d={`${path(top, 1)}${back(epiTop, 0.9)} Z`} fill="#FCEEE4" />
        {bricks}
        <path d={path(top, 1)} stroke="#E7B9A2" strokeWidth={2} fill="none" />
        <path d={`${path(top, 1)}${back(top + 26, 1)} Z`} fill="url(#sheen)" />
        {bumps}
        {pearls}
        {particles === "heat" ? [0.25, 0.5, 0.75].map((p, i) => (
          <path key={`h${i}`} d={`M ${W * p} ${top - 50} C ${W * p + 16} ${top - 20}, ${W * p - 16} ${top + 30}, ${W * p} ${dermTop + 40}`}
            stroke="#F08A5D" strokeWidth={3} fill="none" strokeLinecap="round" opacity={heatOn * 0.9} strokeDasharray="2 10" />
        )) : null}
      </g>
      {fill ? (
        <g>
          {tag(W * 0.1, ribbonY, W * 0.1, H + 30, "Collagen")}
          {newShown ? tag(W * 0.58, freshY, W * 0.58, H + 30, "New collagen", true) : null}
          {depth > 12 ? tag(creaseXs[0], surf(creaseXs[0], top, 1), creaseXs[0] + 34, top - 38, "Fine line") : null}
        </g>
      ) : null}
      {sideLabels && [["Epidermis", (epiTop + dermTop) / 2, "epidermis"], ["Dermis", (dermTop + fatY) / 2, "dermis"], ["Fat layer", (fatY + H) / 2, "subcutis"]].map(([n, y, id]) => (
        <g key={String(n)}>
          <line x1={W + 6} y1={Number(y)} x2={W + 26} y2={Number(y)} stroke="#3C5A4B" strokeWidth={1.6} />
          <text x={W + 32} y={Number(y) + 8} fontFamily={serif} fontSize={24} letterSpacing={1.2} fontWeight={hl(String(id)) ? 800 : 500}
            fill={hl(String(id)) ? "#1F3B2D" : "#6B8276"}>{String(n).toUpperCase()}</text>
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
