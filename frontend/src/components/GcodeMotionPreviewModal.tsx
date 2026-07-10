import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Play } from 'lucide-react';
import { Card } from './Card';
import { GcodeMotionPreview } from './GcodeMotionPreview';

/**
 * Preview button + fullscreen-ish modal for the G-code motion preview (#422).
 * Self-contained: renders the trigger button and, while open, the modal with
 * the animated bed-slinger/CoreXY scene. Hidden when the snippet is empty.
 */
export function GcodeMotionPreviewButton({
  model,
  gcode,
}: {
  model: string;
  gcode: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open]);

  if (!gcode.trim()) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded bg-bambu-dark-tertiary text-white hover:bg-bambu-green/20 hover:text-bambu-green transition-colors whitespace-nowrap"
      >
        <Play className="w-3 h-3" />
        {t('gcodeMotion.previewButton', 'Preview motion')}
      </button>
      {open && (
        <div
          className="fixed inset-0 bg-black/50 flex items-center justify-center p-4 z-50"
          onClick={() => setOpen(false)}
        >
          <Card
            className="w-full max-w-3xl"
            onClick={(e: React.MouseEvent) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
              <h3 className="text-base font-semibold text-white">
                {t('gcodeMotion.title', 'G-code Motion Preview')} — {model}
              </h3>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label={t('common.close', 'Close')}
                className="p-1 rounded text-bambu-gray hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4">
              <GcodeMotionPreview gcode={gcode} printerModel={model} />
            </div>
          </Card>
        </div>
      )}
    </>
  );
}
