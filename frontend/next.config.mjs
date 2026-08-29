/** @type {import('next').NextConfig} */
const backendApiUrl = (process.env.GROUPROXY_BACKEND_API_URL || "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);

const nextConfig = {
  devIndicators: false,
  output: "standalone",
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
