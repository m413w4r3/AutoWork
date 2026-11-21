export interface BarChartScalePoint {
  startPercent: number;
  widthPercent: number;
}

export interface BarChartScale {
  minimum: number;
  maximum: number;
  range: number;
  zeroPositionPercent: number;
  points: readonly BarChartScalePoint[];
}

export function getBarChartScale(
  values: readonly (number | null)[],
): BarChartScale {
  const finiteValues = values.filter(
    (value): value is number => value !== null && Number.isFinite(value),
  );
  const minimum = Math.min(0, ...finiteValues);
  const maximum = Math.max(0, ...finiteValues);
  const range = maximum - minimum;
  const zeroPositionPercent =
    range === 0 || minimum === 0 ? 0 : (-minimum / range) * 100;

  return {
    minimum,
    maximum,
    range,
    zeroPositionPercent,
    points: values.map((value) => {
      if (value === null || !Number.isFinite(value) || range === 0) {
        return { startPercent: zeroPositionPercent, widthPercent: 0 };
      }
      const widthPercent = (Math.abs(value) / range) * 100;
      return {
        startPercent:
          value < 0 ? zeroPositionPercent - widthPercent : zeroPositionPercent,
        widthPercent,
      };
    }),
  };
}
