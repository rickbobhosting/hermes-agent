import { createContext } from "react";

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
  setProfile: (name: string) => void;
}

export const ProfileContext = createContext<ProfileContextValue>({
  profile: "",
  currentProfile: "default",
  ready: false,
  profiles: [],
  setProfile: () => {},
});
