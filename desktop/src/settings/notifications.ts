// Notifications: (to be built).

import { el } from "../render";
import type { Page } from "./index";

export const page: Page = {
  id: "notifications",
  label: "Notifications",
  icon: "Bell",
  build(body) {
    body.append(el("p", "dim small", "Coming soon."));
  },
};
