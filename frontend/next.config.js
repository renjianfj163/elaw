/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    const base = process.env.BACKEND_URL || "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${base}/api/:path*` }];
  },
};

module.exports = nextConfig;
