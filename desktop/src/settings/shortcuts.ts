// Shortcuts: (to be built).

import { el } from "../render";
import type { Page } from "./index";

export const page: Page = {
  id: "shortcuts",
  label: "Shortcuts",
  icon: "Keyboard",
  build(body) {
    body.append(el("p", "dim small", "Coming soon."));
  },
};
