import React from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-mono/400.css';
import '@fontsource/noto-sans-devanagari/400.css';
import '@fontsource/noto-sans-devanagari/600.css';
import './styles/tokens.css';
import './styles/app.css';
import { App } from './components/App';

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
