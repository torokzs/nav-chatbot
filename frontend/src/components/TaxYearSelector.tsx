import type { TaxYear } from '../types';
import { SUPPORTED_TAX_YEARS } from '../types';
import { colors, spacing } from '../styles/theme';

interface TaxYearSelectorProps {
  value: TaxYear | null;
  onChange: (year: TaxYear) => void;
}

const TaxYearSelector = ({ value, onChange }: TaxYearSelectorProps) => (
  <label
    style={{
      display: 'flex',
      alignItems: 'center',
      gap: spacing[2],
      color: colors.primary,
      fontWeight: 700,
    }}
  >
    Adóév
    <select
      aria-label="Adóév kiválasztása"
      onChange={(event) => onChange(Number(event.target.value) as TaxYear)}
      value={value ?? ''}
    >
      <option disabled value="">
        Válasszon
      </option>
      {SUPPORTED_TAX_YEARS.map((year) => (
        <option key={year} value={year}>
          {year}
        </option>
      ))}
    </select>
  </label>
);

export default TaxYearSelector;
