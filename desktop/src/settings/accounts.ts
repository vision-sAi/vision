// Subscriptions: the brains Vision works through, and how much of each plan is used.

import { el } from "../render";
import { subscriptions } from "../setup";
import * as usage from "../usage";
import type { Page } from "./index";
import { closeSettings } from "./index";
import { section, setRow } from "./ui";

export const page: Page = {
  id: "accounts",
  label: "Subscriptions",
  icon: "Gauge",
  build(body, host) {
    const showUsage = el("button", "chip-btn bordered", "Show…");
    showUsage.onclick = () => usage.open();
    body.append(section("Usage", setRow("Subscription usage", "How much of each plan's session and week is used, and when they reset.", showUsage)));

    if (host.local()) {
      const manage = el("button", "chip-btn bordered", "Manage…");
      manage.onclick = () => { closeSettings(); subscriptions(false); };
      body.append(section("Subscriptions", setRow("Claude, ChatGPT, Grok", "Install and sign in to the subscriptions Vision works through.", manage)));
    }
  },
};
