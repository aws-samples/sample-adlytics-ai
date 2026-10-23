// SPDX-License-Identifier: MIT-0
import { metricDisplay } from './page-bindings';

const CANONICAL_DECIMAL = /^([+-]?)(0|[1-9][0-9]*)(\.[0-9]+)?$/;
const ISO_DATE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const ISO_INSTANT = /^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$/;

interface DecimalParts {
  readonly fraction: string;
  readonly integer: string;
  readonly sign: string;
}

interface DateParts {
  readonly day: string;
  readonly displayDay: string;
  readonly month: string;
  readonly monthLabel: string;
  readonly source: string;
  readonly year: string;
}

const MONTHS: Readonly<Record<string, { readonly label: string; readonly maximumDay: string }>> = {
  '01': { label: 'Jan', maximumDay: '31' },
  '02': { label: 'Feb', maximumDay: '28' },
  '03': { label: 'Mar', maximumDay: '31' },
  '04': { label: 'Apr', maximumDay: '30' },
  '05': { label: 'May', maximumDay: '31' },
  '06': { label: 'Jun', maximumDay: '30' },
  '07': { label: 'Jul', maximumDay: '31' },
  '08': { label: 'Aug', maximumDay: '31' },
  '09': { label: 'Sep', maximumDay: '30' },
  '10': { label: 'Oct', maximumDay: '31' },
  '11': { label: 'Nov', maximumDay: '30' },
  '12': { label: 'Dec', maximumDay: '31' },
};

function decimalParts(value: string): DecimalParts {
  const match = CANONICAL_DECIMAL.exec(value);
  if (match === null) {
    throw new RangeError(`Expected a canonical decimal string, received ${JSON.stringify(value)}.`);
  }

  const sign = match[1];
  const integer = match[2];
  const fraction = match[3];
  if (sign === undefined || integer === undefined) {
    throw new RangeError('A canonical decimal must contain an integer part.');
  }
  return { fraction: fraction ?? '', integer, sign };
}

function groupedInteger(value: string): string {
  const groups: string[] = [];
  let boundary = value.length;
  while (boundary > 3) {
    groups.unshift(value.slice(boundary - 3, boundary));
    boundary -= 3;
  }
  groups.unshift(value.slice(0, boundary));
  return groups.join(',');
}

function isLeapYear(year: string): boolean {
  const exactYear = BigInt(year);
  return exactYear % 400n === 0n || (exactYear % 4n === 0n && exactYear % 100n !== 0n);
}

function dateParts(value: string): DateParts {
  const match = ISO_DATE.exec(value);
  const year = match?.[1];
  const month = match?.[2];
  const day = match?.[3];
  if (year === undefined || month === undefined || day === undefined) {
    throw new RangeError(`Expected an ISO calendar date, received ${JSON.stringify(value)}.`);
  }

  const monthDefinition = MONTHS[month];
  if (monthDefinition === undefined) {
    throw new RangeError(`Expected an ISO calendar date, received ${JSON.stringify(value)}.`);
  }
  const maximumDay = month === '02' && isLeapYear(year)
    ? '29'
    : monthDefinition.maximumDay;
  if (day === '00' || day > maximumDay) {
    throw new RangeError(`Expected an ISO calendar date, received ${JSON.stringify(value)}.`);
  }

  return {
    day,
    displayDay: day.startsWith('0') ? day.slice(1) : day,
    month,
    monthLabel: monthDefinition.label,
    source: value,
    year,
  };
}

/** Group a canonical decimal without rounding, rescaling, or numeric coercion. */
export function formatCanonicalDecimal(value: string): string {
  const parts = decimalParts(value);
  return `${parts.sign}${groupedInteger(parts.integer)}${parts.fraction}`;
}

/** Render an exact monetary value with its unambiguous ISO 4217 denomination. */
export function formatCurrency(value: string, currencyCode: string): string {
  if (!/^[A-Z]{3}$/.test(currencyCode)) {
    throw new RangeError(`Expected an ISO 4217 currency code, received ${JSON.stringify(currencyCode)}.`);
  }
  return `${formatCanonicalDecimal(value)} ${currencyCode}`;
}

