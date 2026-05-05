import { useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { colors, spacing } from '../styles/theme';

interface InputBarProps {
  isLoading: boolean;
  onSend: (message: string) => Promise<void>;
}

const MAX_MESSAGE_LENGTH = 500;

const InputBar = ({ isLoading, onSend }: InputBarProps) => {
  const [value, setValue] = useState('');

  const canSubmit = value.trim().length > 0 && !isLoading;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }

    const nextValue = value;
    setValue('');
    await onSend(nextValue);
  };

  const formStyles = useMemo(
    () => ({
      display: 'grid',
      gap: spacing[3],
      backgroundColor: colors.white,
      borderRadius: '1.25rem',
      border: `1px solid ${colors.border}`,
      padding: spacing[4],
      boxShadow: '0 12px 30px rgba(0, 51, 102, 0.06)',
    }),
    [],
  );

  return (
    <form aria-label="Üzenetküldés" onSubmit={handleSubmit} style={formStyles}>
      <label className="visually-hidden" htmlFor="chat-input">
        Kérdés szövege
      </label>
      <textarea
        aria-label="Kérdezzen adózási témában"
        disabled={isLoading}
        id="chat-input"
        maxLength={MAX_MESSAGE_LENGTH}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            void handleSubmit(event as unknown as FormEvent<HTMLFormElement>);
          }
        }}
        placeholder="Kérdezzen adózási témában..."
        rows={3}
        style={{
          width: '100%',
          resize: 'vertical',
          minHeight: '3.5rem',
          borderRadius: '1rem',
          border: `1px solid ${colors.border}`,
          padding: spacing[3],
          color: colors.text,
        }}
        value={value}
      />
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: spacing[3],
          flexWrap: 'wrap',
        }}
      >
        <span aria-live="polite" style={{ color: colors.text, fontSize: '0.95rem' }}>
          {`${value.length}/${MAX_MESSAGE_LENGTH} karakter`}
        </span>
        <button
          aria-label="Üzenet küldése"
          disabled={!canSubmit}
          style={{
            border: 'none',
            borderRadius: '999px',
            padding: '0.8rem 1.2rem',
            backgroundColor: canSubmit ? colors.primary : colors.border,
            color: colors.white,
            fontWeight: 700,
          }}
          type="submit"
        >
          {isLoading ? 'Küldés folyamatban…' : 'Küldés →'}
        </button>
      </div>
    </form>
  );
};

export default InputBar;
