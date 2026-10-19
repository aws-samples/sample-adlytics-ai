// SPDX-License-Identifier: MIT-0
/**
 * Turns a generated table definition into Glue's Iceberg table input.
 *
 * L1 `glue.CfnTable` only, per ADR-016: written when Glue had no stable L2 in aws-cdk-lib, and the
 * alpha package is declined. aws-cdk-lib 2.273.0 ships one, so ADR-016 is reopened for Glue
 * (2026-10-16); this stays L1 until that decision is made. `OpenTableFormatInput.IcebergInput` with `MetadataOperation: CREATE`
 * is the supported way to create a new Iceberg table from CloudFormation — it writes the
 * initial metadata file, which a plain `CfnTable` cannot do, so a catalog entry made without
 * it points at metadata that does not exist and every query fails.
 * [verified 2026-08-19] against the CfnTable typings in aws-cdk-lib 2.265.0:
 * `IcebergTableInputProperty` requires `location` and `schema`, and `metadataOperation` is
 * documented as settable only to `CREATE`.
 */

import * as glue from 'aws-cdk-lib/aws-glue';

import type { GeneratedColumn, GeneratedTable } from './generated-tables';

/**
 * Iceberg format version 2.
 *
 * Stated rather than left to default. V2 is what makes row-level deletes and merge-on-read
 * possible, and `dim_product` is an SCD2 merge — on v1 that becomes a full partition rewrite
 * per load. The CDK typings say the default is already 2, which is exactly why it is pinned:
 * a default that changes underneath a table that already exists cannot be corrected by a
 * redeploy, because `MetadataOperation: CREATE` only ever runs once.
 */
const ICEBERG_FORMAT_VERSION = '2';

/**
 * Every partition in the canonical model is an identity partition.
 *
 * `date_key` is already a day and `marketplace_key` is already a short string, so a `day()`
 * or `bucket()` transform would add a derived value that has to be kept consistent with the
 * column it came from. Iceberg's hidden partitioning means an identity partition on
 * `date_key` still prunes on a range predicate, so the transform buys nothing here.
 */
const IDENTITY = 'identity';

/**
 * Athena/Glue type names to Iceberg primitive names.
 *
 * **These vocabularies are not the same, and only one entry differs.** Iceberg's 64-bit
 * integer is `long`; Athena's is `bigint`. [verified 2026-08-19] against a real deploy, which
 * is the only thing that could have caught it — the template synthesises, every assertion
 * passes, and Glue then rejects the create with "Cannot parse type string to primitive:
 * bigint (through reference chain: CreateTableRequest["schema"])".
 *
 * Translated here rather than by changing the generated schema, because `bigint` is the name
 * a human writes in a query and reads back from `SHOW CREATE TABLE`, and every other consumer
 * of `v2-tables.json` — the Pydantic row models, the DQDL ruleset — wants the query-engine name.
 * Iceberg is the one consumer needing its own spelling, so the translation belongs with it.
 *
 * A type absent from this map fails loudly rather than passing through: an unmapped name
 * would reach Glue verbatim and be rejected at deploy, which is far later and much harder to
 * read than a synth-time error naming the column.
 */
const ICEBERG_PRIMITIVES: Readonly<Record<string, string>> = {
  boolean: 'boolean',
  int: 'int',
  bigint: 'long',
  double: 'double',
  'decimal(38,4)': 'decimal(38, 4)',
  date: 'date',
  timestamp: 'timestamp',
  string: 'string',
};

/** A generated table that cannot be expressed as an Iceberg schema. */
export class IcebergSchemaError extends Error {}

/** The Iceberg spelling of one canonical type, or a synth-time failure naming the column. */
export function icebergType(column: GeneratedColumn): string {
  const mapped = ICEBERG_PRIMITIVES[column.type];
  if (mapped === undefined) {
    throw new IcebergSchemaError(
      `${column.name}: no Iceberg primitive is mapped for type ${JSON.stringify(column.type)}. ` +
        `Add it to ICEBERG_PRIMITIVES. Known: ${Object.keys(ICEBERG_PRIMITIVES).join(', ')}.`,
    );
  }
  return mapped;
}

