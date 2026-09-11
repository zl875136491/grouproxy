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
