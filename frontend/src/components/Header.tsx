import { colors, spacing } from '../styles/theme';

const Header = () => {
  return (
    <header
      role="banner"
      style={{
        background: `linear-gradient(135deg, ${colors.primary} 0%, ${colors.secondary} 100%)`,
        color: colors.white,
        padding: `${spacing[5]} ${spacing[4]}`,
        boxShadow: '0 8px 24px rgba(0, 51, 102, 0.18)',
      }}
    >
      <a className="visually-hidden" href="#main-content">
        Ugrás a fő tartalomra
      </a>
      <div
        style={{
          width: 'min(100%, 72rem)',
          margin: '0 auto',
          display: 'flex',
          gap: spacing[4],
          alignItems: 'center',
        }}
      >
        <div
          aria-hidden="true"
          style={{
            width: '3rem',
            height: '3rem',
            borderRadius: '0.875rem',
            background: 'rgba(255, 255, 255, 0.14)',
            border: '1px solid rgba(255, 255, 255, 0.35)',
            display: 'grid',
            placeItems: 'center',
            fontWeight: 800,
            letterSpacing: '0.08em',
          }}
        >
          IA
        </div>
        <div>
          <p style={{ margin: 0, fontSize: '1.5rem', fontWeight: 800 }}>NAV Információs Asszisztens</p>
          <p style={{ margin: 0, opacity: 0.92 }}>2026-os adózási tájékoztató füzetek</p>
        </div>
      </div>
    </header>
  );
};

export default Header;