/**
 * The Iceberg field ids for one table, read from the governed generated schema.
 *
 * Glue persists sequential ordinals regardless of the submitted number. The generated artifact
 * therefore carries the compatibility decision and its loader proves each ID equals its 1-based
 * position before this construct can use it. Recomputing here would hide a missing or stale
 * compatibility baseline.
 * Keyed by name rather than returned as an array so the grain and partition lookups, which
 * address columns by name, cannot silently disagree with the schema's own numbering.
 */
export function fieldIds(columns: readonly GeneratedColumn[]): ReadonlyMap<string, number> {
  const ids = new Map<string, number>();
  for (const column of columns) {
    if (ids.has(column.name)) {
      // A duplicate name is a corrupt generated schema, and silent if unchecked: the second
      // column would overwrite the first's id and the table would carry one fewer field than
      // its contract declares.
      throw new IcebergSchemaError(
        `${column.name} appears twice in the generated column list, so its Iceberg field id ` +
          'is ambiguous. Fix the schema generator.',
      );
    }
    ids.set(column.name, column.fieldId);
  }
  return ids;
}

/** Read one column's id, or fail naming the column rather than emitting `undefined`. */
function idOf(ids: ReadonlyMap<string, number>, columnName: string, table: string): number {
  const id = ids.get(columnName);
  if (id === undefined) {
    // Reached when a table's grain or partition keys name a column its own column list does
    // not contain. `v2-tables.json` is generated so this should be impossible, and an undefined
    // field id would synthesise a template Glue rejects with no indication of which column.
    throw new IcebergSchemaError(
      `${table}: ${columnName} is named in the grain or partition keys but is not a column ` +
        'of the table.',
    );
  }
  return id;
}

/**
 * Glue's limit on a schema field's `doc`, in characters.
 *
 * [verified 2026-08-19] by deploy: "Member must have length less than or equal to 255 ...
 * at 'openTableFormatInput.icebergInput.createIcebergTableInput.schema.fields.17.member.doc'".
 * `fct_ad_exposure.suppression_reason` is written by four contracts, so its provenance note
 * is four clauses long and comfortably over.
 */
const MAX_FIELD_DOC = 255;

/**
 * The provenance note for one column, trimmed to what Glue accepts.
 *
 * **The null meaning is kept whole and the provenance is what gets cut.** Which of the four
 * absences applies to a column is load-bearing — a reader who cannot tell "suppressed" from
 * "not applicable under these options" will read a null as a zero — while the list of
 * contributing contracts is a convenience that `v2-tables.json` states in full anyway.
 *
 * A margin below the limit rather than exactly at it, because the limit is documented in
 * characters and the note contains a non-ASCII separator, so a byte-counted check on the
 * service side would round differently.
 */
export function icebergDoc(column: GeneratedColumn): string {
  // Keep the deployed field-doc encoding stable. Changing this value is interpreted by Glue as
  // an OpenTableFormatInput update and can replace a live Iceberg table.
  const nullClause =
    column.nullMeaning === undefined ? '' : ` ? null means: ${column.nullMeaning}`;
  const budget = MAX_FIELD_DOC - nullClause.length - DOC_SAFETY_MARGIN;
  const provenance =
    column.comment.length <= budget ? column.comment : `${column.comment.slice(0, budget)}...`;
  return `${provenance}${nullClause}`;
}

/** Headroom under {@link MAX_FIELD_DOC}, for the multi-byte separator and the ellipsis. */
const DOC_SAFETY_MARGIN = 8;

function icebergField(
  column: GeneratedColumn,
  id: number,
): glue.CfnTable.IcebergStructFieldProperty {
  return {
    id,
    name: column.name,
    // Inverted deliberately. A column is nullable in the generated schema when only some of
    // the contracts writing this table write it, and Iceberg's `required` is the same
    // statement from the other side: a required field must be present on every write.
    required: !column.nullable,
    type: icebergType(column),
    // The provenance comment reaches the catalog, so `SHOW CREATE TABLE` in Athena says
    // where a column came from without the contract open. Trimmed to Glue's 255-character
    // limit, keeping the null meaning whole.
    doc: icebergDoc(column),
  };
}

