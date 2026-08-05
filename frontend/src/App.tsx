import { useMemo } from 'react';
import ChatWindow from './components/ChatWindow';
import Footer from './components/Footer';
import Header from './components/Header';
import InputBar from './components/InputBar';
import TaxYearSelector from './components/TaxYearSelector';
import { useChat } from './hooks/useChat';
import { colors, spacing } from './styles/theme';

const App = () => {
  const {
    awaitingTaxYear,
    error,
    isLoading,
    messages,
    selectedTaxYear,
    selectTaxYear,
    sendMessage,
    setSelectedTaxYear,
  } = useChat();

  const appStyles = useMemo(
    () => ({
      minHeight: '100vh',
      display: 'grid',
      gridTemplateRows: 'auto 1fr auto',
      backgroundColor: colors.background,
      color: colors.text,
    }),
    [],
  );

  const mainStyles = useMemo(
    () => ({
      width: 'min(100%, 72rem)',
      margin: '0 auto',
      padding: `${spacing[5]} ${spacing[4]} ${spacing[6]}`,
      display: 'grid',
      gap: spacing[4],
      alignItems: 'stretch',
    }),
    [],
  );

  return (
    <div style={appStyles}>
      <Header />
      <main id="main-content" style={mainStyles}>
        <TaxYearSelector value={selectedTaxYear} onChange={setSelectedTaxYear} />
        <ChatWindow
          messages={messages}
          isLoading={isLoading}
          awaitingTaxYear={awaitingTaxYear}
          onSelectTaxYear={selectTaxYear}
        />
        <InputBar isLoading={isLoading} onSend={sendMessage} />
        {error ? (
          <p role="alert" style={{ color: colors.error, margin: 0 }}>
            {error}
          </p>
        ) : null}
      </main>
      <Footer />
    </div>
  );
};

export default App;
