// SPDX-License-Identifier: MIT-0

export type ExactChartLineStyle = 'dashed' | 'dotted' | 'solid';
export type ExactChartColor = 'chart-1' | 'chart-2' | 'chart-3' | 'chart-4' | 'chart-5';
export type ExactChartPointStatus = 'available' | 'missing' | 'suppressed';

export interface ExactChartTick {
  readonly key: string;
  readonly label: string;
  readonly position: number;
}

export interface ExactChartScale {
  readonly height: number;
  readonly plot: {
    readonly bottom: number;
    readonly left: number;
    readonly right: number;
    readonly top: number;
  };
  readonly width: number;
  readonly xTicks: readonly ExactChartTick[];
  readonly yTicks: readonly ExactChartTick[];
}

export interface ExactChartPoint {
  readonly key: string;
  readonly label: string;
  readonly status: ExactChartPointStatus;
  readonly value: string | null;
  readonly x: number;
  readonly y: number | null;
}

export interface ExactChartSeries {
  readonly color: ExactChartColor;
  readonly id: string;
  readonly label: string;
  readonly lineStyle: ExactChartLineStyle;
  readonly points: readonly ExactChartPoint[];
}

export interface ExactChartProps {
  readonly description: string;
  readonly headingLevel?: 2 | 3 | 4;
  readonly id: string;
  readonly scale: ExactChartScale;
  readonly series: readonly ExactChartSeries[];
  readonly tableId: string;
  readonly tableLabel?: string;
  readonly title: string;
}

type DrawablePoint = ExactChartPoint & {
  readonly status: 'available';
  readonly value: string;
  readonly y: number;
};

function isDrawable(point: ExactChartPoint): point is DrawablePoint {
  return point.status === 'available' && point.value !== null && point.y !== null;
}

function contiguousSegments(points: readonly ExactChartPoint[]): readonly (readonly DrawablePoint[])[] {
  const result: DrawablePoint[][] = [];
  let current: DrawablePoint[] = [];

  for (const point of points) {
    if (isDrawable(point)) {
      current.push(point);
    } else if (current.length > 0) {
      result.push(current);
      current = [];
    }
  }
  if (current.length > 0) result.push(current);
  return result;
}

function pointList(points: readonly DrawablePoint[]): string {
  return points.map((point) => `${point.x},${point.y}`).join(' ');
}

export function ExactChart({
  description,
  headingLevel = 2,
  id,
  scale,
  series,
  tableId,
  tableLabel = 'View exact data table',
  title,
}: ExactChartProps) {
  const Heading = headingLevel === 4 ? 'h4' : headingLevel === 3 ? 'h3' : 'h2';
  const headingId = `${id}-heading`;
  const descriptionId = `${id}-description`;
  const svgTitleId = `${id}-svg-title`;
  const svgDescriptionId = `${id}-svg-description`;

  return (
    <figure
      className="exact-chart"
      aria-labelledby={headingId}
      aria-describedby={descriptionId}
      data-table-id={tableId}
    >
      <header className="exact-chart__header">
        <Heading id={headingId}>{title}</Heading>
        <ul className="exact-chart__legend" aria-label="Chart series">
          {series.map((item) => (
            <li key={item.id}>
              <span
                aria-hidden="true"
                className={`exact-chart__legend-line exact-chart__series--${item.color} exact-chart__series--${item.lineStyle}`}
              />
              <span>{item.label}</span>
            </li>
          ))}
        </ul>
      </header>
      <svg
        className="exact-chart__svg"
        viewBox={`0 0 ${scale.width} ${scale.height}`}
        role="img"
        aria-labelledby={svgTitleId}
        aria-describedby={svgDescriptionId}
        aria-details={tableId}
        focusable="false"
      >
        <title id={svgTitleId}>{title}</title>
        <desc id={svgDescriptionId}>
          {description} Exact values and gap reasons are available in the associated table.
        </desc>
        <g className="exact-chart__grid" aria-hidden="true">
          {scale.yTicks.map((tick) => (
            <line
              className="exact-chart__grid-line"
              key={tick.key}
              x1={scale.plot.left}
              x2={scale.plot.right}
              y1={tick.position}
              y2={tick.position}
            />
          ))}
          {scale.xTicks.map((tick) => (
            <line
              className="exact-chart__grid-line exact-chart__grid-line--vertical"
              key={tick.key}
              x1={tick.position}
              x2={tick.position}
              y1={scale.plot.top}
              y2={scale.plot.bottom}
            />
          ))}
        </g>
        <g className="exact-chart__axes" aria-hidden="true">
          <line
            className="exact-chart__axis"
            x1={scale.plot.left}
            x2={scale.plot.right}
            y1={scale.plot.bottom}
            y2={scale.plot.bottom}
          />
          <line
            className="exact-chart__axis"
            x1={scale.plot.left}
            x2={scale.plot.left}
            y1={scale.plot.top}
            y2={scale.plot.bottom}
          />
          {scale.yTicks.map((tick) => (
            <text
              className="exact-chart__tick exact-chart__tick--y"
              key={tick.key}
              textAnchor="end"
              x={scale.plot.left}
              y={tick.position}
            >
              {tick.label}
            </text>
          ))}
          {scale.xTicks.map((tick) => (
            <text
              className="exact-chart__tick exact-chart__tick--x"
              key={tick.key}
              textAnchor="middle"
              x={tick.position}
              y={scale.plot.bottom}
            >
              {tick.label}
            </text>
          ))}
        </g>
        <g className="exact-chart__series" aria-hidden="true">
          {series.map((item) => {
            const className = `exact-chart__series--${item.color} exact-chart__series--${item.lineStyle}`;
            const segments = contiguousSegments(item.points);
            return (
              <g className={className} data-series={item.id} key={item.id}>
                {segments.map((segment, segmentIndex) => segment.length < 2 ? null : (
                  <polyline
                    className="exact-chart__line"
                    data-segment={`${item.id}-${segmentIndex}`}
                    key={`${item.id}-${segmentIndex}`}
                    points={pointList(segment)}
                  />
                ))}
                {item.points.map((point) => isDrawable(point) ? (
                  <circle
                    className="exact-chart__point"
                    cx={point.x}
                    cy={point.y}
                    data-point={point.key}
                    data-value={point.value}
                    key={point.key}
                    r="3.5"
                  >
                    <title>{`${item.label}, ${point.label}: ${point.value}`}</title>
                  </circle>
                ) : (
                  <g
                    data-gap={point.status === 'suppressed' ? 'suppressed' : 'missing'}
                    data-point={point.key}
                    key={point.key}
                  />
                ))}
              </g>
            );
          })}
        </g>
      </svg>
      <figcaption>
        <span id={descriptionId}>{description}</span>{' '}
        <a href={`#${tableId}`}>{tableLabel}</a>
      </figcaption>
    </figure>
  );
}
