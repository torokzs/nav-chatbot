import { colors, spacing } from '../styles/theme';

const Footer = () => {
  return (
    <footer
      style={{
        backgroundColor: colors.primary,
        color: colors.white,
        padding: `${spacing[4]} ${spacing[4]} ${spacing[5]}`,
      }}
    >
      <div
        style={{
          width: 'min(100%, 72rem)',
          margin: '0 auto',
          display: 'flex',
          flexWrap: 'wrap',
          gap: spacing[3],
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <p style={{ margin: 0 }}>Ez nem hivatalos NAV szolgáltatás. A válaszok tájékoztató jellegűek.</p>
        <a
          href="https://www.w3.org/WAI/"
          target="_blank"
          rel="noreferrer"
          style={{ textDecoration: 'underline', textUnderlineOffset: '0.2rem' }}
        >
          Akadálymentességi tudnivalók
        </a>
      </div>
    </footer>
  );
};

export default Footer;
