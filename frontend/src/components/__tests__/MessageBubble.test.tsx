import { render, screen } from '@testing-library/react';
import MessageBubble from '../MessageBubble';

describe('MessageBubble', () => {
  it('renders user message content', () => {
    render(<MessageBubble message={{ id: 'user-1', role: 'user', content: 'Teszt kérdés' }} />);

    expect(screen.getByText('Teszt kérdés')).toBeInTheDocument();
  });

  it('renders assistant message with clickable sources limited to three', () => {
    render(
      <MessageBubble
        message={{
          id: 'assistant-1',
          role: 'assistant',
          adoev: 2024,
          content: '**Fontos:**\n- első pont',
          sources: [
            {
              adoev: 2024,
              fuzet_szam: '12',
              fuzet_cim: 'Általános forgalmi adó',
              page_from: 3,
              page_to: 5,
              breadcrumb: 'ÁFA > Bevallás',
              url: '/api/documents/2024/12/pdf',
            },
            {
              adoev: 2024,
              fuzet_szam: '7',
              fuzet_cim: 'Adózás rendje',
              page_from: 10,
              page_to: 10,
              breadcrumb: 'Eljárás',
            },
            {
              adoev: 2024,
              fuzet_szam: '55',
              fuzet_cim: 'Tao-felajánlás',
              page_from: 2,
              page_to: 2,
              breadcrumb: 'Túlfizetés',
            },
            {
              adoev: 2024,
              fuzet_szam: '99',
              fuzet_cim: 'Negyedik forrás',
              page_from: 1,
              page_to: 1,
              breadcrumb: 'Rejtett',
            },
          ],
        }}
      />,
    );

    expect(screen.getByText('Fontos:')).toBeInTheDocument();
    expect(screen.getByText('első pont')).toBeInTheDocument();

    const primarySource = screen.getByRole('link', {
      name: '[1] 2024 · Általános forgalmi adó, 3-5. oldal',
    });
    expect(primarySource).toHaveAttribute('href', '/api/documents/2024/12/pdf');
    expect(screen.getByLabelText('2024-es adóév')).toBeInTheDocument();
    expect(screen.getAllByRole('link')).toHaveLength(3);
    expect(screen.queryByRole('link', { name: '[4] 2024 · Negyedik forrás, 1. oldal' })).not.toBeInTheDocument();
  });

  it('renders streaming state', () => {
    render(
      <MessageBubble
        message={{ id: 'assistant-2', role: 'assistant', content: 'Válasz készül', isStreaming: true }}
      />,
    );

    expect(screen.getByLabelText('A válasz készül')).toBeInTheDocument();
  });
});
