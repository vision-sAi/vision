// Memory: (to be built).

import { el } from "../render";
import type { Page } from "./index";

export const page: Page = {
  id: "memory",
  label: "Memory",
  icon: "Brain",
  build(body) {
    body.append(el("p", "dim small", "Coming soon."));
  },
};