/**
 * The Iceberg table input for one generated table.
 *
 * `location` is a plain string built from config rather than an SSM token. The bucket name is
 * a pure function of the environment name and account, so it is available at synth — and a
 * deploy-time token here would make the synthesised template unreadable and could not be
 * used to build the IAM resource ARN a reader needs.
 */
export function icebergTableInput(
  table: GeneratedTable,
  bucketName: string,
  locationPrefix?: string,
): glue.CfnTable.IcebergTableInputProperty {
  const ids = fieldIds(table.columns);

  const nullableColumns = new Set(
    table.columns.filter((column) => column.nullable).map((column) => column.name),
  );

  // Iceberg requires every identifier field to be `required`, so the declared grain can only
  // be recorded as row identity when none of its columns is nullable. Where one is, the grain
  // still holds — it is asserted at load — but the catalog cannot carry it, and claiming it
  // here would make an invalid table rather than a lenient one.
  const grainIsIdentifiable =
    table.grain.length > 0 && table.grain.every((column) => !nullableColumns.has(column));

  return {
    location: `s3://${bucketName}/${locationPrefix ?? table.name}/`,
    // Iceberg table properties. `comment` is the key Athena's `SHOW CREATE TABLE` surfaces,
    // and it is the only place a description can go on this path: supplying `TableInput`
    // alongside `IcebergTableInput` is rejected by Glue outright.
    properties: {
      comment:
        `${table.kind}, loaded by ${table.load}. ` +
        `Grain: ${table.grain.join(' + ')}. ` +
        `Source contributors: ${table.writtenBy.join(', ')}. ` +
        `Canonical publisher: ${table.canonicalPublisher}. ` +
        // Deployed catalog metadata is compatibility-sensitive: changing this comment causes Glue
        // to treat the OpenTableFormatInput as an update that may replace the Iceberg table.
        'GENERATED from contracts/generated/v2-tables.json ? edit the contract, not the table.',
    },
    schema: {
      type: 'struct',
      schemaId: 0,
      fields: table.columns.map((column) => icebergField(column, idOf(ids, column.name, table.name))),
      ...(grainIsIdentifiable
        ? { identifierFieldIds: table.grain.map((column) => idOf(ids, column, table.name)) }
        : {}),
    },
    ...(table.partitionKeys.length > 0
      ? {
          partitionSpec: {
            specId: 0,
            fields: table.partitionKeys.map((column) => ({
              name: column,
              sourceId: idOf(ids, column, table.name),
              transform: IDENTITY,
            })),
          },
        }
      : {}),
  };
}

/**
 * The full `CfnTable` props for one generated table.
 *
 * Compaction, target file size and compression are deliberately not set. They are write-path
 * tuning, the write path does not exist yet, and a number guessed here would look like a
 * measured decision to whoever reads it next. They belong with the curation pipeline.
 */
export function cfnTableProps(options: {
  readonly table: GeneratedTable;
  readonly catalogId: string;
  readonly databaseName: string;
  readonly bucketName: string;
  readonly locationPrefix?: string;
}): glue.CfnTableProps {
  const { table, catalogId, databaseName, bucketName, locationPrefix } = options;

  return {
    catalogId,
    databaseName,
    // The table name goes here, at the top level, and **not** in a `TableInput`.
    //
    // [verified 2026-08-19] against a real deploy, which is the only way this was going to
    // surface: the template synthesises and every assertion passes, then Glue rejects the
    // create with "Table metadata is expected only via TableInput or via
    // IcebergTableInputProperties inside OpenTableFormatInput". The two are alternative ways
    // to describe a table and supplying both is an error, so the description moved into the
    // Iceberg table `properties` and `tableInput` went away.
    name: table.name,
    openTableFormatInput: {
      icebergInput: {
        metadataOperation: 'CREATE',
        version: ICEBERG_FORMAT_VERSION,
        icebergTableInput: icebergTableInput(table, bucketName, locationPrefix),
      },
    },
  };
}
