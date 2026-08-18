import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "maplibre-gl/dist/maplibre-gl.css";

import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("root_element_missing");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
