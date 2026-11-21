import { BookOpen, GraduationCap, Link as LinkIcon, Rocket, Sparkles } from "lucide-react";

/** Icons for configured shell links (config.shell.links[].icon), header and phone alike. */
export const LINK_ICONS: Record<string, typeof LinkIcon> = {
  "book-open": BookOpen,
  "graduation-cap": GraduationCap,
  rocket: Rocket,
  sparkles: Sparkles,
  link: LinkIcon,
};

export const linkIcon = (name: string) => LINK_ICONS[name] ?? LinkIcon;
