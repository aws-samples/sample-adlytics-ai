// SPDX-License-Identifier: MIT-0
/**
 * Where the API lives, from the browser's point of view.
 *
 * There is deliberately no build-time backend configuration here — no API base
 * URL, no pool ID, no host of any kind. CloudFront serves this app and the API
 * from one distribution, so every call is same-origin and a relative path is
 * always correct.
 *
 * That is a stronger guarantee than validating an injected URL would be. The
 * localhost defect this project has hit repeatedly is only possible when a
 * hostname has to be supplied; here there is nothing to supply and nothing to get
 * wrong. A static export cannot bake in the wrong origin if it never names one.
 *
 * The moment an absolute URL appears in this file, that guarantee is gone and the
 * validation rules in the frontend steering apply again.
 */

/**
 * Matches API_PREFIX in the Python routing table and API_PATH_PATTERN in the CDK
 * config. All three must agree; the CloudFront behaviour is what routes it.
 */
export const API_PREFIX = '/api';

/**
 * Where a login begins.
 *
 * Our own endpoint, never a provider URL assembled here. The app does not know
 * that login currently happens to be a redirect to Cognito managed login, which is
 * what keeps replacing it an server-side change — see ADR-007.
 */
export const LOGIN_PATH = `${API_PREFIX}/auth/login`;

export const SESSION_PATH = `${API_PREFIX}/me`;
export const REFRESH_PATH = `${API_PREFIX}/auth/refresh`;
export const LOGOUT_PATH = `${API_PREFIX}/auth/logout`;
export const CAMPAIGN_LIST_PATH = `${API_PREFIX}/presentation/campaigns`;
export const CAMPAIGN_DETAIL_PATH = `${API_PREFIX}/presentation/campaign-detail`;
export const CAMPAIGN_METRIC_FOCUS_PATH = `${API_PREFIX}/presentation/campaign-metric-focus`;
export const PORTFOLIO_METRIC_FOCUS_PATH = `${API_PREFIX}/presentation/portfolio-metric-focus`;
export const RETAIL_METRIC_FOCUS_PATH = `${API_PREFIX}/presentation/retail-metric-focus`;
export const INSIGHT_DEEP_DIVE_PATH = `${API_PREFIX}/presentation/insight-deep-dive`;
export const RETAIL_HEALTH_PATH = `${API_PREFIX}/presentation/retail-health`;
export const SCORECARD_PATH = `${API_PREFIX}/presentation/scorecard`;
export const NEW_CUSTOMERS_PATH = `${API_PREFIX}/presentation/new-customers`;
export const REACH_PATH = `${API_PREFIX}/presentation/reach`;
export const RETENTION_PATH = `${API_PREFIX}/presentation/retention`;
export const AUDIENCES_PATH = `${API_PREFIX}/presentation/audiences`;
/** Retail health › product detail (ADR-125), named apart from the retired retail-product id. */
export const RETAIL_HEALTH_PRODUCT_PATH = `${API_PREFIX}/presentation/retail-health-product`;
export const CHAT_PATH = `${API_PREFIX}/chat`;
