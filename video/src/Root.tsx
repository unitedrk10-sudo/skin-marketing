import React from "react";
import { Composition } from "remotion";
import { Explainer, ExplainerProps, Figure, FigureProps } from "./Explainer";

const sample: ExplainerProps = {
  seconds: 6,
  background: [],
  beats: [
    { start: 0, end: 3, kind: "skin_layers", highlight: "dermis", particles: "diffuse", label: "PN fragments", note: "" },
    { start: 3, end: 6, kind: "timeline", count: 3, interval: "2 weeks apart", label: "One trial's protocol", note: "" },
  ],
};

const figureSample: FigureProps = { beat: { start: 0, end: 4, kind: "molecule", label: "PN", note: "" } };

export const Root: React.FC = () => (
  <>
  <Composition
    id="Explainer"
    component={Explainer}
    width={1080}
    height={1920}
    fps={30}
    durationInFrames={180}
    defaultProps={sample}
    calculateMetadata={({ props }) => ({ durationInFrames: Math.max(1, Math.round(props.seconds * 30)) })}
  />
  <Composition id="Figure" component={Figure} width={1200} height={900} fps={30} durationInFrames={120} defaultProps={figureSample}
    calculateMetadata={({ props }) => (props.beat.kind === "skin_layers" && props.beat.particles && props.beat.stages && props.beat.stages.length === 3
      ? { width: 1800, height: 900 } : {})} />
  </>
);
