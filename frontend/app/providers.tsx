"use client";

import { keepPreviousData, MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type PropsWithChildren } from "react";
import { notifyToast, ToastProvider, toastErrorMessage } from "../components/toast";
import { PreferencesProvider } from "../lib/preferences";

export function Providers({ children }: PropsWithChildren) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        mutationCache: new MutationCache({
          onError: (error, _variables, _context, mutation) => {
            if (mutation.meta?.toast === false) return;
            notifyToast({
              title: "Operation failed",
              description: toastErrorMessage(error),
              variant: "destructive",
            });
          },
          onSuccess: (_data, _variables, _context, mutation) => {
            if (mutation.meta?.toast === false) return;
            notifyToast({ title: "Operation completed", variant: "success" });
          },
        }),
        defaultOptions: {
          queries: {
            retry: 1,
            refetchOnWindowFocus: false,
            staleTime: 5_000,
            placeholderData: keepPreviousData,
          },
        },
      }),
  );

  return <QueryClientProvider client={queryClient}><PreferencesProvider><ToastProvider>{children}</ToastProvider></PreferencesProvider></QueryClientProvider>;
}
