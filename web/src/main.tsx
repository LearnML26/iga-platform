import React from "react";
import ReactDOM from "react-dom/client";
import { PublicClientApplication, EventType } from "@azure/msal-browser";
import { MsalProvider } from "@azure/msal-react";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { msalConfig } from "./authConfig";
import { registerMsal } from "./api/client";
import "./styles.css";

const pca = new PublicClientApplication(msalConfig);
registerMsal(pca);

// Keep the active account pinned after a redirect login completes.
pca.addEventCallback((event) => {
  if (event.eventType === EventType.LOGIN_SUCCESS && event.payload && "account" in event.payload) {
    pca.setActiveAccount((event.payload as { account: never }).account);
  }
});

pca.initialize().then(() => {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <MsalProvider instance={pca}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </MsalProvider>
    </React.StrictMode>,
  );
});
