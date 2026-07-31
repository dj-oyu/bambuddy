import { describe, expect, it } from 'vitest';
import { bmcuTimestamp, formatBMCUDateTime, formatBMCUTime } from '../../components/bmcu/bmcuTime';

describe('BMCU time formatting', () => {
  it('interprets naive backend timestamps as UTC', () => {
    expect(bmcuTimestamp('2026-07-31T12:34:56')).toBe(Date.parse('2026-07-31T12:34:56Z'));
  });

  it('formats timestamps in JST while using the requested locale', () => {
    expect(formatBMCUTime('2026-07-31T12:34:56', 'ja-JP')).toBe('21:34');
    expect(formatBMCUDateTime('2026-07-31T12:34:56', 'ja-JP')).toBe('2026/7/31 21:34:56');
  });
});
