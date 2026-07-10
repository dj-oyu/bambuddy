import { describe, it, expect } from 'vitest';
import {
  parseGcodeMotion,
  motionBounds,
  totalDurationMs,
  DEFAULT_FEEDRATE,
  HOMING_FEEDRATE,
} from '../../utils/gcodeMotion';

describe('parseGcodeMotion', () => {
  it('parses a simple absolute XY move with duration = distance / feedrate', () => {
    // 30mm move at 3000 mm/min => 30/3000 min = 0.01 min = 600 ms
    const segs = parseGcodeMotion('G1 X30 F3000');
    expect(segs).toHaveLength(1);
    expect(segs[0].kind).toBe('move');
    expect(segs[0].from).toEqual({ x: 0, y: 0, z: 0 });
    expect(segs[0].to).toEqual({ x: 30, y: 0, z: 0 });
    expect(segs[0].durationMs).toBeCloseTo(600, 6);
  });

  it('uses the default feedrate when no F word seen', () => {
    // distance 3000 at default 3000 mm/min => 60000 ms
    const segs = parseGcodeMotion('G1 X3000');
    expect(DEFAULT_FEEDRATE).toBe(3000);
    expect(segs[0].durationMs).toBeCloseTo(60_000, 3);
  });

  it('persists feedrate across lines until changed', () => {
    const segs = parseGcodeMotion(['G1 X10 F600', 'G1 X20', 'G1 X40 F1200'].join('\n'));
    expect(segs).toHaveLength(3);
    // seg0: 10mm @ 600 => 1000ms
    expect(segs[0].durationMs).toBeCloseTo(1000, 3);
    // seg1: 10mm @ 600 (persisted) => 1000ms
    expect(segs[1].durationMs).toBeCloseTo(1000, 3);
    // seg2: 20mm @ 1200 => 1000ms
    expect(segs[2].durationMs).toBeCloseTo(1000, 3);
  });

  it('computes 3D straight-line distance for multi-axis moves', () => {
    // 3-4-5 triangle: X3 Y4 => distance 5
    const segs = parseGcodeMotion('G1 X3 Y4 F300');
    expect(segs[0].durationMs).toBeCloseTo((5 / 300) * 60_000, 6);
  });

  it('handles G91 relative mode per-axis and G90 back to absolute', () => {
    const segs = parseGcodeMotion(
      ['G90', 'G1 X10 Y10 F6000', 'G91', 'G1 X5', 'G1 Y-3', 'G90', 'G1 X0'].join('\n'),
    );
    // absolute move to (10,10)
    expect(segs[0].to).toEqual({ x: 10, y: 10, z: 0 });
    // relative +5 in X => (15,10)
    expect(segs[1].to).toEqual({ x: 15, y: 10, z: 0 });
    // relative -3 in Y => (15,7)
    expect(segs[2].to).toEqual({ x: 15, y: 7, z: 0 });
    // absolute X0 => (0,7)
    expect(segs[3].to).toEqual({ x: 0, y: 7, z: 0 });
  });

  it('emits dwell segments for G4 P (ms) and G4 S (sec), holding position', () => {
    const segs = parseGcodeMotion(['G1 X10 F6000', 'G4 P250', 'G4 S2'].join('\n'));
    expect(segs).toHaveLength(3);
    expect(segs[1].kind).toBe('dwell');
    expect(segs[1].durationMs).toBe(250);
    expect(segs[1].from).toEqual(segs[1].to);
    expect(segs[2].kind).toBe('dwell');
    expect(segs[2].durationMs).toBe(2000);
    expect(segs[2].to).toEqual({ x: 10, y: 0, z: 0 });
  });

  it('approximates G28 with no axes as a home of all axes to 0', () => {
    const segs = parseGcodeMotion(['G1 X10 Y20 Z5 F6000', 'G28'].join('\n'));
    expect(segs[1].kind).toBe('home');
    expect(segs[1].to).toEqual({ x: 0, y: 0, z: 0 });
    const dist = Math.sqrt(10 * 10 + 20 * 20 + 5 * 5);
    expect(segs[1].durationMs).toBeCloseTo((dist / HOMING_FEEDRATE) * 60_000, 6);
  });

  it('approximates G28 with named axes by homing only those axes', () => {
    const segs = parseGcodeMotion(['G1 X10 Y20 Z5 F6000', 'G28 X Y'].join('\n'));
    expect(segs[1].to).toEqual({ x: 0, y: 0, z: 5 });
  });

  it('ignores comments, blank lines, M-codes, and unknown G-codes', () => {
    const segs = parseGcodeMotion(
      ['; a comment', '', 'M104 S200', 'G92 E0', 'G1 X5 F600 ; move', 'M400'].join('\n'),
    );
    expect(segs).toHaveLength(1);
    expect(segs[0].to.x).toBe(5);
  });

  it('respects a provided startPosition', () => {
    const segs = parseGcodeMotion('G1 X10 F600', { x: 5, y: 5, z: 1 });
    expect(segs[0].from).toEqual({ x: 5, y: 5, z: 1 });
    expect(segs[0].to).toEqual({ x: 10, y: 5, z: 1 });
  });

  it('skips zero-distance moves (F-only lines)', () => {
    const segs = parseGcodeMotion(['G1 F1000', 'G1 X0'].join('\n'));
    expect(segs).toHaveLength(0);
  });
});

describe('motionBounds', () => {
  it('returns origin box for empty input', () => {
    expect(motionBounds([])).toEqual({
      min: { x: 0, y: 0, z: 0 },
      max: { x: 0, y: 0, z: 0 },
    });
  });

  it('computes min/max across all segment endpoints', () => {
    const segs = parseGcodeMotion(
      ['G1 X10 Y-5 F6000', 'G1 X-2 Y20 Z3', 'G1 X5 Y5 Z-1'].join('\n'),
    );
    const b = motionBounds(segs);
    expect(b.min).toEqual({ x: -2, y: -5, z: -1 });
    expect(b.max).toEqual({ x: 10, y: 20, z: 3 });
  });
});

describe('totalDurationMs', () => {
  it('sums segment durations including dwells', () => {
    const segs = parseGcodeMotion(['G1 X10 F600', 'G4 P500'].join('\n'));
    // move: 10/600*60000 = 1000ms, dwell 500ms
    expect(totalDurationMs(segs)).toBeCloseTo(1500, 3);
  });
});
