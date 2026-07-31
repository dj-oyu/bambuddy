import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { BMCULinkSnapshot } from '../../api/bmcuMonitors';
import { SlotStateGrid } from '../../components/bmcu/SlotStateGrid';

const link = (linkIndex: number, linkId: string): BMCULinkSnapshot => ({
  linkIndex,
  linkId,
  state: 'online',
  currentSlot: null,
  activeMask: 0,
  motion: null,
  pullPercent: null,
  pressure: null,
  faultCount: 0,
  lastSeenAt: null,
});

describe('SlotStateGrid', () => {
  it('shows loader links in natural link-id order', () => {
    render(<SlotStateGrid links={[
      link(1, 'bmcu-b'),
      link(0, 'bmcu-a'),
    ]} />);

    const loaderLabels = screen
      .getAllByText((content) => content.startsWith('bmcu-'))
      .map((element) => element.textContent);

    expect(loaderLabels).toEqual(['bmcu-a', 'bmcu-b']);
  });
});
