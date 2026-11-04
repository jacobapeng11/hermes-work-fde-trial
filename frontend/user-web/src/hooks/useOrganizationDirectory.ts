import { useCallback, useEffect, useState } from "react";
import type { OrganizationDirectoryNode } from "@hermes-cloud/schemas";
import { userApi } from "../api/client";

export function useOrganizationDirectory(enabled: boolean) {
  const [directory, setDirectory] = useState<OrganizationDirectoryNode[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDirectory(await userApi.organizationDirectoryTree());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "组织目录加载失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) void load();
    return () => {
      setDirectory([]);
      setError(null);
      setLoading(false);
    };
  }, [enabled, load]);

  return { directory, loading, error, retry: load };
}
