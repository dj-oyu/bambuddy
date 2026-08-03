import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatusGrid } from '../../components/BMCULinkSettings';

/** A last_status payload as the bmcu-link route serves it. */
const status = (overrides: Record<string, unknown> = {}) => ({
  current_slot: 1,
  inserted_mask: 0b1111,
  online_mask: 0b0010,
  motion: [0, 2, 0, 0],
  pull_pct: 51,
  pressure: 1310,
  control_error: 0,
  age_s: 1.2,
  ...overrides,
});

const flags = (overrides: Record<string, number> = {}) => ({
  ks: 0,
  low: 0,
  jam: 0,
  dm_fail: 0,
  loaded: 0,
  tail: 0,
  ...overrides,
});

describe('StatusGrid latch row', () => {
  it('names the latches each channel is holding', () => {
    render(
      <StatusGrid
        status={status({
          channel_flags: [flags(), flags({ loaded: 1, ks: 1 }), flags({ jam: 1, low: 1 }), flags()],
        })}
        enums={undefined}
      />
    );

    expect(screen.getByText('LOADED')).toBeTruthy();
    expect(screen.getByText('JAM')).toBeTruthy();
    expect(screen.getByText('LOW')).toBeTruthy();
    // Nothing latched on channels 0 and 3.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('shows the runout pair together, because tail never stands alone', () => {
    render(
      <StatusGrid status={status({ channel_flags: [flags({ loaded: 1, tail: 1 }), flags(), flags(), flags()] })} enums={undefined} />
    );

    expect(screen.getByText('LOADED')).toBeTruthy();
    expect(screen.getByText('TAIL')).toBeTruthy();
  });

  it('omits the row entirely when the bridge reported no flags byte', () => {
    // A 27-byte STATUS predates the per-channel flags. Rendering four clear
    // channels would claim the bridge said something it never said.
    render(<StatusGrid status={status({ channel_flags: null })} enums={undefined} />);

    expect(screen.queryByText('LOADED')).toBeNull();
    expect(screen.queryByText('Latch')).toBeNull();
    // The channel grid itself is still there.
    expect(screen.getByText('Filament')).toBeTruthy();
  });

  it('keeps the latches out of the scalar tiles', () => {
    // Before the row existed the array fell through to the generic tile
    // renderer and printed as a wall of JSON.
    const { container } = render(
      <StatusGrid status={status({ channel_flags: [flags({ loaded: 1 }), flags(), flags(), flags()] })} enums={undefined} />
    );

    expect(container.textContent).not.toContain('"loaded"');
  });
});
