// The window's icons: Lucide's, drawn at the size of the text around them.

import {
  Pin, PinOff,
  ArrowUp, Bell, Bot, Check, ChevronDown, ChevronRight, CircleAlert, Clock, Copy, FilePen, FileText, Folder, FolderSearch,
  Globe, Lightbulb, ListTodo, Monitor, Moon, PanelLeft, Paperclip, RotateCcw, Search, Settings, Sparkles, Square, SquarePen,
  Terminal, Trash2, Volume2, Wrench, X, Layers, Plus, Ellipsis, Activity, Pause, Play, Archive, Pencil, MessageSquare, createElement,
} from "lucide";

export const ICONS = {
  Pin, PinOff,
  ArrowUp, Bell, Bot, Check, ChevronDown, ChevronRight, CircleAlert, Clock, Copy, FilePen, FileText, Folder, FolderSearch,
  Globe, Layers, Lightbulb, ListTodo, Monitor, Moon, PanelLeft, Paperclip, RotateCcw, Search, Settings, Sparkles, Square, SquarePen,
  Terminal, Trash2, Volume2, Wrench, X, Plus, Ellipsis, Activity, Pause, Play, Archive, Pencil, MessageSquare,
};
export type IconName = keyof typeof ICONS;

export function icon(name: IconName, size = 16): SVGElement {
  const svg = createElement(ICONS[name]) as unknown as SVGElement;
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.classList.add("icon");
  return svg;
}

/** Fill every `data-icon` placeholder under `root` with its icon. */
export function placeIcons(root: ParentNode = document) {
  for (const node of root.querySelectorAll<HTMLElement>("[data-icon]")) {
    const name = node.dataset.icon as IconName;
    if (ICONS[name] && !node.querySelector("svg")) node.append(icon(name));
  }
}
