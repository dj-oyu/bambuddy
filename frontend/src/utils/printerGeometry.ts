/**
 * Per-model physical printer geometry table.
 *
 * Used by the motion preview to render an accurate-ish physical scene: build
 * volume, kinematics style (which axes physically move which body), and the
 * physical bed plate footprint.
 *
 * NOTE: bed plate physical sizes below are approximations (the plate is larger
 * than the printable build area). Refine from real-machine measurement.
 */

import type { Vec3 } from './gcodeMotion';

export type Kinematics = 'bedslinger' | 'corexy';

export interface PrinterGeometry {
  buildVolume: Vec3;
  kinematics: Kinematics;
  /** Physical bed plate dimensions (mm). Larger than the printable area. */
  bedSize: { x: number; y: number };
  /** False when this is the generic fallback (model not recognised). */
  known: boolean;
}

interface GeometryEntry {
  /** Lowercase match keys, checked as substrings. Order-sensitive: see below. */
  aliases: string[];
  geometry: Omit<PrinterGeometry, 'known'>;
}

/**
 * Entries are evaluated in order, so more specific models must precede prefixes
 * they collide with. In particular 'A1 mini' must come BEFORE 'A1' so that an
 * "A1 mini" string isn't captured by the bare "A1" entry.
 */
const TABLE: GeometryEntry[] = [
  {
    aliases: ['a1 mini', 'a1mini', 'a1m'],
    geometry: {
      buildVolume: { x: 180, y: 180, z: 180 },
      kinematics: 'bedslinger',
      bedSize: { x: 184, y: 184 },
    },
  },
  {
    aliases: ['a1'],
    geometry: {
      buildVolume: { x: 256, y: 256, z: 256 },
      kinematics: 'bedslinger',
      bedSize: { x: 260, y: 260 },
    },
  },
  {
    aliases: ['p1p', 'p1s', 'x1c', 'x1e', 'x1 carbon', 'x1'],
    geometry: {
      buildVolume: { x: 256, y: 256, z: 256 },
      kinematics: 'corexy',
      bedSize: { x: 260, y: 260 },
    },
  },
  {
    aliases: ['h2d', 'h2 d'],
    geometry: {
      buildVolume: { x: 325, y: 320, z: 325 },
      kinematics: 'corexy',
      bedSize: { x: 330, y: 325 },
    },
  },
];

/** Generic fallback used when the model string isn't recognised. */
const FALLBACK: Omit<PrinterGeometry, 'known'> = {
  buildVolume: { x: 256, y: 256, z: 256 },
  kinematics: 'corexy',
  bedSize: { x: 260, y: 260 },
};

/**
 * Look up physical geometry for a printer model with case-insensitive fuzzy
 * (substring) matching. Returns a generic corexy fallback with `known: false`
 * when nothing matches.
 */
export function getPrinterGeometry(model: string | null | undefined): PrinterGeometry {
  const needle = (model ?? '').toLowerCase().trim();

  if (needle) {
    for (const entry of TABLE) {
      if (entry.aliases.some((alias) => needle.includes(alias))) {
        return { ...entry.geometry, known: true };
      }
    }
  }

  return { ...FALLBACK, known: false };
}
