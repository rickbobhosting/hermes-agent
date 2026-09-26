import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useSearchParams } from "react-router";
import { api, setManagementProfile, type ProfileInfo } from "@/lib/api";
import { ProfileContext } from "@/contexts/profile-context";

const isFleetSelector = (name: string) => name.startsWith("fleet:");

/**
 * Machine-level management-profile scope.
 *
 * One switcher (rendered in the sidebar) decides which profile every
 * management page reads/writes. React STATE is the source of truth; the
 * URL (`?profile=<name>`) is a synchronized projection of it so deep links
 * land scoped and refresh survives. The selection is mirrored into the api
 * module so `fetchJSON` transparently appends it to the profile-scoped
 * endpoint families. "" = the dashboard's own profile.
 *
 * Why state-first instead of URL-first: sidebar nav links are bare paths
 * (`/config`, `/skills`). A URL-derived scope would silently reset to the
 * dashboard's own profile on every nav click — the switcher would LOOK
 * global while normal navigation dropped the write target. With state as
 * truth, the effect below re-asserts `?profile=` onto the new location
 * after each navigation, so the scope survives nav and stays deep-linkable.
 *
 * This exists because "Set as active" on the Profiles page historically only
 * flipped the sticky active_profile file (future CLI/gateway runs). The
 * switcher is the dashboard's write-target selector for Chat and management
 * pages. We now sync the switcher when the sticky active profile differs from
 * the dashboard process on load, and ProfilesPage updates the switcher when
 * you click "Set as active".
 */
export function ProfileProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const { pathname } = useLocation();
  const [profiles, setProfiles] = useState<string[]>([]);
  const [targets, setTargets] = useState<ProfileInfo[]>([]);
  const [currentProfile, setCurrentProfile] = useState("default");
  const [ready, setReady] = useState(false);
  const selectionVersion = useRef(0);

  // Initial value comes from the URL (deep link / refresh / unified-launch
  // preselect); afterwards state leads and the URL follows.
  const [profile, setProfileState] = useState(
    () => {
      const requested = searchParams.get("profile") ?? "";
      return requested;
    },
  );

  // Mirror into the api module synchronously on every render where it
  // changed, so fetches fired by child effects in the same commit see it.
  setManagementProfile(profile);

  // A profile param arriving via in-app navigation (e.g. the Profiles
  // page's "Manage skills & tools" linking to /skills?profile=X) must win
  // over current state — it's an explicit scope request.
  const urlProfile = searchParams.get("profile");
  useEffect(() => {
    if (urlProfile !== null && urlProfile !== profile) {
      selectionVersion.current += 1;
      setManagementProfile(urlProfile);
      setProfileState(urlProfile);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlProfile]);

  // Re-assert ?profile= after navigations that dropped it (bare nav links).
  // Runs on every pathname/profile change; no-ops when already in sync.
  useEffect(() => {
    const inUrl = searchParams.get("profile") ?? "";
    if ((profile || "") === inUrl) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (profile) next.set("profile", profile);
        else next.delete("profile");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname, profile]);

  useEffect(() => {
    let cancelled = false;
    const urlProfile = searchParams.get("profile");
    const initialSelectionVersion = selectionVersion.current;

    Promise.all([api.getProfiles(), api.getActiveProfile()])
      .then(([profilesRes, info]) => {
        if (cancelled) return;

        setTargets(profilesRes.profiles);
        setProfiles(profilesRes.profiles.map((p) => p.name));

        const current = info.current || "default";
        const active = info.active || "default";
        setCurrentProfile(current);

        // Deep links (?profile=) win. Otherwise align the switcher with the
        // sticky active profile so Chat and management pages match what the
        // Profiles page shows as "active" (machine dashboard runs as
        // `current`, usually default).
        if (urlProfile === null && selectionVersion.current === initialSelectionVersion && active !== current && !isFleetSelector(active)) {
          setManagementProfile(active);
          setProfileState(active);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setReady(true);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setProfile = useCallback(
    (name: string) => {
      if (isFleetSelector(name) && !targets.some((target) => target.name === name && target.external)) return;
      selectionVersion.current += 1;
      setManagementProfile(name);
      setProfileState(name);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (name) next.set("profile", name);
          else next.delete("profile");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams, targets],
  );

  const selectedTarget = targets.find((target) => target.name === profile);
  // The merged backend exposes external homes for inspection only. Keep the
  // effective UI capability fail-closed even if a future/older record advertises
  // a stronger value without a matching execute or manage route here.
  const access = isFleetSelector(profile) ? "inspect" as const : "execute" as const;
  const canManage = access !== "inspect";
  const canExecute = access === "execute";
  const value = useMemo(
    () => ({ profile, currentProfile, ready, profiles, targets, selectedTarget, access, canManage, canExecute, setProfile }),
    [profile, currentProfile, ready, profiles, targets, selectedTarget, access, canManage, canExecute, setProfile],
  );

  return (
    <ProfileContext.Provider value={value}>{children}</ProfileContext.Provider>
  );
}
