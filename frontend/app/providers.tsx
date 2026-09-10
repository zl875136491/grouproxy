"use client";

import { keepPreviousData, MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useRef, useState, type PropsWithChildren } from "react";
import { notifyToast, ToastProvider, toastErrorMessage } from "../components/toast";
import { PreferencesProvider, usePreferences } from "../lib/preferences";

export function Providers({ children }: PropsWithChildren) {
  return <PreferencesProvider><LocalizedQueryClientProvider>{children}</LocalizedQueryClientProvider></PreferencesProvider>;
}

function LocalizedQueryClientProvider({ children }: PropsWithChildren) {
  const { t } = usePreferences();
  const translateRef = useRef(t);
  translateRef.current = t;
  const [queryClient] = useState(
    () =>
      new QueryClient({
        mutationCache: new MutationCache({
          onError: (error, _variables, _context, mutation) => {
            if (mutation.meta?.toast === false) return;
            notifyToast({
              title: translateRef.current("Operation failed"),
              description: translateRef.current(toastErrorMessage(error)),
              variant: "destructive",
            });
          },
          onSuccess: (_data, _variables, _context, mutation) => {
            if (mutation.meta?.toast === false) return;
            notifyToast({ title: translateRef.current("Operation completed"), variant: "success" });
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

  return <QueryClientProvider client={queryClient}><ToastProvider>{children}</ToastProvider></QueryClientProvider>;
}
