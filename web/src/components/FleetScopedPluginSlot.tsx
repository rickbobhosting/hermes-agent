import type { ComponentProps } from "react";
import { useProfileScope } from "@/contexts/useProfileScope";
import { PluginSlot } from "@/plugins";

/** Plugin shell controls may be arbitrary components, so omit them for fleet targets. */
export function FleetScopedPluginSlot(props: ComponentProps<typeof PluginSlot>) {
  const { profile } = useProfileScope();
  return profile.startsWith("fleet:") ? null : <PluginSlot {...props} />;
}