/** Render an exact whole-number count with digit grouping. */
export function formatCount(value: string): string {
  const parts = decimalParts(value);
  if (parts.fraction !== '') {
    throw new RangeError(`Expected a canonical integer count, received ${JSON.stringify(value)}.`);
  }
  return `${parts.sign}${groupedInteger(parts.integer)}`;
}

/** Render an exact ratio without converting, rounding, or trimming its scale. */
export function formatRatio(value: string): string {
  return `${formatCanonicalDecimal(value)}×`;
}

/** Render an already-computed exact percentage without multiplying or rounding it. */
export function formatPercent(value: string): string {
  return `${formatCanonicalDecimal(value)}%`;
}

/** Render an ISO calendar date without applying a runtime timezone or locale. */
export function formatIsoDate(value: string): string {
  const parts = dateParts(value);
  return `${parts.monthLabel} ${parts.displayDay}, ${parts.year}`;
}

/** Render an RFC 3339 whole-second UTC instant as `YYYY-MM-DD HH:MM:SS UTC`, refusing anything else. */
export function formatIsoInstant(value: string): string {
  const match = ISO_INSTANT.exec(value);
  const datePart = match?.[1];
  const hours = match?.[2];
  const minutes = match?.[3];
  const seconds = match?.[4];
  if (datePart === undefined || hours === undefined || minutes === undefined || seconds === undefined
    || hours > '23' || minutes > '59' || seconds > '59') {
    throw new RangeError(`Expected an RFC 3339 UTC instant, received ${JSON.stringify(value)}.`);
  }
  // Validates the calendar date (month length, leap years) without a runtime clock or locale.
  dateParts(datePart);
  return `${datePart} ${hours}:${minutes}:${seconds} UTC`;
}

/** Render an inclusive ISO date range with an unambiguous, compact label. */
export function formatIsoDateRange(start: string, end: string): string {
  const first = dateParts(start);
  const last = dateParts(end);
  if (first.source > last.source) {
    throw new RangeError('The start of an ISO date range must not follow its end.');
  }
  if (first.source === last.source) return formatIsoDate(first.source);
  if (first.year === last.year && first.month === last.month) {
    return `${first.monthLabel} ${first.displayDay}–${last.displayDay}, ${first.year}`;
  }
  if (first.year === last.year) {
    return `${first.monthLabel} ${first.displayDay}–${last.monthLabel} ${last.displayDay}, ${first.year}`;
  }
  return `${formatIsoDate(first.source)}–${formatIsoDate(last.source)}`;
}
/** A served window without its year ("Sep 9–15", "Aug 17–Sep 15"), for a date-control button. */
export function formatIsoDateRangeShort(start: string, end: string): string {
  const first = dateParts(start);
  const last = dateParts(end);
  if (first.source > last.source) {
    throw new RangeError('The start of an ISO date range must not follow its end.');
  }
  if (first.year !== last.year) return formatIsoDateRange(start, end);
  if (first.source === last.source) return `${first.monthLabel} ${first.displayDay}`;
  if (first.month === last.month) return `${first.monthLabel} ${first.displayDay}–${last.displayDay}`;
  return `${first.monthLabel} ${first.displayDay}–${last.monthLabel} ${last.displayDay}`;
}
/**
 * Render a served rate (0.000725) as a percentage at a fixed number of places ("0.073%"),
 * shifting the decimal point exactly and rounding half up only for display. The served value
 * and its change stay unrounded (ADR-121).
 */
