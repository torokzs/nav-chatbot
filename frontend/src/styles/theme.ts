export const colors = {
  primary: '#003366',
  secondary: '#0066CC',
  white: '#FFFFFF',
  background: '#F5F5F5',
  text: '#1A1A1A',
  border: '#C7D3E0',
  mutedText: '#4A5565',
  surface: '#FFFFFF',
  surfaceAlt: '#EAF2FB',
  focus: '#FFBF47',
  error: '#B42318',
} as const;

export const fonts = {
  body: "'Inter', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  heading: "'Inter', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  mono: "'Cascadia Code', 'Fira Code', monospace",
} as const;

export const spacing = {
  1: '0.25rem',
  2: '0.5rem',
  3: '0.75rem',
  4: '1rem',
  5: '1.5rem',
  6: '2rem',
  8: '3rem',
} as const;

export const breakpoints = {
  sm: '30rem',
  md: '48rem',
  lg: '64rem',
} as const;
