// Permissions: (to be built).

import { el } from "../render";
import type { Page } from "./index";

export const page: Page = {
  id: "permissions",
  label: "Permissions",
  icon: "ShieldCheck",
  build(body) {
    body.append(el("p", "dim small", "Coming soon."));
  },
};
