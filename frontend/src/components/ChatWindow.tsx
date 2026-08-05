import { useEffect, useMemo, useRef } from 'react';
import type { ChatMessage, TaxYear } from '../types';
import { SUPPORTED_TAX_YEARS } from '../types';
import { colors, spacing } from '../styles/theme';
import MessageBubble from './MessageBubble';

interface ChatWindowProps {
  messages: ChatMessage[];
  isLoading?: boolean;
  awaitingTaxYear?: boolean;
  onSelectTaxYear?: (year: TaxYear) => void;
}

const suggestions = [
  'Mikor kell áfabevallást benyújtani egyéni vállalkozóként?',
  'Milyen költségek számolhatók el átalányadózás esetén?',
  'Hogyan kell bejelenteni az alkalmazottat a NAV felé?',
];

const ChatWindow = ({
  messages,
  isLoading = false,
  awaitingTaxYear = false,
  onSelectTaxYear,
}: ChatWindowProps) => {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const target = endRef.current;
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [messages, isLoading]);

  const containerStyles = useMemo(
    () => ({
      backgroundColor: colors.white,
      border: `1px solid ${colors.border}`,
      borderRadius: '1.5rem',
      boxShadow: '0 16px 40px rgba(0, 51, 102, 0.08)',
      minHeight: '26rem',
      maxHeight: 'calc(100vh - 19rem)',
      overflowY: 'auto' as const,
      padding: spacing[4],
      display: 'flex',
      flexDirection: 'column' as const,
      gap: spacing[4],
    }),
    [],
  );

  return (
    <section aria-label="Beszélgetés" style={containerStyles}>
      <div aria-atomic="false" aria-live="polite" aria-relevant="additions text" role="log" style={{ display: 'grid', gap: spacing[4] }}>
        {messages.length === 0 ? (
          <div
            style={{
              backgroundColor: colors.background,
              borderRadius: '1rem',
              padding: spacing[5],
              display: 'grid',
              gap: spacing[3],
            }}
          >
            <h2 style={{ margin: 0, color: colors.primary, fontSize: '1.25rem' }}>Kérdezzen a NAV tájékoztatók alapján</h2>
            <p style={{ margin: 0, color: colors.text }}>
              Az alábbi példák segítenek az indulásban. A válaszok forrásoldalakkal együtt jelennek meg.
            </p>
            <ul style={{ margin: 0, paddingLeft: '1.25rem', color: colors.text }}>
              {suggestions.map((suggestion) => (
                <li key={suggestion}>{suggestion}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}
        {awaitingTaxYear ? (
          <div role="group" aria-label="Adóév kiválasztása" style={{ display: 'grid', gap: spacing[2] }}>
            <strong>Melyik adóévre vonatkozik a kérdés?</strong>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: spacing[2] }}>
              {SUPPORTED_TAX_YEARS.map((year) => (
                <button key={year} type="button" onClick={() => onSelectTaxYear?.(year)}>
                  {year}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        <div ref={endRef} />
      </div>
    </section>
  );
};

export default ChatWindow;
