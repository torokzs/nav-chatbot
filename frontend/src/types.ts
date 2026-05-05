export interface ChatSource {
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
  sources?: ChatSource[];
  isStreaming?: boolean;
}

export interface SSEEvent {
  type: 'token' | 'sources' | 'done' | 'error';
  content: any;
}
