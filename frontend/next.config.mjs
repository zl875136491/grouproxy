/** @type {import('next').NextConfig} */
const backendApiUrl = (process.env.GROUPROXY_BACKEND_API_URL || "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);

const nextConfig = {
  devIndicators: false,
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
        source: "/backend-api/:path*",
        destination: `${backendApiUrl}/:path*`,
      },
    ];
  },
};

export default nextConfig;
