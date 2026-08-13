/**
 * Minimal SVG chart primitives.
 *
 * Hand-rolled rather than pulling in a charting library: two chart shapes on one
 * screen does not justify ~90kb of Recharts, and inline SVG inherits the design
 * tokens directly so the charts match the rest of the console.
 */

const VIEW_WIDTH = 600;
const VIEW_HEIGHT = 160;
const PADDING = { top: 12, right: 8, bottom: 22, left: 40 };

const PLOT_WIDTH = VIEW_WIDTH - PADDING.left - PADDING.right;
const PLOT_HEIGHT = VIEW_HEIGHT - PADDING.top - PADDING.bottom;

export interface ChartPoint {
  label: string;
  value: number;
}

function xFor(index: number, count: number): number {
  if (count <= 1) return PADDING.left + PLOT_WIDTH / 2;
  return PADDING.left + (index / (count - 1)) * PLOT_WIDTH;
}

function yFor(value: number, max: number): number {
  if (max <= 0) return PADDING.top + PLOT_HEIGHT;
  return PADDING.top + PLOT_HEIGHT - (value / max) * PLOT_HEIGHT;
}

function formatTick(value: number): string {
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  if (value < 1 && value > 0) return value.toFixed(2);
  return String(Math.round(value));
}

interface ChartProps {
  points: ChartPoint[];
  color: string;
  /** Formats the accessible summary and tooltips. */
  unit?: string;
}

export function LineChart({ points, color, unit = '' }: ChartProps) {
  if (points.length === 0) return <EmptyChart />;

  const max = Math.max(...points.map((p) => p.value), 0);
  // Headroom so the peak isn't flush against the top edge.
  const scaleMax = max === 0 ? 1 : max * 1.15;

  const path = points
    .map((point, index) => {
      const command = index === 0 ? 'M' : 'L';
      return `${command}${xFor(index, points.length)},${yFor(point.value, scaleMax)}`;
    })
    .join(' ');

  const areaPath =
    `${path} L${xFor(points.length - 1, points.length)},${PADDING.top + PLOT_HEIGHT}` +
    ` L${xFor(0, points.length)},${PADDING.top + PLOT_HEIGHT} Z`;

  return (
    <svg
      className="chart"
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Line chart, peak ${formatTick(max)}${unit}`}
    >
      <Grid scaleMax={scaleMax} unit={unit} />
      <path d={areaPath} fill={color} opacity="0.12" />
      <path d={path} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
      {points.map((point, index) => (
        <circle
          key={point.label}
          cx={xFor(index, points.length)}
          cy={yFor(point.value, scaleMax)}
          r="2.5"
          fill={color}
        >
          <title>{`${point.label}: ${formatTick(point.value)}${unit}`}</title>
        </circle>
      ))}
    </svg>
  );
}

export function BarChart({ points, color, unit = '' }: ChartProps) {
  if (points.length === 0) return <EmptyChart />;

  const max = Math.max(...points.map((p) => p.value), 0);
  const scaleMax = max === 0 ? 1 : max * 1.15;
  const barWidth = Math.max(2, (PLOT_WIDTH / points.length) * 0.62);

  return (
    <svg
      className="chart"
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Bar chart, peak ${formatTick(max)}${unit}`}
    >
      <Grid scaleMax={scaleMax} unit={unit} />
      {points.map((point, index) => {
        const y = yFor(point.value, scaleMax);
        return (
          <rect
            key={point.label}
            x={xFor(index, points.length) - barWidth / 2}
            y={y}
            width={barWidth}
            height={Math.max(0, PADDING.top + PLOT_HEIGHT - y)}
            fill={color}
            rx="2"
          >
            <title>{`${point.label}: ${formatTick(point.value)}${unit}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function Grid({ scaleMax, unit }: { scaleMax: number; unit: string }) {
  const ticks = [0, 0.5, 1];
  return (
    <g>
      {ticks.map((fraction) => {
        const value = scaleMax * fraction;
        const y = yFor(value, scaleMax);
        return (
          <g key={fraction}>
            <line
              x1={PADDING.left}
              y1={y}
              x2={VIEW_WIDTH - PADDING.right}
              y2={y}
              stroke="currentColor"
              strokeOpacity="0.12"
              strokeWidth="1"
            />
            <text x={PADDING.left - 6} y={y + 3} className="chart__tick" textAnchor="end">
              {formatTick(value)}
              {unit}
            </text>
          </g>
        );
      })}
    </g>
  );
}

function EmptyChart() {
  return (
    <div className="chart chart--empty">
      <span>No data in this window</span>
    </div>
  );
}
