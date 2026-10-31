import type { SVGProps } from "react";

const P: Record<string, string> = {
  play: "M5 3.5v9l7.5-4.5z",
  pause: "M4.5 3.5v9M11.5 3.5v9",
  stop: "M4 4h8v8H4z",
  upload: "M8 11V2.5M4.5 6 8 2.5 11.5 6M2.5 13.5h11",
  arrow: "M2 8h11M9 4l4 4-4 4",
  cube: "M8 1.5 14 5v6l-6 3.5L2 11V5zM2 5l6 3.5L14 5M8 8.5v6",
  board: "M2 3h12v10H2zM2 6.5h3M2 9.5h3M8 3v2.5M11 3v2.5",
  layers: "M8 2 14.5 5.5 8 9 1.5 5.5zM1.5 8.5 8 12l6.5-3.5M1.5 11.5 8 15l6.5-3.5",
  send: "M2 8 14 2.5 10 14 7.5 9z",
  close: "M3.5 3.5l9 9M12.5 3.5l-9 9",
  check: "M3 8.5 6.5 12 13 4.5",
  chevron: "M5 3.5 10 8l-5 4.5",
  chevd: "M3.5 5.5 8 10.5l4.5-5",
  fit: "M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10",
  wave: "M1.5 8c1.2-4 2.3-4 3.5 0s2.3 4 3.5 0 2.3-4 3.5 0 2.3 4 3 1",
  file: "M3.5 1.5h6l3 3v10h-9zM9.5 1.5v3h3",
  compare: "M3 3v10M13 3v10M3 8h10M6 5 3 8l3 3M10 5l3 3-3 3",
  chat: "M2 3h12v8H7.5L4 14v-3H2z",
  print: "M4 6V2h8v4M4 11H2V6h12v5h-2M4 9h8v5H4z",
  eye: "M1 8s2.5-4.5 7-4.5S15 8 15 8s-2.5 4.5-7 4.5S1 8 1 8zM8 6a2 2 0 100 4 2 2 0 000-4z",
  dot: "M8 6.5a1.5 1.5 0 100 3 1.5 1.5 0 000-3z",
  plus: "M8 3v10M3 8h10",
  minus: "M3 8h10",
  link: "M6.5 9.5l3-3M5.5 7 4 8.5a2.5 2.5 0 003.5 3.5L9 10.5M10.5 9 12 7.5A2.5 2.5 0 008.5 4L7 5.5",
  grid: "M2 2h12v12H2zM2 6h12M2 10h12M6 2v12M10 2v12",
  bolt: "M9 1.5 3.5 9H8l-1 5.5L12.5 7H8z",
  external: "M9 2.5h4.5V7M13.5 2.5 7 9M12 9v4.5H2.5V4H7",
  question: "M6 6a2 2 0 114 0c0 1.5-2 1.5-2 3M8 11.5v.5",
  rotate: "M13.5 8a5.5 5.5 0 11-1.6-3.9M13.5 2.5v3h-3",
  explode: "M8 1.5v3M8 11.5v3M3.5 5.5 8 8l4.5-2.5M3.5 10.5 8 8l4.5 2.5",
  back: "M9.5 3.5 5 8l4.5 4.5",
  flag: "M3.5 14V2.5h8.5L10 5.5l2 3H3.5",
  search: "M7 2.5a4.5 4.5 0 100 9 4.5 4.5 0 000-9zM10.3 10.3 13.5 13.5",
  gauge: "M2.5 11.5a5.5 5.5 0 1111 0M8 11.5 10.5 7",
  folder: "M2 4a1 1 0 011-1h3l1.5 2H13a1 1 0 011 1v6a1 1 0 01-1 1H3a1 1 0 01-1-1z",
  info: "M8 2a6 6 0 100 12A6 6 0 008 2zM8 7.5V11M8 5v.5",
};

export function Icon({ name, size = 16, ...rest }: { name: keyof typeof P | string; size?: number } & Omit<SVGProps<SVGSVGElement>, "name">) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden {...rest}>
      <path d={P[name] ?? P.dot} />
    </svg>
  );
}
