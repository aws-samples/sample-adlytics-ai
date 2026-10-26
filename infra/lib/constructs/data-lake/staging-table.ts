// SPDX-License-Identifier: MIT-0
/**
 * ADR-149: the generated all-`string` external tables a staged table unit is read through.
 *
 * One table per distinct physical table name across the source-aligned and canonical areas: a
 * candidate and a current table share a name and columns, so 54 staging tables serve 79
 * physical tables. Each reads the JSON Lines object `s3://<staging bucket>/<table>/<key>/rows.jsonl`
 * through an injected partition projection on `staging_key`, so no runtime DDL or partition API
 * call is ever needed. Every value is a JSON string or null and every column is `string`; the
 * MERGE casts each one exactly as the deleted parameter renderer cast the same text.
 *
 * L1 `glue.CfnTable` (ADR-016), as `iceberg-table.ts` does. The SerDe and table properties are the
 * ones probe P-1 proved on Athena ("literal key + OpenX succeed").
 */
import * as glue from 'aws-cdk-lib/aws-glue';

import type { GeneratedTable, GeneratedV2Storage } from './generated-tables';

/** The two names a staging table adds; no catalog column may use either (ADR-149 §4.2). */
export const STAGING_RESERVED_COLUMNS = ['stg_ordinal', 'staging_key'] as const;

/**
 * Every staging table name, sorted: the distinct source-aligned and canonical table names of
 * `contracts/generated/v2-tables.json`. The IAM `s3:prefix` conditions are built from this list,
 * and `stagingTables` refuses at synth a contract whose names differ, so the tables and the
 * grants cannot drift apart (the canonical allowlist's pattern, `canonical-dataset-allowlist.test.ts`).
 */
export const STAGING_TABLE_NAMES = [
  'dim_audience_segment',
  'dim_campaign',
  'dim_product',
  'fct_ad_exposure',
  'fct_ad_exposure_withheld',
  'fct_attributed_conversion',
  'fct_attributed_conversion_withheld',
  'fct_deduplicated_temporal_attribution',
  'fct_deduplicated_temporal_attribution_withheld',
  'fct_exposure_frequency',
  'fct_exposure_frequency_withheld',
  'fct_exposure_group_outcome',
  'fct_exposure_group_outcome_withheld',
  'fct_new_to_brand_buyers',
  'fct_product_page_views',
  'fct_product_page_views_reported_total',
  'fct_retail_purchase_amc',
  'fct_retail_purchase_amc_withheld',
  'fct_retail_sales',
  'fct_retail_sales_reported_total',
  'fct_segment_delivery',
  'fct_segment_delivery_withheld',
  'fct_segment_profile',
  'fct_shopper_acquisition',
  'fct_shopper_acquisition_withheld',
  'sa_amc_acquisition_cohorts',
  'sa_amc_acquisition_cohorts_arp',
  'sa_amc_attributed_conversion',
  'sa_amc_audience_performance',
  'sa_amc_campaign_dsp',
  'sa_amc_campaign_sponsored',
  'sa_amc_deduplicated_temporal_attribution',
  'sa_amc_dsp_clicks',
  'sa_amc_dsp_impressions',
  'sa_amc_dsp_reach',
  'sa_amc_dsp_segment_names',
  'sa_amc_dsp_views',
  'sa_amc_exposure_frequency_channel',
  'sa_amc_exposure_frequency_platform',
  'sa_amc_exposure_groups',
  'sa_amc_new_to_brand_buyers_campaign',
  'sa_amc_new_to_brand_buyers_channel',
  'sa_amc_new_to_brand_buyers_platform',
  'sa_amc_reach_frequency',
  'sa_amc_retail_purchases',
  'sa_amc_segment_buyer_profile_dsp_exposed',
  'sa_amc_segment_buyer_profile_inmarket',
  'sa_amc_segment_buyer_profile_lifestyle',
  'sa_amc_segment_metadata',
  'sa_vendor_product_hierarchy',
  'sa_vendor_sales_by_asin',
  'sa_vendor_sales_reported_total',
  'sa_vendor_traffic_by_asin',
  'sa_vendor_traffic_reported_total',
] as const;

/** The JSON SerDe P-1 selected; a malformed line fails the read rather than vanishing. */
export const STAGING_SERDE = 'org.openx.data.jsonserde.JsonSerDe';

/** A generated catalog that cannot be staged. */
export class StagingSchemaError extends Error {}

/**
 * The distinct tables to stage, in {@link STAGING_TABLE_NAMES} order.
 *
 * Refuses a candidate/current pair whose columns differ (one staging table serves both), a
 * catalog column using a reserved name, and a name set that differs from the IAM prefix list.
 */
export function stagingTables(generated: GeneratedV2Storage): readonly GeneratedTable[] {
  const problems: string[] = [];
  const byName = new Map<string, GeneratedTable>();
  for (const table of generated.sourceAlignedTables) byName.set(table.name, table);
  const current = new Map(generated.canonicalCurrentTables.map((table) => [table.name, table]));
  for (const candidate of generated.canonicalCandidateTables) {
    const twin = current.get(candidate.name);
    if (twin === undefined || JSON.stringify(twin.columns) !== JSON.stringify(candidate.columns)) {
      problems.push(`${candidate.name}: candidate and current columns differ`);
    }
    if (byName.has(candidate.name)) problems.push(`${candidate.name}: names two staging tables`);
    byName.set(candidate.name, candidate);
  }
  for (const table of byName.values()) {
    for (const column of table.columns) {
      if ((STAGING_RESERVED_COLUMNS as readonly string[]).includes(column.name)) {
        problems.push(`${table.name}.${column.name}: a reserved staging column name`);
      }
    }
  }
  const names = [...byName.keys()].sort();
  if (JSON.stringify(names) !== JSON.stringify([...STAGING_TABLE_NAMES])) {
    problems.push('the staging table names differ from STAGING_TABLE_NAMES; update the list');
  }
  if (problems.length > 0) {
    throw new StagingSchemaError(
      `contracts/generated/v2-tables.json cannot be staged:\n${problems.map((p) => `  - ${p}`).join('\n')}`,
    );
  }
  return STAGING_TABLE_NAMES.map((name) => byName.get(name) as GeneratedTable);
}

/** The `CfnTable` props for one staging table: external JSON, all `string`, injected key. */
export function stagingCfnTableProps(options: {
  readonly table: GeneratedTable;
  readonly catalogId: string;
  readonly databaseName: string;
  readonly bucketName: string;
}): glue.CfnTableProps {
  const { table, catalogId, databaseName, bucketName } = options;
  const location = `s3://${bucketName}/${table.name}/`;
  return {
    catalogId,
    databaseName,
    tableInput: {
      name: table.name,
      description: `ADR-149 staging for ${table.name}. GENERATED from v2-tables.json.`,
      tableType: 'EXTERNAL_TABLE',
      partitionKeys: [{ name: 'staging_key', type: 'string' }],
      parameters: {
        classification: 'json',
        'projection.enabled': 'true',
        'projection.staging_key.type': 'injected',
        'storage.location.template': `${location}\${staging_key}/`,
      },
      storageDescriptor: {
        columns: [
          ...table.columns.map((column) => ({ name: column.name, type: 'string' })),
          { name: 'stg_ordinal', type: 'string' },
        ],
        location,
        inputFormat: 'org.apache.hadoop.mapred.TextInputFormat',
        outputFormat: 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
        serdeInfo: {
          serializationLibrary: STAGING_SERDE,
          parameters: { 'ignore.malformed.json': 'false' },
        },
      },
    },
  };
}
