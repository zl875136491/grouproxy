/** @type {import('next').NextConfig} */
const backendApiUrl = (process.env.GROUPROXY_BACKEND_API_URL || "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);
const nextConfig = {
  devIndicators: false,
  // The shared test console is reached through its DNS name instead of
  // localhost. Permit that origin so Next's development assets can reload
  // cleanly when the test frontend is restarted.
  allowedDevOrigins: ["test-proxy.1oa.com.cn"],
  output: "standalone",
  // Keep the dev compiler output separate from production builds. Running
  // `next build` while the local console is open must not invalidate its
  // module graph or stylesheet manifest.
  distDir:
    process.env.NEXT_DIST_DIR ||
    (process.env.NODE_ENV === "development" ? ".next-dev" : ".next"),
  poweredByHeader: false,
  async headers() {
    return [
      {
        // Apply security headers to all routes
        source: "/:path*",
        headers: [
          {
            key: "X-Frame-Options",
            value: "DENY",
          },
          {
            key: "X-Content-Type-Options",
            value: "nosniff",
          },
          {
            key: "X-XSS-Protection",
            value: "1; mode=block",
          },
          {
            key: "Referrer-Policy",
            value: "strict-origin-when-cross-origin",
          },
          {
            key: "Permissions-Policy",
            value: "geolocation=(), microphone=(), camera=()",
          },
          // Conservative CSP: allow inline scripts/styles for Next.js compatibility
          // Tighten after testing if app doesn't break
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'", // Next.js needs eval for dev
              "style-src 'self' 'unsafe-inline'", // Next.js uses inline styles
              "img-src 'self' data: blob:",
              "font-src 'self' data:",
              "connect-src 'self'",
              "frame-ancestors 'none'",
            ].join("; "),
          },
        ],
      },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/healthz",
        destination: `${backendApiUrl}/healthz`,
      },
      {
        source: "/readyz",
        destination: `${backendApiUrl}/readyz`,
      },
      {
        source: "/api/:path*",
        destination: `${backendApiUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
