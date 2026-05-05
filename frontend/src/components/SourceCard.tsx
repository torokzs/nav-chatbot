import type { ChatSource } from '../types';
import { colors } from '../styles/theme';

interface SourceCardProps {
  source: ChatSource;
  index: number;
}

const SourceCard = ({ source, index }: SourceCardProps) => {
  const pageLabel =
    source.page_from === source.page_to
      ? `${source.page_from}. oldal`
      : `${source.page_from}-${source.page_to}. oldal`;

  const apiBase = import.meta.env.VITE_API_BASE_URL || '';
  const href = source.url
    ? `${apiBase}${source.url}`
    : `${apiBase}/api/documents/${source.fuzet_szam}/pdf`;

  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      title={source.breadcrumb}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        maxWidth: '100%',
        padding: '0.45rem 0.75rem',
        borderRadius: '999px',
        backgroundColor: colors.surfaceAlt,
        border: `1px solid ${colors.border}`,
        color: colors.primary,
        fontSize: '0.95rem',
        lineHeight: 1.4,
        textDecoration: 'none',
      }}
    >
      {`[${index}] ${source.fuzet_cim}, ${pageLabel}`}
    </a>
  );
};

export default SourceCard;
