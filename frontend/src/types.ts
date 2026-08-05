export const SUPPORTED_TAX_YEARS = [2021, 2022, 2023, 2024, 2025, 2026] as const;
export type TaxYear = (typeof SUPPORTED_TAX_YEARS)[number];

export interface ChatSource {
  adoev: TaxYear;
  fuzet_szam: string;
  fuzet_cim: string;
  page_from: number;
  page_to: number;
  breadcrumb: string;
  url?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  adoev?: TaxYear;
  sources?: ChatSource[];
  isStreaming?: boolean;
}

export interface SSEEvent {
  type: 'token' | 'sources' | 'done' | 'error';
  content: any;
}
