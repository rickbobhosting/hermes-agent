import { createContext } from "react";
import type { ProfileInfo } from "@/lib/api";

export interface ProfileContextValue {
  /** Profile every management surface reads/writes ("" = the dashboard
   *  process's own profile). */
  profile: string;
  /** The profile the dashboard process itself runs under. */
  currentProfile: string;
  /** Initial active-profile lookup has settled; chat may choose its scope. */
  ready: boolean;
  /** Known profile names (includes "default"). */
  profiles: string[];
  selectedTarget: ProfileInfo | undefined;
  targets: ProfileInfo[];
  /** Effective capability of the selected dashboard target. */
  access: "inspect" | "manage" | "execute";
  canManage: boolean;
  canExecute: boolean;
  setProfile: (name: string) => void;
}

export const ProfileContext = createContext<ProfileContextValue>({
  profile: "",
  currentProfile: "default",
  ready: false,
  profiles: [],
  selectedTarget: undefined,
  targets: [],
  access: "execute",
  canManage: true,
  canExecute: true,
  setProfile: () => {},
});
