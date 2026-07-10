/**
 * G-code motion parser + timeline builder.
 *
 * Parses a subset of G-code into a list of {@link MotionSegment}s that describe
 * the tool's path over time in gcode/bed-relative coordinates (mm). This is used
 * to drive a physical-motion preview (e.g. how far a bed-slinger bed swings out
 * during a plate-eject macro).
 *
 * Supported:
 *  - G0/G1 linear moves with any subset of X/Y/Z/E/F (E ignored).
 *  - F feedrate in mm/min, persists across lines, default 3000 mm/min.
 *  - G4 P<ms> / G4 S<sec> dwell (position held).
 *  - G90/G91 absolute/relative distance mode (relative applies per-axis).
 *  - G28 homing (approximation, see note below).
 *
 * Everything else (M-codes, comments starting with ';', blank lines, unknown
 * G-codes) is ignored silently.
 */

export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

export type MotionKind = 'move' | 'dwell' | 'home';

export interface MotionSegment {
  from: Vec3;
  to: Vec3;
  durationMs: number;
  kind: MotionKind;
}

/** Default feedrate (mm/min) used before any F word is seen. */
export const DEFAULT_FEEDRATE = 3000;

/**
 * Feedrate (mm/min) used to approximate G28 homing moves. G28 has no user
 * feedrate; real firmware homes at a fixed (usually slower) speed. This is a
 * rough stand-in purely so the animation has a plausible duration.
 */
export const HOMING_FEEDRATE = 1500;

function distance(a: Vec3, b: Vec3): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const dz = b.z - a.z;
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

/** Compute move duration (ms) from a distance (mm) and feedrate (mm/min). */
function moveDurationMs(dist: number, feedrateMmPerMin: number): number {
  if (feedrateMmPerMin <= 0) return 0;
  return (dist / feedrateMmPerMin) * 60_000;
}

/**
 * Parse a single G-code line into a { letter -> number } map of word params,
 * plus the leading command token (e.g. 'G1'). Returns null for lines with no
 * command (blank / comment-only).
 */
interface ParsedLine {
  command: string; // uppercase, e.g. 'G1', 'G28', 'M104'
  params: Map<string, number>;
}

function parseLine(rawLine: string): ParsedLine | null {
  // Strip comments: everything after ';'. Also strip Marlin-style '(...)'.
  let line = rawLine;
  const semi = line.indexOf(';');
  if (semi >= 0) line = line.slice(0, semi);
  line = line.replace(/\([^)]*\)/g, ' ').trim();
  if (!line) return null;

  const tokens = line.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return null;

  const command = tokens[0].toUpperCase();
  const params = new Map<string, number>();

  for (let i = 1; i < tokens.length; i++) {
    const tok = tokens[i];
    const letter = tok[0]?.toUpperCase();
    const value = Number(tok.slice(1));
    if (letter && Number.isFinite(value)) {
      params.set(letter, value);
    }
  }

  return { command, params };
}

/**
 * Parse G-code into physical motion segments.
 *
 * @param gcode Raw G-code text.
 * @param startPosition Optional initial tool position (defaults to origin).
 */
export function parseGcodeMotion(gcode: string, startPosition?: Vec3): MotionSegment[] {
  const segments: MotionSegment[] = [];

  let pos: Vec3 = {
    x: startPosition?.x ?? 0,
    y: startPosition?.y ?? 0,
    z: startPosition?.z ?? 0,
  };
  let feedrate = DEFAULT_FEEDRATE;
  let absolute = true; // G90 default

  const lines = gcode.split(/\r?\n/);

  for (const rawLine of lines) {
    const parsed = parseLine(rawLine);
    if (!parsed) continue;

    const { command, params } = parsed;

    switch (command) {
      case 'G0':
      case 'G1': {
        if (params.has('F')) {
          const f = params.get('F')!;
          if (f > 0) feedrate = f;
        }

        const next: Vec3 = { ...pos };
        const applyAxis = (axis: 'x' | 'y' | 'z', letter: string) => {
          if (!params.has(letter)) return;
          const v = params.get(letter)!;
          next[axis] = absolute ? v : pos[axis] + v;
        };
        applyAxis('x', 'X');
        applyAxis('y', 'Y');
        applyAxis('z', 'Z');

        const dist = distance(pos, next);
        // Emit a segment even for pure-F lines? No — no motion, skip.
        if (dist > 0) {
          segments.push({
            from: { ...pos },
            to: { ...next },
            durationMs: moveDurationMs(dist, feedrate),
            kind: 'move',
          });
          pos = next;
        }
        break;
      }

      case 'G4': {
        // Dwell: P is milliseconds, S is seconds. Position unchanged.
        let ms = 0;
        if (params.has('P')) ms += params.get('P')!;
        if (params.has('S')) ms += params.get('S')! * 1000;
        if (ms > 0) {
          segments.push({
            from: { ...pos },
            to: { ...pos },
            durationMs: ms,
            kind: 'dwell',
          });
        }
        break;
      }

      case 'G28': {
        // Approximation: move the named axes (or all axes if none named) to 0
        // at a fixed homing feedrate. Real homing seeks endstops per-axis with
        // firmware-specific sequencing/speeds; this is only for preview timing.
        const named = ['X', 'Y', 'Z'].filter((l) => params.has(l));
        const next: Vec3 = { ...pos };
        if (named.length === 0) {
          next.x = 0;
          next.y = 0;
          next.z = 0;
        } else {
          if (params.has('X')) next.x = 0;
          if (params.has('Y')) next.y = 0;
          if (params.has('Z')) next.z = 0;
        }
        const dist = distance(pos, next);
        if (dist > 0) {
          segments.push({
            from: { ...pos },
            to: { ...next },
            durationMs: moveDurationMs(dist, HOMING_FEEDRATE),
            kind: 'home',
          });
          pos = next;
        }
        break;
      }

      case 'G90':
        absolute = true;
        break;

      case 'G91':
        absolute = false;
        break;

      default:
        // Ignore M-codes and unknown G-codes silently.
        break;
    }
  }

  return segments;
}

/** Axis-aligned bounding box of all positions touched by the segments. */
export function motionBounds(segments: MotionSegment[]): { min: Vec3; max: Vec3 } {
  if (segments.length === 0) {
    const zero = { x: 0, y: 0, z: 0 };
    return { min: { ...zero }, max: { ...zero } };
  }

  const min: Vec3 = { x: Infinity, y: Infinity, z: Infinity };
  const max: Vec3 = { x: -Infinity, y: -Infinity, z: -Infinity };

  const expand = (p: Vec3) => {
    min.x = Math.min(min.x, p.x);
    min.y = Math.min(min.y, p.y);
    min.z = Math.min(min.z, p.z);
    max.x = Math.max(max.x, p.x);
    max.y = Math.max(max.y, p.y);
    max.z = Math.max(max.z, p.z);
  };

  for (const seg of segments) {
    expand(seg.from);
    expand(seg.to);
  }

  return { min, max };
}

/** Total timeline duration (ms) across all segments. */
export function totalDurationMs(segments: MotionSegment[]): number {
  let total = 0;
  for (const seg of segments) total += seg.durationMs;
  return total;
}
