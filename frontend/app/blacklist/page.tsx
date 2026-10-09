"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { LoadingState } from "../../components/data-state";

export default function LegacyBlacklistPage() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/access-control");
  }, [router]);

  return <LoadingState rows={5} />;
}
