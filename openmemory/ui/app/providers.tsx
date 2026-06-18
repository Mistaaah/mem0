"use client";

import axios from "axios";
import { Provider } from "react-redux";
import { store } from "../store/store";

// Attach the API key to every request so the secured OpenMemory API
// (X-API-KEY guard on all endpoints) accepts browser calls.
const apiKey = process.env.NEXT_PUBLIC_API_KEY;
if (apiKey) {
  axios.defaults.headers.common["X-API-KEY"] = apiKey;
}

export function Providers({ children }: { children: React.ReactNode }) {
  return <Provider store={store}>{children}</Provider>;
}