export function formatRatePercent(value: string, places: number): string {
  const parts = decimalParts(value);
  const digits = `${parts.integer}${parts.fraction.replace('.', '')}`;
  const scale = Math.max(0, parts.fraction.length - 1);
  // value = digits / 10^scale; percent = digits / 10^(scale - 2); keep `places` decimals.
  const shift = scale - 2 - places;
  let scaled = BigInt(digits === '' ? '0' : digits);
  if (shift > 0) {
    const divisor = 10n ** BigInt(shift);
    const remainder = scaled % divisor;
    scaled = scaled / divisor + (remainder * 2n >= divisor ? 1n : 0n);
  } else if (shift < 0) {
    scaled *= 10n ** BigInt(-shift);
  }
  const text = scaled.toString().padStart(places + 1, '0');
  const whole = text.slice(0, text.length - places);
  const fraction = places === 0 ? '' : `.${text.slice(text.length - places)}`;
  const sign = parts.sign !== '' && scaled !== 0n ? parts.sign : '';
  return `${sign}${groupedInteger(whole)}${fraction}%`;
}

/** Round a canonical decimal half up (away from zero) to `places`, only for display. */
function roundedDecimal(value: string, places: number): string {
  const parts = decimalParts(value);
  const fraction = parts.fraction.replace('.', '');
  const kept = fraction.slice(0, places).padEnd(places, '0');
  let scaled = BigInt(`${parts.integer}${kept}`);
  if ((fraction[places] ?? '0') >= '5') scaled += 1n;
  const text = scaled.toString().padStart(places + 1, '0');
  const whole = text.slice(0, text.length - places);
  const tail = places === 0 ? '' : `.${text.slice(text.length - places)}`;
  const sign = parts.sign === '-' && scaled !== 0n ? '-' : '';
  return `${sign}${groupedInteger(whole)}${tail}`;
}

/**
 * Render a served multiplier (2.2437) at a fixed number of places with a times sign ("2.24×"),
 * rounding half up only for display. The exact value stays in the exact-values disclosure.
 */
export function formatMultiplier(value: string, places: number): string {
  return `${roundedDecimal(value, places)}×`;
}

/** A served figure's display text and its exact text for the "Show exact values" disclosure. */
export interface DisplayedFigure {
  readonly exact: string;
  readonly text: string;
}

/**
 * Display one served figure of a governed metric by its dictionary unit and ratio scale (plan
 * D4): a rate as a 2-place percentage, a multiplier as a 2-place "×", a count grouped, and money
 * as the server's 2-place `display_value` with its ISO code (ADR-084). Nothing is computed: the
 * text only shifts the decimal point and rounds for display.
 */
export function displayFigure(
  metric: string,
  value: string,
  unitKind: string,
  currencyCode: string | null,
  displayValue: string | null = null,
): DisplayedFigure {
  const rule = metricDisplay.get(metric);
  if (rule === undefined) throw new RangeError(`No governed display rule for ${JSON.stringify(metric)}.`);
  if (rule.units !== unitKind) throw new RangeError(`${metric} is served as ${unitKind}, not ${rule.units}.`);
  if (rule.units === 'currency') {
    if (currencyCode === null) throw new RangeError(`${metric} is money without a currency code.`);
    return { exact: formatCurrency(value, currencyCode), text: formatCurrency(displayValue ?? value, currencyCode) };
  }
  if (rule.units === 'count') {
    const text = value.includes('.') ? formatCanonicalDecimal(value) : formatCount(value);
    return { exact: text, text };
  }
  if (rule.ratio_scale === 'rate') return { exact: formatPercent(shiftedPercent(value)), text: formatRatePercent(value, 2) };
  return { exact: formatRatio(value), text: formatMultiplier(value, 2) };
}

/** The exact percentage of a served rate: the decimal point moved two places, nothing rounded. */
function shiftedPercent(value: string): string {
  const parts = decimalParts(value);
  const fraction = parts.fraction.replace('.', '').padEnd(2, '0');
  const integer = `${parts.integer}${fraction.slice(0, 2)}`.replace(/^0+(?=\d)/, '');
  const rest = fraction.slice(2);
  return `${parts.sign}${integer}${rest === '' ? '' : `.${rest}`}`;
}
