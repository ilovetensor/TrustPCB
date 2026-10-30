"use client";
import { formatMm, rulerSteps, tickRange } from "../../lib/geometry.mjs";

export interface View { scale: number; tx: number; ty: number }
export const RULER = 22;

export function Rulers({ view, width, height, cursor, gridOnly }: { view: View; width: number; height: number; cursor: [number, number] | null; gridOnly?: boolean }) {
  const { major, minor } = rulerSteps(view.scale, 64);
  const minorPx = minor * view.scale;
  const bx0 = (RULER - view.tx) / view.scale, bx1 = (width - view.tx) / view.scale;
  const by0 = (RULER - view.ty) / view.scale, by1 = (height - view.ty) / view.scale;
  const majorX = tickRange(bx0, bx1, major);
  const majorY = tickRange(by0, by1, major);
  const minorX = minorPx >= 6 ? tickRange(bx0, bx1, minor) : [];
  const minorY = minorPx >= 6 ? tickRange(by0, by1, minor) : [];
  const sx = (x: number) => x * view.scale + view.tx;
  const sy = (y: number) => y * view.scale + view.ty;
  const isMajor = (v: number) => Math.abs(v / major - Math.round(v / major)) < 1e-6;

  return (
    <svg className="rl-svg" width={width} height={height} aria-hidden>
      {/* drawing grid */}
      <g stroke="var(--paper)" strokeWidth="1">
        {minorX.filter((v) => !isMajor(v)).map((v) => (<line key={"gx" + v} x1={sx(v)} x2={sx(v)} y1={RULER} y2={height} opacity="0.025" />))}
        {minorY.filter((v) => !isMajor(v)).map((v) => (<line key={"gy" + v} y1={sy(v)} y2={sy(v)} x1={RULER} x2={width} opacity="0.025" />))}
        {majorX.map((v) => (<line key={"mx" + v} x1={sx(v)} x2={sx(v)} y1={RULER} y2={height} opacity={v === 0 ? 0.16 : 0.06} />))}
        {majorY.map((v) => (<line key={"my" + v} y1={sy(v)} y2={sy(v)} x1={RULER} x2={width} opacity={v === 0 ? 0.16 : 0.06} />))}
      </g>
      {!gridOnly && (
        <g>
          {/* strips */}
          <rect x="0" y="0" width={width} height={RULER} fill="var(--stage)" opacity="0.94" />
          <rect x="0" y="0" width={RULER} height={height} fill="var(--stage)" opacity="0.94" />
          <path d={`M${RULER} ${RULER}H${width}M${RULER} ${RULER}V${height}`} stroke="var(--hair-2)" strokeWidth="1" />
          <g stroke="var(--paper-3)" strokeWidth="1" fill="var(--paper-3)">
            {minorX.map((v) => (<line key={"tx" + v} x1={sx(v)} x2={sx(v)} y1={RULER} y2={RULER - (isMajor(v) ? 11 : 5)} />))}
            {minorY.map((v) => (<line key={"ty" + v} y1={sy(v)} y2={sy(v)} x1={RULER} x2={RULER - (isMajor(v) ? 11 : 5)} />))}
            {minorX.length === 0 && majorX.map((v) => (<line key={"tmx" + v} x1={sx(v)} x2={sx(v)} y1={RULER} y2={RULER - 11} />))}
            {minorY.length === 0 && majorY.map((v) => (<line key={"tmy" + v} y1={sy(v)} y2={sy(v)} x1={RULER} x2={RULER - 11} />))}
            {majorX.map((v) => (<text key={"lx" + v} x={sx(v) + 3} y={9} className="rl-t" stroke="none">{formatMm(v, major)}</text>))}
            {majorY.map((v) => (<text key={"ly" + v} transform={`translate(9 ${sy(v) - 3}) rotate(-90)`} className="rl-t" stroke="none" textAnchor="start">{formatMm(v, major)}</text>))}
          </g>
          <text x={RULER / 2} y={RULER / 2} textAnchor="middle" dy="0.34em" className="rl-t" fill="var(--signal)">mm</text>
          {cursor && (
            <g fill="var(--signal)" stroke="var(--signal)">
              <path d={`M${cursor[0]} ${RULER}l-4 -7h8z`} stroke="none" />
              <path d={`M${RULER} ${cursor[1]}l-7 -4v8z`} stroke="none" />
            </g>
          )}
        </g>
      )}
    </svg>
  );
}
