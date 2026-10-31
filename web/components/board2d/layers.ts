export interface LayerState {
  top: boolean; bottom: boolean; silk: boolean; zones: boolean; pads: boolean; vias: boolean;
  issues: boolean; regions: boolean; rf: boolean; heat: boolean;
}
export const DEFAULT_LAYERS: LayerState = {
  top: true, bottom: true, silk: true, zones: true, pads: true, vias: true,
  issues: true, regions: true, rf: false, heat: false,
};
export const LAYER_TOGGLES: { key: keyof LayerState; label: string }[] = [
  { key: "issues", label: "Issues" },
  { key: "regions", label: "Zones" },
  { key: "top", label: "Top copper" },
  { key: "bottom", label: "Bottom copper" },
  { key: "silk", label: "Silkscreen" },
  { key: "rf", label: "RF field" },
  { key: "heat", label: "Surface current" },
];
