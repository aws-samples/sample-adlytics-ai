// SPDX-License-Identifier: MIT-0
/**
 * Static export only. ADR-008: served from S3 behind CloudFront with OAC.
 *
 * No SSR, no route handlers, no server-side secrets. Anything the app needs at
 * runtime comes from the API; anything it needs at build time is injected from
 * CDK outputs via SSM.
 *
 * @type {import('next').NextConfig}
 */
const nextConfig = {
  output: 'export',

  // S3 + CloudFront serves directories more predictably than extensionless keys.
  trailingSlash: true,

  // next/image optimisation requires a server. There is none.
  images: { unoptimized: true },

  // Fail the build on a type error rather than shipping it.
  typescript: { ignoreBuildErrors: false },

  // Security headers are NOT set here — a static export cannot emit them.
  // They come from the CloudFront response-headers policy in FrontendStack,
  // which is precisely why Amplify Hosting was rejected in ADR-008: CSP is
  // load-bearing for the cookie auth model and Amplify does not expose it.
};

export default nextConfig;
