// G-code template presets for the Settings > G-code Injection card (#422).
// Users can insert a known-good snippet into a printer model's start/end
// G-code field instead of hand-typing it.

export interface GcodeTemplate {
  id: string;
  name: string;
  /** Printer model names this template is safe for, e.g. ['A1 mini']. Matched
   * case-insensitively / by substring against the configured model string. */
  appliesTo: string[];
  field: 'start_gcode' | 'end_gcode';
  gcode: string;
  description: string;
}

export const GCODE_TEMPLATES: GcodeTemplate[] = [
  {
    id: 'chitu-platecycler-c1m-eject',
    name: 'Chitu PlateCycler C1M plate eject',
    appliesTo: ['A1 mini'],
    field: 'end_gcode',
    description:
      'Plate-eject sequence for the Chitu PlateCycler C1M automated plate changer (A1 mini only). Sourced from OrcaSlicer PR #13177.',
    gcode: `;Chitu PlateCycler C1M plate change (A1 mini only)
;second four notes of Beethoven's 5th to announce
;music_long: 0.6
M17
M400 S1
M1006 S1
M1006 L70 M70 N99
M1006 A42 B20 L66 C54 D20 M69
M1006 A42 B20 L66 C54 D20 M69
M1006 A42 B20 L66 C54 D20 M69
M1006 A39 B90 L62 C51 D90 M56
M1006 W
M18
G0 X-10 F5000;
G0 Z175;
G0 Y-5 F2000;
G0 Y186.5 F2000;
G0 Y182 F10000;
G0 Z186;
G0 X180 F5000;
G0 Y120 F500;
G0 Y-4 Z175 X-15 F3000;
G0 Y145;
G0 Y115 F1000;
G0 Y25 F500;
G0 Y85 F1000;
G0 Y180 F1000;
G0 X-10 F5000;
G4 P500; wait
G0 Y186.5 F200;
G4 P500; wait
G0 Y3 F3000;
G0 Y-5 F200;
G4 P500; wait
G0 Y10 F1000;
G0 Z100 Y186 F2000;
G0 Y150;
G4 P1000; wait;`,
  },
];

/** Returns templates that apply to a given printer model string, matching
 * case-insensitively and by substring (model strings can vary, e.g. "A1 mini"
 * vs "A1M"). */
export function templatesForModel(model: string, field: 'start_gcode' | 'end_gcode'): GcodeTemplate[] {
  const normalizedModel = model.trim().toLowerCase();
  return GCODE_TEMPLATES.filter((tpl) => {
    if (tpl.field !== field) return false;
    return tpl.appliesTo.some((applies) => {
      const normalizedApplies = applies.trim().toLowerCase();
      return normalizedModel.includes(normalizedApplies) || normalizedApplies.includes(normalizedModel);
    });
  });
}
