// SPDX-License-Identifier: MIT-0
import { documentTitle } from '@/lib/route-manifest';

/**
 * The exported `/404.html`'s document title, from its one home (`documentTitle`). A plain module, not
 * a client one, so the not-found route's metadata reads the string itself, not a client reference.
 */
export const notFoundTitle = documentTitle(null);
