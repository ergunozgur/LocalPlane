import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import { PreferencesProvider } from './preferences/preferences';
import { ViewerProvider } from './identity/viewer';
import './styles/tokens.css';
import './styles/base.css';

const container = document.getElementById('root');
if (!container) throw new Error('#root is missing from the document');

createRoot(container).render(
  <StrictMode>
    <ViewerProvider>
      <PreferencesProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </PreferencesProvider>
    </ViewerProvider>
  </StrictMode>,
);
